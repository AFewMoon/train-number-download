"""编组与车辆信息：人工校正 -> 车型字典 -> 车次等级规则，逐级降级并记录来源。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import storage
from .config import Settings
from .http import HttpClient

# 车次等级 -> 布尔特征。None 表示「未知」，不写入库，避免臆造。
CLASS_RULES: dict[str, dict[str, int | None]] = {
    "高速": {"dining": 1, "sleeper": 0, "ac": 1},
    "动车": {"dining": 1, "sleeper": None, "ac": 1},
    "城际": {"dining": 1, "sleeper": 0, "ac": 1},
    "直达": {"dining": 1, "sleeper": 1, "ac": 1},
    "特快": {"dining": 1, "sleeper": 1, "ac": 1},
    "快速": {"dining": 1, "sleeper": None, "ac": 1},
    "普快": {"dining": None, "sleeper": None, "ac": None},
    "普客": {"dining": None, "sleeper": None, "ac": None},
    "旅游": {"dining": None, "sleeper": None, "ac": None},
}

CLASS_CONFIDENCE = 0.45
DICT_CONFIDENCE = 0.9
OVERRIDE_CONFIDENCE = 0.95


def load_rolling_stock(settings: Settings) -> dict[str, dict[str, Any]]:
    path = settings.data_dir / "rolling_stock.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    types = payload.get("types") or {}
    return {str(key).upper(): value for key, value in types.items() if isinstance(value, dict)}


def load_overrides(settings: Settings) -> dict[str, dict[str, Any]]:
    path = settings.data_dir / "train_formation_overrides.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    trains = payload.get("trains") or {}
    return {str(key).upper(): value for key, value in trains.items() if isinstance(value, dict)}


def _normalize_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().upper().replace(" ", "")


def build_row(
    train_no: str,
    code: str,
    train_class: str | None,
    current: sqlite3.Row | None,
    stock: dict[str, dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
    enable_class_rules: bool = True,
) -> dict[str, Any] | None:
    existing_type = _normalize_type(current["train_type"]) if current else None
    existing_source = current["source"] if current else None

    override = overrides.get(code.upper())
    if override:
        train_type = _normalize_type(override.get("train_type")) or existing_type
        spec = stock.get(train_type or "", {})
        return _row(
            train_no, code, train_type,
            override.get("formation_cars", spec.get("cars")),
            override.get("seats_total", spec.get("seats")),
            override.get("has_dining_car", spec.get("dining")),
            override.get("has_sleeper", spec.get("sleeper")),
            override.get("has_ac", spec.get("ac")),
            spec.get("layout"),
            "override", OVERRIDE_CONFIDENCE,
        )

    train_type = existing_type
    if train_type and train_type in stock:
        spec = stock[train_type]
        return _row(
            train_no, code, train_type,
            spec.get("cars"), spec.get("seats"), spec.get("dining"),
            spec.get("sleeper"), spec.get("ac"), spec.get("layout"),
            f"dictionary+{existing_source or 'provider'}", DICT_CONFIDENCE,
        )

    if train_type:
        return _row(train_no, code, train_type, None, None, None, None, None, None,
                    existing_source or "provider", 0.6)

    if not enable_class_rules:
        return None

    rule = CLASS_RULES.get((train_class or "").strip())
    if not rule:
        return None
    if all(value is None for value in rule.values()):
        return None
    return _row(
        train_no, code, None, None, None,
        rule.get("dining"), rule.get("sleeper"), rule.get("ac"),
        None, "rule:train_class", CLASS_CONFIDENCE,
    )


def _row(
    train_no: str,
    code: str,
    train_type: str | None,
    cars: Any,
    seats: Any,
    dining: Any,
    sleeper: Any,
    ac: Any,
    layout: str | None,
    source: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "train_no": train_no,
        "station_train_code": code,
        "train_type": train_type,
        "formation_cars": cars,
        "seats_total": seats,
        "has_dining_car": dining,
        "has_sleeper": sleeper,
        "has_ac": ac,
        "seat_layout_json": json.dumps({"layout": layout}, ensure_ascii=False) if layout else None,
        "source": source,
        "confidence": confidence,
    }


def apply_formation(
    conn: sqlite3.Connection,
    settings: Settings,
    logger=None,
    limit: int | None = None,
) -> dict[str, int]:
    stock = load_rolling_stock(settings)
    overrides = load_overrides(settings)

    sql = """
        SELECT t.train_no, t.station_train_code, t.train_class,
               f.train_type, f.source
        FROM trains t
        LEFT JOIN train_formation f
          ON f.train_no = t.train_no AND f.station_train_code = t.station_train_code
    """
    rows = list(conn.execute(sql))
    if limit:
        rows = rows[:limit]

    pending: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        built = build_row(
            row["train_no"], row["station_train_code"], row["train_class"],
            row, stock, overrides, settings.enrich.enable_class_rules,
        )
        if built is None:
            continue
        pending.append(built)
        counts[built["source"].split("+")[0]] = counts.get(built["source"].split("+")[0], 0) + 1

    for start in range(0, len(pending), 200):
        storage.upsert_formation(conn, pending[start : start + 200])

    if logger:
        logger.info(
            "编组补全：写入 %s 条（%s）",
            len(pending),
            ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "无",
        )
    return {"rows": len(pending), **counts}


def pending_provider_trains(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """需要第三方补全的车次：车型未知或缺少里程；已成功的任务（crawl_tasks.status=done）跳过，便于断点续跑。"""
    sql = """
        SELECT t.train_no, t.station_train_code, t.total_mileage_km,
               f.train_type
        FROM trains t
        LEFT JOIN train_formation f
          ON f.train_no = t.train_no AND f.station_train_code = t.station_train_code
        WHERE (f.train_type IS NULL OR t.total_mileage_km IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM crawl_tasks c
              WHERE c.stage = 'enrich'
                AND c.task_key = t.train_no || '|' || t.station_train_code
                AND c.status = 'done'
          )
        ORDER BY t.station_train_code
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))
