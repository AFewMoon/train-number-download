"""通用小工具：时间解析、日志、文件写入。"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_DASHES = {"", "----", "--", "—", "null", "None"}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha1_key(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_hhmm(value: str | None) -> int | None:
    """'07:18' -> 438（分钟）。占位符返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if text in _DASHES:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 48 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def fmt_hhmm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def stop_minutes(stopover_time: str | None) -> int | None:
    """'2分钟' -> 2；'----' -> None。"""
    if stopover_time is None:
        return None
    text = str(stopover_time).strip()
    if text in _DASHES:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def setup_logger(log_path: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("rail")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    ensure_dir(log_path.parent)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(stream)
    return logger
