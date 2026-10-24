"""逐站时刻表抓取：线程池并发获取，主线程串行落库。"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tqdm import tqdm

from . import storage
from .config import Settings
from .http import INIT_URL, TIMETABLE_URL, HttpClient
from .parsers import (
    Timetable,
    build_stop_rows,
    build_train_row,
    parse_timetable_payload,
)
from .util import append_jsonl, now_iso

STAGE = "timetable"


@dataclass(slots=True)
class FetchResult:
    train_no: str
    code: str
    timetable: Timetable | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None


def fetch_timetable(client: HttpClient, train_no: str, code: str, date: str) -> tuple[Timetable, dict[str, Any]]:
    payload = client.get_json(
        TIMETABLE_URL,
        {
            "train_no": train_no,
            "from_station_telecode": "",
            "to_station_telecode": "",
            "depart_date": date,
        },
        referer=INIT_URL,
    )
    return parse_timetable_payload(payload), payload


def load_station_telecodes(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["name"]: row["telecode"]
        for row in conn.execute("SELECT name, telecode FROM stations")
        if row["name"]
    }


def crawl_timetable(
    client: HttpClient,
    settings: Settings,
    conn: sqlite3.Connection,
    limit: int | None = None,
    retry_failed: bool = False,
    logger=None,
) -> dict[str, int]:
    statuses = ("pending", "failed") if retry_failed else ("pending",)
    tasks = storage.iter_tasks(conn, STAGE, statuses, limit)
    if not tasks:
        if logger:
            logger.info("没有待抓取的时刻表任务（可用 --retry-failed 重跑失败项）")
        return {"total": 0, "ok": 0, "failed": 0}

    telecodes = load_station_telecodes(conn)
    raw_path = settings.raw_dir / "timetable.jsonl"
    workers = settings.http.workers
    total = len(tasks)
    ok = failed = 0
    lock = threading.Lock()

    def work(row: sqlite3.Row) -> FetchResult:
        train_no, code = str(row["task_key"]).split("|", 1)
        try:
            timetable, payload = fetch_timetable(client, train_no, code, settings.date)
            return FetchResult(train_no, code, timetable, payload)
        except Exception as exc:
            return FetchResult(train_no, code, error=f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, row): row for row in tasks}
        progress = tqdm(total=total, desc="timetable", unit="趟", disable=None)
        for future in as_completed(futures):
            result = future.result()
            key = f"{result.train_no}|{result.code}"
            try:
                if result.timetable is not None:
                    train_row = build_train_row(
                        {"train_no": result.train_no, "station_train_code": result.code},
                        result.timetable,
                        telecodes,
                        settings.date,
                    )
                    stop_rows = build_stop_rows(
                        {"train_no": result.train_no, "station_train_code": result.code},
                        result.timetable,
                        telecodes,
                    )
                    storage.upsert_trains(conn, [train_row])
                    storage.upsert_stops(conn, stop_rows)
                    append_jsonl(
                        raw_path,
                        {
                            "fetched_at": now_iso(),
                            "stage": "timetable",
                            "train_no": result.train_no,
                            "station_train_code": result.code,
                            "payload": result.payload,
                        },
                    )
                    storage.set_task(conn, STAGE, key, "done", bump=False)
                    ok += 1
                else:
                    storage.set_task(conn, STAGE, key, "failed", result.error, bump=True)
                    failed += 1
                    if logger and failed <= 10:
                        logger.warning("时刻表失败 %s: %s", key, result.error)
            except Exception as exc:
                storage.set_task(conn, STAGE, key, "failed", f"persist: {exc}", bump=True)
                failed += 1
                if logger:
                    logger.exception("写入失败 %s", key)
            finally:
                with lock:
                    progress.update(1)
        progress.close()

    storage.set_meta(conn, "timetable_updated_at", now_iso())
    stats = {"total": total, "ok": ok, "failed": failed}
    if logger:
        logger.info("时刻表阶段完成：成功 %s / 失败 %s / 共 %s", ok, failed, total)
    return stats


def reload_pending(conn: sqlite3.Connection, logger=None) -> int:
    """把仍缺失逐站数据的车次重新置为 pending（用于修复历史失败）。"""
    cursor = conn.execute(
        """
        SELECT t.train_no, t.station_train_code
        FROM trains t
        LEFT JOIN train_stops s
          ON s.train_no = t.train_no AND s.station_train_code = t.station_train_code
        WHERE s.train_no IS NULL
        """
    )
    keys = [f"{row[0]}|{row[1]}" for row in cursor]
    storage.add_tasks(conn, STAGE, keys)
    for key in keys:
        storage.set_task(conn, STAGE, key, "pending")
    if logger:
        logger.info("重新排队 %s 个缺失时刻表的车次", len(keys))
    return len(keys)
