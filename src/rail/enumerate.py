"""车次枚举：用 12306 车次搜索接口按前缀递归细分，得到当前图定全量车次。

search/v1/train/search 单次最多返回 200 条且按车次号排序，因此某个前缀命中 200 条时
视为被截断，继续用「前缀 + 0~9」细分，直到每个分支都不足 200 条或达到最大前缀长度。

前缀队列本身也写进 crawl_tasks(stage='enumerate')，因此被风控中断后重跑会从断点继续。
"""

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Any, Iterable

from . import storage
from .config import Settings
from .http import SEARCH_URL, HttpClient
from .parsers import parse_search_payload
from .util import now_iso

BATCH = 200
STAGE = "enumerate"
# 连续这么多个前缀都失败（通常是被风控），就停下等待下次续跑，避免持续触发风控
MAX_CONSECUTIVE_FAILURES = 3


def search_prefix(client: HttpClient, prefix: str, date: str) -> list[dict[str, Any]]:
    payload = client.get_json(
        SEARCH_URL,
        {"keyword": prefix, "date": date.replace("-", "")},
        referer="https://www.12306.cn/index/",
    )
    return parse_search_payload(payload)


def _row(hit: dict[str, Any], train_date: str) -> dict[str, Any]:
    return {
        "train_no": hit["train_no"],
        "station_train_code": hit["station_train_code"],
        "train_date": train_date,
        "train_class": None,
        "start_station": hit.get("start_station"),
        "end_station": hit.get("end_station"),
        "start_telecode": None,
        "end_telecode": None,
        "depart_time": None,
        "arrive_time": None,
        "duration_min": None,
        "station_count": hit.get("station_count"),
        "total_mileage_km": None,
        "source": "12306:search",
    }


def _seed_queue(
    conn: sqlite3.Connection, roots: Iterable[str], retry_failed: bool, logger=None
) -> deque[str]:
    counts = storage.count_tasks(conn, STAGE)
    if counts:
        statuses = ["pending"] + (["failed"] if retry_failed else [])
        keys = [row["task_key"] for row in storage.iter_tasks(conn, STAGE, statuses)]
        if logger:
            logger.info("枚举续跑：待处理前缀 %s 个（历史状态 %s）", len(keys), counts)
        return deque(keys)

    seed = sorted({x.upper() for x in roots})
    storage.add_tasks(conn, STAGE, seed)
    return deque(seed)


def enumerate_trains(
    client: HttpClient,
    settings: Settings,
    conn: sqlite3.Connection,
    roots: Iterable[str] | None = None,
    limit: int | None = None,
    logger=None,
    retry_failed: bool = False,
) -> dict[str, int]:
    cap = settings.crawl.search_cap
    max_len = settings.crawl.max_prefix_len
    queue = _seed_queue(conn, roots or settings.crawl.roots, retry_failed, logger)

    taken: dict[tuple[str, str], dict[str, Any]] = {}
    requests = 0
    truncated_prefixes = 0
    failed_prefixes = 0
    consecutive_failures = 0

    while queue:
        if limit and len(taken) >= limit:
            break
        prefix = queue.popleft()
        if len(prefix) > max_len:
            storage.set_task(conn, STAGE, prefix, "skipped", "前缀过长")
            continue

        try:
            hits = search_prefix(client, prefix, settings.date)
        except Exception as exc:  # 单个前缀失败不影响其他分支
            failed_prefixes += 1
            consecutive_failures += 1
            storage.set_task(conn, STAGE, prefix, "failed", str(exc)[:300], bump=True)
            if logger:
                logger.warning("枚举前缀 %s 失败(%s/%s): %s", prefix, consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                if logger:
                    logger.error("连续 %s 个前缀失败，疑似被风控；保留剩余 %s 个前缀待续跑", consecutive_failures, len(queue))
                break
            continue

        requests += 1
        consecutive_failures = 0

        rows = []
        for raw in hits:
            if not raw.get("train_no") or not raw.get("station_train_code"):
                continue
            key = (raw["train_no"], raw["station_train_code"])
            if key in taken:
                continue
            taken[key] = raw
            rows.append(_row(raw, settings.date))

        if rows:
            for start in range(0, len(rows), BATCH):
                storage.upsert_trains(conn, rows[start : start + BATCH])
            storage.add_tasks(conn, "timetable", [f"{r['train_no']}|{r['station_train_code']}" for r in rows])

        storage.set_task(conn, STAGE, prefix, "done", f"hits={len(hits)}")

        # 命中上限说明被截断，继续细分
        if len(hits) >= cap:
            truncated_prefixes += 1
            for digit in "0123456789":
                child = f"{prefix}{digit}"
                if len(child) <= max_len and child not in queue:
                    storage.add_tasks(conn, STAGE, [child])
                    queue.append(child)

        if logger and requests % 20 == 0:
            logger.info(
                "枚举进度 prefix=%s 请求=%s 车次=%s 待细分=%s",
                prefix, requests, len(taken), len(queue),
            )

    storage.set_meta(conn, "enumerated_date", settings.date)
    storage.set_meta(conn, "enumerated_at", now_iso())

    stats = {
        "trains": len(taken),
        "requests": requests,
        "truncated_prefixes": truncated_prefixes,
        "failed_prefixes": failed_prefixes,
        "pending_prefixes": len(queue),
    }
    if logger:
        logger.info(
            "枚举结束：本次新增车次 %s，请求 %s 次，截断细分 %s 个，失败 %s 个，剩余待续跑 %s 个",
            stats["trains"], stats["requests"], stats["truncated_prefixes"],
            stats["failed_prefixes"], stats["pending_prefixes"],
        )
    return stats
