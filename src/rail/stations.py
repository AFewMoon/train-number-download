"""车站库：抓取并解析 station_name.js。"""

from __future__ import annotations

import sqlite3

from . import storage
from .config import Settings
from .http import INIT_URL, STATION_JS_URL, HttpClient
from .parsers import parse_station_js
from .util import now_iso

BATCH = 500


def import_stations(client: HttpClient, settings: Settings, conn: sqlite3.Connection, logger=None) -> int:
    text = client.get_text(STATION_JS_URL, referer=INIT_URL)
    rows = parse_station_js(text)
    total = 0
    for start in range(0, len(rows), BATCH):
        total += storage.upsert_stations(conn, rows[start : start + BATCH])
    storage.set_meta(conn, "stations_updated_at", now_iso())
    if logger:
        logger.info("车站库导入完成：%s 个车站", total)
    return total


def station_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0])
