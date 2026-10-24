"""纯函数解析层：12306 各类响应 -> 结构化数据（可离线单测）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import parse_hhmm, stop_minutes

_CODE_RE = re.compile(r"^([A-Za-z]*)(\d+)")
_ROUTE_RE = re.compile(r"^([^(（]+)[（(]([^)）]+)[)）]$")


# ------------------------------------------------------------------ stations
def parse_station_js(text: str) -> list[dict[str, Any]]:
    """解析 station_name.js：

    @bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||
    索引：0 简拼(带@) 1 站名 2 电报码 3 全拼 4 首字母 5 序号 6 城市代码 7 城市名
    """
    if "station_names" not in text:
        raise ValueError("响应不是 station_name.js")
    raw = text.split("'", 2)
    body = raw[1] if len(raw) >= 3 else text
    stations: list[dict[str, Any]] = []
    for chunk in body.split("@"):
        if not chunk.strip():
            continue
        fields = chunk.split("|")
        if len(fields) < 3:
            continue
        name, telecode = fields[1].strip(), fields[2].strip().upper()
        if not name or not telecode:
            continue
        stations.append(
            {
                "short_pinyin": fields[0].strip(),
                "name": name,
                "telecode": telecode,
                "pinyin": fields[3].strip() if len(fields) > 3 else None,
                "order_no": _to_int(fields[5]) if len(fields) > 5 else None,
                "city_code": fields[6].strip() if len(fields) > 6 else None,
                "city_name": fields[7].strip() if len(fields) > 7 else None,
            }
        )
    return stations


# ---------------------------------------------------------------- train list
def parse_train_list_js(text: str) -> list[dict[str, Any]]:
    """解析 resources/js/query/train_list.js（该文件为 2022 年静态快照，仅作交叉校验用）。"""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("响应不是 train_list.js")
    payload = json.loads(text[start : end + 1])
    results: list[dict[str, Any]] = []
    for date, groups in payload.items():
        if not isinstance(groups, dict):
            continue
        for prefix, items in groups.items():
            for item in items or []:
                if isinstance(item, str):
                    code, train_no, origin, terminal = item, None, None, None
                else:
                    code = item.get("station_train_code") or ""
                    train_no = item.get("train_no")
                    origin = item.get("start_station_name")
                    terminal = item.get("end_station_name")
                origin, terminal = _split_route(code, origin, terminal)
                results.append(
                    {
                        "date": date,
                        "prefix": prefix,
                        "station_train_code": _split_code(code)[0],
                        "train_no": train_no,
                        "start_station": origin,
                        "end_station": terminal,
                    }
                )
    return results


# ------------------------------------------------------------------- search
def parse_search_payload(payload: Any) -> list[dict[str, Any]]:
    """解析 search/v1/train/search 响应。"""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = (item.get("station_train_code") or "").strip()
        if not code:
            continue
        results.append(
            {
                "date": item.get("date"),
                "station_train_code": code,
                "train_no": (item.get("train_no") or "").strip() or None,
                "start_station": (item.get("from_station") or "").strip() or None,
                "end_station": (item.get("to_station") or "").strip() or None,
                "station_count": _to_int(item.get("total_num")),
            }
        )
    return results


# --------------------------------------------------------------- timetable
@dataclass(slots=True)
class TimetableStop:
    station_no: int
    station_name: str
    arrive_time: str | None
    depart_time: str | None
    stop_minutes: int | None
    day_offset: int = 0
    depart_day_offset: int = 0
    is_start: bool = False
    is_end: bool = False


@dataclass(slots=True)
class Timetable:
    train_class: str | None
    stops: list[TimetableStop] = field(default_factory=list)
    start_station: str | None = None
    end_station: str | None = None
    duration_min: int | None = None


def parse_timetable_payload(payload: Any) -> Timetable:
    """解析 czxx/queryByTrainNo 响应，并推算跨天偏移与全程历时。"""
    if isinstance(payload, dict) and payload.get("status") is False:
        raise ValueError(str(payload.get("messages") or "接口返回失败"))
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("时刻表为空")

    stops: list[TimetableStop] = []
    train_class = None
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        arrive = _clean_time(row.get("arrive_time"))
        depart = _clean_time(row.get("start_time"))
        station_no = _to_int(row.get("station_no")) or index
        if train_class is None:
            train_class = (row.get("train_class_name") or "").strip() or None
        stops.append(
            TimetableStop(
                station_no=station_no,
                station_name=(row.get("station_name") or "").strip(),
                arrive_time=arrive,
                depart_time=depart,
                stop_minutes=stop_minutes(row.get("stopover_time")),
            )
        )

    stops.sort(key=lambda s: s.station_no)
    if not stops:
        raise ValueError("时刻表无可解析站点")

    stops[0].is_start = True
    stops[-1].is_end = True

    # 终到站不存在开车行为：12306 会在这行回填「到达时刻」甚至别的车次的时刻，统一清空，
    # 避免导出里出现「终到站开车时刻早于/晚于到达」的伪数据。
    last = stops[-1]
    last.depart_time = None
    last.stop_minutes = None

    # 以「绝对分钟」推算跨天：到/发时刻必须不早于上一站开车时刻，必要时 +1440。
    prev_abs: int | None = None
    for stop in stops:
        arrive_min = parse_hhmm(stop.arrive_time)
        depart_min = parse_hhmm(stop.depart_time)
        if arrive_min is not None:
            abs_arrive = arrive_min
            while prev_abs is not None and abs_arrive < prev_abs:
                abs_arrive += 1440
            stop.day_offset = abs_arrive // 1440
            prev_abs = abs_arrive
        if depart_min is not None:
            base = prev_abs if prev_abs is not None else depart_min
            abs_depart = depart_min
            while abs_depart < base:
                abs_depart += 1440
            stop.depart_day_offset = abs_depart // 1440
            prev_abs = abs_depart

    duration = _duration(stops)
    return Timetable(
        train_class=train_class,
        stops=stops,
        start_station=stops[0].station_name,
        end_station=stops[-1].station_name,
        duration_min=duration,
    )


def _duration(stops: list[TimetableStop]) -> int | None:
    first = stops[0]
    last = stops[-1]
    start_min = parse_hhmm(first.depart_time)
    end_min = parse_hhmm(last.arrive_time) or parse_hhmm(last.depart_time)
    if start_min is None or end_min is None:
        return None
    total = end_min + last.day_offset * 1440 - start_min
    return total if total > 0 else None


# -------------------------------------------------------------------- utils
def _clean_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"----", "--"}:
        return None
    return text


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _split_code(value: str) -> tuple[str, str | None]:
    text = (value or "").strip()
    match = _ROUTE_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text, None


def _split_route(code: str, origin: str | None, terminal: str | None) -> tuple[str | None, str | None]:
    _, route = _split_code(code)
    if route and "-" in route:
        parts = route.split("-", 1)
        return parts[0].strip(), parts[1].strip()
    return origin, terminal


def code_prefix(code: str) -> str:
    """'G1234' -> 'G'；'6025' -> ''。"""
    match = _CODE_RE.match(code or "")
    return match.group(1).upper() if match else ""


def build_train_row(
    hit: dict[str, Any],
    timetable: Timetable,
    station_telecodes: dict[str, str],
    train_date: str,
) -> dict[str, Any]:
    stops = timetable.stops
    start_name = timetable.start_station or hit.get("start_station")
    end_name = timetable.end_station or hit.get("end_station")
    return {
        "train_no": hit["train_no"],
        "station_train_code": hit["station_train_code"],
        "train_date": train_date,
        "train_class": timetable.train_class,
        "start_station": start_name,
        "end_station": end_name,
        "start_telecode": station_telecodes.get(start_name or ""),
        "end_telecode": station_telecodes.get(end_name or ""),
        "depart_time": stops[0].depart_time,
        "arrive_time": stops[-1].arrive_time,
        "duration_min": timetable.duration_min,
        "station_count": len(stops),
        "total_mileage_km": None,
        "source": "12306:czxx",
    }


def build_stop_rows(
    hit: dict[str, Any],
    timetable: Timetable,
    station_telecodes: dict[str, str],
) -> list[dict[str, Any]]:
    rows = []
    for stop in timetable.stops:
        rows.append(
            {
                "train_no": hit["train_no"],
                "station_train_code": hit["station_train_code"],
                "station_no": stop.station_no,
                "station_name": stop.station_name,
                "station_telecode": station_telecodes.get(stop.station_name),
                "arrive_time": stop.arrive_time,
                "depart_time": stop.depart_time,
                "stop_minutes": stop.stop_minutes,
                "day_offset": stop.day_offset,
                "depart_day_offset": stop.depart_day_offset,
                "mileage_km": None,
                "platform": None,
                "track": None,
                "is_start": 1 if stop.is_start else 0,
                "is_end": 1 if stop.is_end else 0,
                "source": "12306:czxx",
            }
        )
    return rows


def iter_rows(rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    return (row for row in rows if row)
