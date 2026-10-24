"""质量校验与统计报表。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import storage
from .config import Settings
from .util import now_iso

PREFIX_EXPR = "CASE WHEN substr(station_train_code,1,1) BETWEEN 'A' AND 'Z' THEN substr(station_train_code,1,1) ELSE '数字' END"


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    value = conn.execute(sql).fetchone()[0]
    return int(value or 0)


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return list(conn.execute(sql))


def collect_metrics(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    trains = _scalar(conn, "SELECT COUNT(*) FROM trains")
    stops = _scalar(conn, "SELECT COUNT(*) FROM train_stops")

    coverage = {
        "车次·始发时刻": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE depart_time IS NOT NULL"),
        "车次·终到时刻": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE arrive_time IS NOT NULL"),
        "车次·全程历时": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE duration_min IS NOT NULL"),
        "车次·站数": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE station_count IS NOT NULL"),
        "车次·全程里程": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE total_mileage_km IS NOT NULL"),
        "车次·始发电报码": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE start_telecode IS NOT NULL"),
        "车次·担当路局": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE operator IS NOT NULL"),
        "经停·电报码": _scalar(conn, "SELECT COUNT(*) FROM train_stops WHERE station_telecode IS NOT NULL"),
        "经停·里程": _scalar(conn, "SELECT COUNT(*) FROM train_stops WHERE mileage_km IS NOT NULL"),
        "经停·站台": _scalar(conn, "SELECT COUNT(*) FROM train_stops WHERE platform IS NOT NULL"),
        "编组·车型": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE train_type IS NOT NULL"),
        "编组·辆数": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE formation_cars IS NOT NULL"),
        "编组·定员": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE seats_total IS NOT NULL"),
        "编组·餐车": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE has_dining_car IS NOT NULL"),
        "编组·卧铺": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE has_sleeper IS NOT NULL"),
        "编组·空调": _scalar(conn, "SELECT COUNT(*) FROM train_formation WHERE has_ac IS NOT NULL"),
    }
    coverage_ratio = {
        key: (round(value / trains * 100, 1) if trains and not key.startswith("经停") else
              (round(value / stops * 100, 1) if stops and key.startswith("经停") else None))
        for key, value in coverage.items()
    }

    validations = {
        "单站车次（时刻表异常）": _scalar(conn, "SELECT COUNT(*) FROM trains WHERE station_count = 1"),
        "无经停记录的车次": _scalar(
            conn,
            "SELECT COUNT(*) FROM trains t WHERE NOT EXISTS ("
            " SELECT 1 FROM train_stops s WHERE s.train_no=t.train_no"
            " AND s.station_train_code=t.station_train_code)",
        ),
        "里程非单调递增的经停序列": _scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT 1,
                     LAG(mileage_km) OVER (PARTITION BY train_no, station_train_code ORDER BY station_no) AS prev,
                     mileage_km
              FROM train_stops WHERE mileage_km IS NOT NULL
            ) WHERE prev IS NOT NULL AND mileage_km < prev
            """,
        ),
        # 同一天内到达时刻晚于开车时刻才算异常；跨天开车（如 23:59 到、00:05 开）是正常现象
        "同站到达晚于开车": _scalar(
            conn,
            "SELECT COUNT(*) FROM train_stops WHERE arrive_time IS NOT NULL"
            " AND depart_time IS NOT NULL AND arrive_time > depart_time"
            " AND COALESCE(depart_day_offset, 0) = COALESCE(day_offset, 0)",
        ),
        "终到站残留开车时刻": _scalar(
            conn, "SELECT COUNT(*) FROM train_stops WHERE is_end = 1 AND depart_time IS NOT NULL"
        ),
        "跨天（day_offset>0）经停": _scalar(conn, "SELECT COUNT(*) FROM train_stops WHERE day_offset > 0"),
        "跨天开车（depart_day_offset>day_offset）": _scalar(
            conn,
            "SELECT COUNT(*) FROM train_stops WHERE COALESCE(depart_day_offset,0) > COALESCE(day_offset,0)",
        ),
    }

    class_rows = _rows(
        conn,
        "SELECT COALESCE(train_class,'未知') AS cls, COUNT(*) AS cnt FROM trains GROUP BY cls ORDER BY cnt DESC",
    )
    prefix_rows = _rows(
        conn,
        f"SELECT {PREFIX_EXPR} AS prefix, COUNT(*) AS cnt FROM trains GROUP BY prefix ORDER BY cnt DESC",
    )
    top_routes = _rows(
        conn,
        """
        SELECT start_station || '→' || end_station AS route, COUNT(*) AS cnt
        FROM trains
        WHERE start_station IS NOT NULL AND end_station IS NOT NULL
        GROUP BY route ORDER BY cnt DESC, route LIMIT 20
        """,
    )
    stop_buckets = _rows(
        conn,
        """
        SELECT CASE
                 WHEN station_count <= 2 THEN '2站及以下'
                 WHEN station_count <= 5 THEN '3-5站'
                 WHEN station_count <= 10 THEN '6-10站'
                 WHEN station_count <= 20 THEN '11-20站'
                 ELSE '20站以上' END AS bucket,
               COUNT(*) AS cnt
        FROM trains WHERE station_count IS NOT NULL
        GROUP BY bucket ORDER BY cnt DESC
        """,
    )
    task_rows = _rows(
        conn,
        "SELECT stage, status, COUNT(*) AS cnt FROM crawl_tasks GROUP BY stage, status ORDER BY stage, status",
    )

    return {
        "generated_at": now_iso(),
        "date": settings.date,
        "totals": {
            "trains": trains,
            "stops": stops,
            "stations": _scalar(conn, "SELECT COUNT(*) FROM stations"),
            "formation": _scalar(conn, "SELECT COUNT(*) FROM train_formation"),
        },
        "coverage": coverage,
        "coverage_ratio": coverage_ratio,
        "validations": validations,
        "classes": [dict(row) for row in class_rows],
        "prefixes": [dict(row) for row in prefix_rows],
        "top_routes": [dict(row) for row in top_routes],
        "stop_buckets": [dict(row) for row in stop_buckets],
        "tasks": [dict(row) for row in task_rows],
    }


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def write_report(settings: Settings, conn: sqlite3.Connection, metrics: dict[str, Any]) -> Path:
    path = settings.raw_dir.parent / "report.md"
    totals = metrics["totals"]
    parts = [
        "# 中国铁路车次数据抓取报告",
        "",
        f"- 生成时间：{metrics['generated_at']}",
        f"- 数据快照日期：{metrics['date']}",
        f"- 车次：**{totals['trains']}** 趟｜经停记录：**{totals['stops']}** 条｜车站：{totals['stations']} 个｜编组记录：{totals['formation']} 条",
        "",
        "## 字段覆盖率",
        "",
        _md_table(
            ["字段", "非空条数", "覆盖率"],
            [
                [key, value, f"{metrics['coverage_ratio'][key]}%" if metrics["coverage_ratio"][key] is not None else "-"]
                for key, value in metrics["coverage"].items()
            ],
        ),
        "",
        "## 数据校验",
        "",
        _md_table(["检查项", "数量"], [[key, value] for key, value in metrics["validations"].items()]),
        "",
        "## 车次等级分布",
        "",
        _md_table(["等级", "车次数"], [[row["cls"], row["cnt"]] for row in metrics["classes"]]),
        "",
        "## 车次号前缀分布",
        "",
        _md_table(["前缀", "车次数"], [[row["prefix"], row["cnt"]] for row in metrics["prefixes"]]),
        "",
        "## 经停站数分布",
        "",
        _md_table(["区间", "车次数"], [[row["bucket"], row["cnt"]] for row in metrics["stop_buckets"]]),
        "",
        "## 车次最多的起终点（Top 20）",
        "",
        _md_table(["线路", "车次数"], [[row["route"], row["cnt"]] for row in metrics["top_routes"]]),
        "",
        "## 任务状态",
        "",
        _md_table(["阶段", "状态", "数量"], [[row["stage"], row["status"], row["cnt"]] for row in metrics["tasks"]]),
        "",
        "## 数据来源与口径",
        "",
        "- 车次目录、逐站到发时刻：12306 官方接口（`search/v1/train/search`、`otn/czxx/queryByTrainNo`），来源标记 `12306:*`。",
        "- 车站电报码、拼音、城市：12306 `station_name.js`。",
        "- 编组辆数、定员：`data/rolling_stock.json` 车型字典中的典型编组（`source=dictionary`），非逐车核实值。",
        "- 卧铺/餐车/空调：车型字典；车型未知时按车次等级规则推断（`source=rule:train_class`，置信度 0.45）。",
        "- 里程、站台：12306 不提供，仅由第三方 provider 补全（`source=thirdparty:*`），未获取到即为空。",
        "",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def write_failures(settings: Settings, conn: sqlite3.Connection) -> Path:
    """failed 全列；enrich 阶段的 skipped 表示「第三方没有该车次页」，同样属于未获取，一并列出。"""
    path = settings.raw_dir.parent / "failures.csv"
    storage.export_csv(
        conn,
        "SELECT stage, task_key, attempts, last_error, updated_at FROM crawl_tasks"
        " WHERE status='failed' OR (status='skipped' AND stage='enrich')"
        " ORDER BY stage, task_key",
        path,
    )
    return path


def print_summary(metrics: dict[str, Any], logger) -> None:
    totals = metrics["totals"]
    logger.info(
        "汇总：车次 %s 趟 / 经停 %s 条 / 车站 %s 个 / 编组 %s 条",
        totals["trains"], totals["stops"], totals["stations"], totals["formation"],
    )
    for key, value in metrics["coverage"].items():
        ratio = metrics["coverage_ratio"][key]
        logger.info("  覆盖率 %-14s %8s 条 %6s%%", key, value, ratio if ratio is not None else "-")
    logger.info("  校验 %s", metrics["validations"])
