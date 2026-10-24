"""接口体检：实测各数据源可用性与响应形态，输出 docs/api-probe.md。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Settings
from .http import (
    INIT_URL,
    SEARCH_URL,
    STATION_JS_URL,
    TIMETABLE_URL,
    TRAIN_LIST_JS_URL,
    HttpClient,
)
from .parsers import parse_search_payload, parse_station_js, parse_timetable_payload, parse_train_list_js
from .util import now_iso


def run_probe(settings: Settings, client: HttpClient, logger) -> dict[str, Any]:
    results: dict[str, Any] = {"checked_at": now_iso(), "date": settings.date, "checks": []}

    def record(name: str, ok: bool, detail: str, sample: str = "") -> None:
        results["checks"].append({"name": name, "ok": ok, "detail": detail, "sample": sample[:400]})
        logger.info("[%s] %s - %s", "OK" if ok else "FAIL", name, detail)

    try:
        text = client.get_text(STATION_JS_URL, referer=INIT_URL)
        stations = parse_station_js(text)
        record("station_name.js", bool(stations), f"解析车站 {len(stations)} 个", text[:200])
    except Exception as exc:
        record("station_name.js", False, f"{type(exc).__name__}: {exc}")

    hits: list[dict[str, Any]] = []
    try:
        payload = client.get_json(
            SEARCH_URL,
            {"keyword": "G1", "date": settings.date.replace("-", "")},
            referer="https://www.12306.cn/index/",
        )
        hits = parse_search_payload(payload)
        record("search/v1/train/search", bool(hits), f"keyword=G1 命中 {len(hits)} 条", json.dumps(hits[:2], ensure_ascii=False))
    except Exception as exc:
        record("search/v1/train/search", False, f"{type(exc).__name__}: {exc}")

    try:
        legacy = client.get_text(TRAIN_LIST_JS_URL, referer=INIT_URL)
        rows = parse_train_list_js(legacy)
        dates = sorted({row["date"] for row in rows})
        record(
            "train_list.js（历史快照）",
            bool(rows),
            f"快照日期 {dates}，共 {len(rows)} 条（已冻结，仅作交叉校验）",
            legacy[:200],
        )
    except Exception as exc:
        record("train_list.js（历史快照）", False, f"{type(exc).__name__}: {exc}")

    if hits:
        sample = next((hit for hit in hits if hit.get("train_no")), None)
        if sample:
            try:
                payload = client.get_json(
                    TIMETABLE_URL,
                    {
                        "train_no": sample["train_no"],
                        "from_station_telecode": "",
                        "to_station_telecode": "",
                        "depart_date": settings.date,
                    },
                    referer=INIT_URL,
                )
                timetable = parse_timetable_payload(payload)
                record(
                    "czxx/queryByTrainNo",
                    len(timetable.stops) >= 2,
                    f"{sample['station_train_code']} 解析 {len(timetable.stops)} 站，历时 {timetable.duration_min} 分钟",
                    json.dumps(
                        {
                            "train_class": timetable.train_class,
                            "start": timetable.start_station,
                            "end": timetable.end_station,
                            "first": asdict(timetable.stops[0]),
                            "last": asdict(timetable.stops[-1]),
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception as exc:
                record("czxx/queryByTrainNo", False, f"{type(exc).__name__}: {exc}")

    for provider in settings.enrich.providers:
        if provider.lower() != "gaotie":
            continue
        try:
            resp = client._client.get(  # noqa: SLF001 - 探针需要原始状态码
                "https://www.gaotie.cn/checi/G1/",
                headers={"Referer": "https://www.gaotie.cn/"},
            )
            record(
                "thirdparty:gaotie",
                resp.status_code == 200,
                f"HTTP {resp.status_code}，响应长度 {len(resp.text)}",
                resp.text[:200],
            )
        except Exception as exc:
            record("thirdparty:gaotie", False, f"{type(exc).__name__}: {exc}")

    return results


def write_probe_doc(settings: Settings, results: dict[str, Any]) -> Path:
    path = settings.docs_dir / "api-probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 12306 接口体检结果",
        "",
        f"- 检查时间：{results['checked_at']}",
        f"- 数据日期：{results['date']}",
        "",
        "## 结论",
        "",
        "| 接口 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for check in results["checks"]:
        lines.append(f"| {check['name']} | {'可用' if check['ok'] else '不可用'} | {check['detail']} |")

    lines += [
        "",
        "## 抓取口径说明",
        "",
        "1. **车次枚举**：`train_list.js` 已冻结在 2022-08-29，不能作为当前图定车次来源；"
        "改用 `search/v1/train/search` 按前缀递归细分（单次上限 200 条），得到当前在售车次。",
        "2. **时刻表**：`otn/czxx/queryByTrainNo` 只需 `train_no` 与日期，"
        "`from_station_telecode` / `to_station_telecode` 可留空，返回全部经停站的到发时刻与等级。",
        "3. **里程 / 站台**：12306 不提供，只能依赖第三方页面；第三方不可达时字段留空并在报告中计入覆盖率，不做推测。",
        "4. **Cookie**：访问 `otn/leftTicket/init` 获取 JSESSIONID 后即可查询时刻表；不需要登录。",
        "",
        "## 响应样本（截断）",
        "",
    ]
    for check in results["checks"]:
        if check.get("sample"):
            lines += [f"### {check['name']}", "", "```", check["sample"], "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
