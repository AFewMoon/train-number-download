"""第三方补全编排：按 provider 顺序尝试，失败降级，结果按来源与置信度入库。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

from tqdm import tqdm

from . import formation, storage
from .config import Settings
from .providers import ProviderUnavailable, build_providers
from .util import append_jsonl, now_iso

STAGE = "enrich"


def _align_mileage(conn: sqlite3.Connection, train_no: str, code: str, result) -> list[tuple]:
    """把第三方给的里程对齐到本库的站序：优先按站名，其次按站号，首站补 0、末站补全程。"""
    rows = storage.stop_rows(conn, train_no, code)
    if not rows:
        return []

    by_name = {name: km for name, km in result.stop_mileage_by_name.items()}
    mileage: dict[int, float] = {}
    for row in rows:
        name = (row["station_name"] or "").strip()
        if name in by_name:
            mileage[row["station_no"]] = by_name[name]
        elif row["mileage_km"] is None and result.stops.get(row["station_no"]):
            extra = result.stops[row["station_no"]]
            if extra.mileage_km is not None:
                mileage[row["station_no"]] = extra.mileage_km

    if not mileage:
        return []

    first_no, last_no = rows[0]["station_no"], rows[-1]["station_no"]
    mileage.setdefault(first_no, 0.0)
    total = result.total_mileage_km or max(mileage.values())
    if total:
        mileage.setdefault(last_no, float(total))

    # 里程必须随站序单调不减，否则视为解析错位，整趟丢弃
    ordered = [mileage.get(row["station_no"]) for row in rows]
    last_value = -1.0
    for value in ordered:
        if value is None:
            continue
        if value < last_value:
            return []
        last_value = value

    return [
        (value, None, None, train_no, code, row["station_no"])
        for row in rows
        if (value := mileage.get(row["station_no"])) is not None
    ]


def _apply(conn: sqlite3.Connection, train_no: str, code: str, result) -> None:
    if result.train_type:
        storage.upsert_formation(
            conn,
            [
                {
                    "train_no": train_no,
                    "station_train_code": code,
                    "train_type": result.train_type,
                    "formation_cars": None,
                    "seats_total": None,
                    "has_dining_car": None,
                    "has_sleeper": None,
                    "has_ac": None,
                    "seat_layout_json": None,
                    "source": result.source,
                    "confidence": result.confidence,
                }
            ],
        )
    if result.operator:
        storage.update_train_operator(conn, train_no, code, result.operator)
    updates = _align_mileage(conn, train_no, code, result)
    if updates:
        storage.upsert_stop_extras(conn, updates)
    if result.total_mileage_km:
        storage.update_train_mileage(conn, train_no, code, result.total_mileage_km)


def run_enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    logger,
    limit: int | None = None,
) -> dict[str, int]:
    targets = formation.pending_provider_trains(conn, limit)
    if not targets:
        logger.info("没有需要第三方补全的车次")
        return {"targets": 0, "enriched": 0, "unavailable": 0}

    providers = build_providers(settings, logger)
    if not providers:
        logger.info("未配置第三方 provider，跳过补全")
        return {"targets": len(targets), "enriched": 0, "unavailable": 0}

    raw_path: Path = settings.raw_dir / "enrich.jsonl"
    failures: dict[str, int] = {provider.name: 0 for provider in providers}
    unavailable: set[str] = set()
    enriched = 0

    progress = tqdm(total=len(targets), desc="enrich", unit="趟", disable=None)
    try:
        for row in targets:
            code, train_no = row["station_train_code"], row["train_no"]
            for provider in providers:
                if provider.name in unavailable:
                    continue
                try:
                    result = provider.fetch(code, train_no)
                except ProviderUnavailable as exc:
                    failures[provider.name] = failures.get(provider.name, 0) + 1
                    if failures[provider.name] >= 3 and provider.name not in unavailable:
                        unavailable.add(provider.name)
                        logger.warning(
                            "provider %s 连续不可用（%s），本次运行已停用：%s",
                            provider.name, failures[provider.name], exc,
                        )
                    continue
                except Exception as exc:  # provider 内部异常不应中断整批
                    failures[provider.name] = failures.get(provider.name, 0) + 1
                    logger.debug("provider %s 处理 %s 异常: %s", provider.name, code, exc)
                    continue

                if not result:
                    continue
                _apply(conn, train_no, code, result)
                append_jsonl(
                    raw_path,
                    {
                        "fetched_at": now_iso(),
                        "train_no": train_no,
                        "station_train_code": code,
                        "source": result.source,
                        "train_type": result.train_type,
                        "operator": result.operator,
                        "total_mileage_km": result.total_mileage_km,
                        "stops": {str(k): asdict(v) for k, v in result.stops.items()},
                        "stop_mileage_by_name": result.stop_mileage_by_name,
                    },
                )
                enriched += 1
                storage.set_task(conn, STAGE, f"{train_no}|{code}", "done")
                break
            else:
                storage.set_task(
                    conn, STAGE, f"{train_no}|{code}", "skipped",
                    "provider unavailable" if unavailable else "no result",
                )
            progress.update(1)
    finally:
        progress.close()
        for provider in providers:
            provider.close()

    stats = {"targets": len(targets), "enriched": enriched, "unavailable": len(unavailable)}
    logger.info("第三方补全完成：目标 %s，成功 %s，停用 provider %s 个", len(targets), enriched, len(unavailable))
    return stats
