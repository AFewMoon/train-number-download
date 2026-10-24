"""SQLite 存储层：建表、幂等 upsert、任务状态机、CSV 导出。"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .util import ensure_dir, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations(
    telecode      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    pinyin        TEXT,
    short_pinyin  TEXT,
    city_code     TEXT,
    city_name     TEXT,
    order_no      INTEGER,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_stations_name ON stations(name);

CREATE TABLE IF NOT EXISTS trains(
    train_no            TEXT NOT NULL,
    station_train_code  TEXT NOT NULL,
    train_date          TEXT,
    train_class         TEXT,
    start_station       TEXT,
    end_station         TEXT,
    start_telecode      TEXT,
    end_telecode        TEXT,
    depart_time         TEXT,
    arrive_time         TEXT,
    duration_min        INTEGER,
    station_count       INTEGER,
    total_mileage_km    REAL,
    operator            TEXT,
    source              TEXT,
    updated_at          TEXT,
    PRIMARY KEY(train_no, station_train_code)
);
CREATE INDEX IF NOT EXISTS idx_trains_code ON trains(station_train_code);
CREATE INDEX IF NOT EXISTS idx_trains_class ON trains(train_class);

CREATE TABLE IF NOT EXISTS train_stops(
    train_no            TEXT NOT NULL,
    station_train_code  TEXT NOT NULL,
    station_no          INTEGER NOT NULL,
    station_name        TEXT,
    station_telecode    TEXT,
    arrive_time         TEXT,
    depart_time         TEXT,
    stop_minutes        INTEGER,
    day_offset          INTEGER,
    depart_day_offset   INTEGER,
    mileage_km          REAL,
    platform            TEXT,
    track               TEXT,
    is_start            INTEGER,
    is_end              INTEGER,
    source              TEXT,
    PRIMARY KEY(train_no, station_train_code, station_no)
);
CREATE INDEX IF NOT EXISTS idx_stops_name ON train_stops(station_name);

CREATE TABLE IF NOT EXISTS train_formation(
    train_no            TEXT NOT NULL,
    station_train_code  TEXT NOT NULL,
    train_type          TEXT,
    formation_cars      INTEGER,
    seats_total         INTEGER,
    has_dining_car      INTEGER,
    has_sleeper         INTEGER,
    has_ac              INTEGER,
    seat_layout_json    TEXT,
    source              TEXT,
    confidence          REAL,
    PRIMARY KEY(train_no, station_train_code)
);

CREATE TABLE IF NOT EXISTS crawl_tasks(
    stage       TEXT NOT NULL,
    task_key    TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT,
    PRIMARY KEY(stage, task_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON crawl_tasks(stage, status);

CREATE TABLE IF NOT EXISTS meta(
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    ensure_dir(db_path.parent)
    conn = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


# 兼容旧库的增量列迁移：(表, 列, 类型)
_MIGRATIONS = (
    ("train_stops", "depart_day_offset", "INTEGER"),
    ("trains", "operator", "TEXT"),
)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, ctype in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")


# --------------------------------------------------------------------- meta
def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------- stations
def upsert_stations(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = now_iso()
    payload = [
        (
            row.get("telecode"), row.get("name"), row.get("pinyin"), row.get("short_pinyin"),
            row.get("city_code"), row.get("city_name"), row.get("order_no"), now,
        )
        for row in rows
        if row.get("telecode") and row.get("name")
    ]
    conn.executemany(
        """
        INSERT INTO stations(telecode, name, pinyin, short_pinyin, city_code, city_name, order_no, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telecode) DO UPDATE SET
            name=excluded.name, pinyin=excluded.pinyin, short_pinyin=excluded.short_pinyin,
            city_code=excluded.city_code, city_name=excluded.city_name,
            order_no=excluded.order_no, updated_at=excluded.updated_at
        """,
        payload,
    )
    return len(payload)


# ------------------------------------------------------------------- trains
TRAIN_COLUMNS = (
    "train_no", "station_train_code", "train_date", "train_class", "start_station",
    "end_station", "start_telecode", "end_telecode", "depart_time", "arrive_time",
    "duration_min", "station_count", "total_mileage_km", "source",
)


def upsert_trains(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = now_iso()
    payload = [tuple(row.get(col) for col in TRAIN_COLUMNS) + (now,) for row in rows]
    conn.executemany(
        f"""
        INSERT INTO trains({", ".join(TRAIN_COLUMNS)}, updated_at)
        VALUES({", ".join("?" for _ in TRAIN_COLUMNS)}, ?)
        ON CONFLICT(train_no, station_train_code) DO UPDATE SET
            train_date=excluded.train_date,
            train_class=COALESCE(excluded.train_class, trains.train_class),
            start_station=COALESCE(excluded.start_station, trains.start_station),
            end_station=COALESCE(excluded.end_station, trains.end_station),
            start_telecode=COALESCE(excluded.start_telecode, trains.start_telecode),
            end_telecode=COALESCE(excluded.end_telecode, trains.end_telecode),
            depart_time=COALESCE(excluded.depart_time, trains.depart_time),
            arrive_time=COALESCE(excluded.arrive_time, trains.arrive_time),
            duration_min=COALESCE(excluded.duration_min, trains.duration_min),
            station_count=COALESCE(excluded.station_count, trains.station_count),
            total_mileage_km=COALESCE(excluded.total_mileage_km, trains.total_mileage_km),
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        payload,
    )
    return len(payload)


def update_train_mileage(conn: sqlite3.Connection, train_no: str, code: str, mileage: float) -> None:
    conn.execute(
        "UPDATE trains SET total_mileage_km=?, updated_at=? WHERE train_no=? AND station_train_code=?",
        (mileage, now_iso(), train_no, code),
    )


def update_train_operator(conn: sqlite3.Connection, train_no: str, code: str, operator: str) -> None:
    conn.execute(
        "UPDATE trains SET operator=?, updated_at=? WHERE train_no=? AND station_train_code=?",
        (operator, now_iso(), train_no, code),
    )


def stop_rows(conn: sqlite3.Connection, train_no: str, code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT station_no, station_name, mileage_km FROM train_stops"
            " WHERE train_no=? AND station_train_code=? ORDER BY station_no",
            (train_no, code),
        )
    )


# -------------------------------------------------------------------- stops
STOP_COLUMNS = (
    "train_no", "station_train_code", "station_no", "station_name", "station_telecode",
    "arrive_time", "depart_time", "stop_minutes", "day_offset", "depart_day_offset",
    "mileage_km", "platform", "track", "is_start", "is_end", "source",
)


def upsert_stops(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    payload = [tuple(row.get(col) for col in STOP_COLUMNS) for row in rows]
    conn.executemany(
        f"""
        INSERT INTO train_stops({", ".join(STOP_COLUMNS)})
        VALUES({", ".join("?" for _ in STOP_COLUMNS)})
        ON CONFLICT(train_no, station_train_code, station_no) DO UPDATE SET
            station_name=excluded.station_name,
            station_telecode=excluded.station_telecode,
            arrive_time=excluded.arrive_time,
            depart_time=excluded.depart_time,
            stop_minutes=excluded.stop_minutes,
            day_offset=excluded.day_offset,
            depart_day_offset=excluded.depart_day_offset,
            mileage_km=COALESCE(excluded.mileage_km, train_stops.mileage_km),
            platform=COALESCE(excluded.platform, train_stops.platform),
            track=COALESCE(excluded.track, train_stops.track),
            is_start=excluded.is_start,
            is_end=excluded.is_end,
            source=excluded.source
        """,
        payload,
    )
    return len(payload)


def upsert_stop_extras(conn: sqlite3.Connection, updates: Iterable[tuple[Any, ...]]) -> int:
    """第三方补全：按里程/站台更新经停记录。updates 为 (mileage, platform, track, train_no, code, station_no)。"""
    rows = list(updates)
    if not rows:
        return 0
    conn.executemany(
        """
        UPDATE train_stops
        SET mileage_km=COALESCE(?, mileage_km),
            platform=COALESCE(?, platform),
            track=COALESCE(?, track)
        WHERE train_no=? AND station_train_code=? AND station_no=?
        """,
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------- formation
FORMATION_COLUMNS = (
    "train_no", "station_train_code", "train_type", "formation_cars", "seats_total",
    "has_dining_car", "has_sleeper", "has_ac", "seat_layout_json", "source", "confidence",
)


def upsert_formation(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    payload = [tuple(row.get(col) for col in FORMATION_COLUMNS) for row in rows]
    conn.executemany(
        f"""
        INSERT INTO train_formation({", ".join(FORMATION_COLUMNS)})
        VALUES({", ".join("?" for _ in FORMATION_COLUMNS)})
        ON CONFLICT(train_no, station_train_code) DO UPDATE SET
            train_type=COALESCE(excluded.train_type, train_formation.train_type),
            formation_cars=COALESCE(excluded.formation_cars, train_formation.formation_cars),
            seats_total=COALESCE(excluded.seats_total, train_formation.seats_total),
            has_dining_car=COALESCE(excluded.has_dining_car, train_formation.has_dining_car),
            has_sleeper=COALESCE(excluded.has_sleeper, train_formation.has_sleeper),
            has_ac=COALESCE(excluded.has_ac, train_formation.has_ac),
            seat_layout_json=COALESCE(excluded.seat_layout_json, train_formation.seat_layout_json),
            source=excluded.source,
            confidence=excluded.confidence
        """,
        payload,
    )
    return len(payload)


# ------------------------------------------------------------------- tasks
def add_tasks(conn: sqlite3.Connection, stage: str, keys: Iterable[str], status: str = "pending") -> int:
    now = now_iso()
    payload = [(stage, key, status, 0, None, now) for key in keys]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO crawl_tasks(stage, task_key, status, attempts, last_error, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(stage, task_key) DO NOTHING
        """,
        payload,
    )
    return len(payload)


def iter_tasks(
    conn: sqlite3.Connection,
    stage: str,
    statuses: Sequence[str],
    limit: int | None = None,
) -> list[sqlite3.Row]:
    marks = ", ".join("?" for _ in statuses)
    sql = f"SELECT * FROM crawl_tasks WHERE stage=? AND status IN ({marks}) ORDER BY rowid"
    params: list[Any] = [stage, *statuses]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def set_task(conn: sqlite3.Connection, stage: str, key: str, status: str, error: str | None = None, bump: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO crawl_tasks(stage, task_key, status, attempts, last_error, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(stage, task_key) DO UPDATE SET
            status=excluded.status,
            attempts=crawl_tasks.attempts + ?,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (stage, key, status, 1, error, now_iso(), 1 if bump else 0),
    )


def count_tasks(conn: sqlite3.Connection, stage: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM crawl_tasks WHERE stage=? GROUP BY status", (stage,)
    )
    return {row[0]: row[1] for row in rows}


# ------------------------------------------------------------------ export
def export_csv(conn: sqlite3.Connection, sql: str, path: Path, params: Sequence[Any] = ()) -> int:
    ensure_dir(path.parent)
    cursor = conn.execute(sql, params)
    headers = [col[0] for col in cursor.description or []]
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in cursor:
            writer.writerow(["" if value is None else value for value in row])
            count += 1
    return count
