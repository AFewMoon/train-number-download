"""导出：CSV（Excel 友好，UTF-8 BOM）与嵌套 JSONL。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import storage
from .config import Settings
from .util import ensure_dir

TRAIN_SQL = """
SELECT
    t.station_train_code AS 车次,
    t.train_no           AS 内部编号,
    t.train_class        AS 等级,
    t.start_station      AS 始发站,
    t.end_station        AS 终到站,
    t.depart_time        AS 始发时刻,
    t.arrive_time        AS 终到时刻,
    t.duration_min       AS 历时分钟,
    t.station_count      AS 经停站数,
    t.total_mileage_km   AS 全程里程km,
    t.start_telecode     AS 始发电报码,
    t.end_telecode       AS 终到电报码,
    f.train_type         AS 车型,
    f.formation_cars     AS 编组辆数,
    f.seats_total        AS 定员,
    f.has_dining_car     AS 餐车,
    f.has_sleeper        AS 卧铺,
    f.has_ac             AS 空调,
    t.operator           AS 担当路局,
    f.source             AS 编组来源,
    f.confidence         AS 编组置信度,
    t.train_date         AS 快照日期,
    t.updated_at         AS 更新时间
FROM trains t
LEFT JOIN train_formation f
       ON f.train_no = t.train_no AND f.station_train_code = t.station_train_code
ORDER BY t.station_train_code, t.train_no
"""

STOP_SQL = """
SELECT
    s.station_train_code AS 车次,
    s.station_no         AS 站序,
    s.station_name       AS 站名,
    s.arrive_time        AS 到达时刻,
    s.depart_time        AS 开车时刻,
    s.stop_minutes       AS 停留分钟,
    s.day_offset         AS 到达跨天,
    s.depart_day_offset  AS 开车跨天,
    s.mileage_km         AS 里程km,
    s.platform           AS 站台,
    s.track              AS 股道,
    s.is_start           AS 始发站,
    s.is_end             AS 终到站,
    s.station_telecode   AS 电报码,
    st.pinyin            AS 拼音,
    st.city_name         AS 城市,
    t.train_class        AS 等级,
    t.train_date         AS 快照日期
FROM train_stops s
LEFT JOIN stations st ON st.telecode = s.station_telecode
LEFT JOIN trains t
       ON t.train_no = s.train_no AND t.station_train_code = s.station_train_code
ORDER BY s.station_train_code, s.station_no
"""

FORMATION_SQL = """
SELECT
    station_train_code AS 车次,
    train_no           AS 内部编号,
    train_type         AS 车型,
    formation_cars     AS 编组辆数,
    seats_total        AS 定员,
    has_dining_car     AS 餐车,
    has_sleeper        AS 卧铺,
    has_ac             AS 空调,
    seat_layout_json   AS 席别布局,
    source             AS 来源,
    confidence         AS 置信度
FROM train_formation
ORDER BY station_train_code
"""

STATION_SQL = """
SELECT
    telecode     AS 电报码,
    name         AS 站名,
    pinyin       AS 全拼,
    short_pinyin AS 简拼,
    city_name    AS 城市,
    city_code    AS 城市代码,
    order_no     AS 序号
FROM stations
ORDER BY short_pinyin
"""


def export_csvs(settings: Settings, conn: sqlite3.Connection, logger) -> dict[str, int]:
    ensure_dir(settings.csv_dir)
    jobs = {
        "trains": TRAIN_SQL,
        "stops": STOP_SQL,
        "formation": FORMATION_SQL,
        "stations": STATION_SQL,
    }
    counts: dict[str, int] = {}
    for name, sql in jobs.items():
        path = settings.csv_dir / f"{name}.csv"
        counts[name] = storage.export_csv(conn, sql, path)
        logger.info("导出 %s -> %s（%s 行）", name, path.relative_to(settings.root), counts[name])
    return counts


def export_jsonl(settings: Settings, conn: sqlite3.Connection, logger) -> tuple[Path, int]:
    """把每个车次及其经停站写成一行的嵌套 JSONL，便于程序化消费。"""
    path = settings.raw_dir.parent / "trains.jsonl"
    ensure_dir(path.parent)
    total = 0
    with path.open("w", encoding="utf-8") as fh:
        cursor = conn.execute(
            """
            SELECT t.*, f.train_type, f.formation_cars, f.seats_total,
                   f.has_dining_car, f.has_sleeper, f.has_ac, f.source AS formation_source,
                   f.confidence
            FROM trains t
            LEFT JOIN train_formation f
                   ON f.train_no = t.train_no AND f.station_train_code = t.station_train_code
            ORDER BY t.station_train_code, t.train_no
            """
        )
        for row in cursor:
            stops = [
                dict(stop)
                for stop in conn.execute(
                    "SELECT station_no, station_name, station_telecode, arrive_time, depart_time,"
                    " stop_minutes, day_offset, depart_day_offset, mileage_km, platform, track, is_start, is_end"
                    " FROM train_stops WHERE train_no=? AND station_train_code=? ORDER BY station_no",
                    (row["train_no"], row["station_train_code"]),
                )
            ]
            record = {
                "station_train_code": row["station_train_code"],
                "train_no": row["train_no"],
                "train_date": row["train_date"],
                "train_class": row["train_class"],
                "start_station": row["start_station"],
                "end_station": row["end_station"],
                "depart_time": row["depart_time"],
                "arrive_time": row["arrive_time"],
                "duration_min": row["duration_min"],
                "station_count": row["station_count"],
                "total_mileage_km": row["total_mileage_km"],
                "operator": row["operator"],
                "formation": {
                    "train_type": row["train_type"],
                    "cars": row["formation_cars"],
                    "seats": row["seats_total"],
                    "dining": row["has_dining_car"],
                    "sleeper": row["has_sleeper"],
                    "ac": row["has_ac"],
                    "source": row["formation_source"],
                    "confidence": row["confidence"],
                },
                "stops": stops,
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            total += 1
    logger.info("导出嵌套 JSONL -> %s（%s 趟）", path.relative_to(settings.root), total)
    return path, total
