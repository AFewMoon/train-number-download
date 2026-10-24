"""高铁网（www.gaotie.com.cn）车次页解析：补全程/逐站里程、承运车型与担当路局。

页面是服务端渲染的 HTML，正文里成段出现这些事实：

    G1 次高速动车组列车(高铁)从 北京南站到上海虹桥站 … 全程距离 1318公里 …
    此车次承运车型为 复兴号(CR400BF-B型) ，… 由中国铁路上海局集团有限公司担当运营服务的 …
    第 2 站 沧州西站 到站时间(07:18) - 停留2分钟 - 发车时间(07:20) 里程(210km) - 历时(48分) …

解析思路：按「第 N 站」切段，段内取 `里程(xxkm)`，用**站名**作为键输出，
再由入库层按站名对齐 12306 的站序（页面站号与 12306 站号口径可能不同，不直接采用）。

注意：`www.gaotie.cn` 已停用（首页都是 nginx 404），本 provider 走 `.com.cn`。
"""

from __future__ import annotations

import re
import time

import httpx

from ..config import Settings
from . import Enrichment, ProviderUnavailable

BASE_URL = "https://www.gaotie.com.cn"
CONFIDENCE = 0.8

_TOTAL_RE = re.compile(r"全程距离\s*([0-9]+(?:\.[0-9]+)?)\s*(?:公里|km)")
_MODEL_RE = re.compile(r"承运车型为\s*([^，,。；;]{1,40})")
_OPERATOR_RE = re.compile(
    r"由中国铁路\s*([\u4e00-\u9fa5]{2,14}?局(?:集团有限公司)?)\s*(?:担当|运营|管辖)"
)
_STOP_SPLIT_RE = re.compile(r"第\s*([0-9]{1,3})\s*站")
_MILEAGE_RE = re.compile(r"里程\s*[（(]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:km|公里)\s*[)）]", re.I)
_CR_RE = re.compile(r"CR[0-9A-Z]{2,}[A-Z0-9\-]*")
_NAME_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9·]{2,12}?)(?:站)?(?=\s|到站|始发|发车|$)")


class GaotieComProvider:
    name = "gaotie_com"

    def __init__(self, settings: Settings, logger) -> None:
        self.logger = logger
        self.delay = 1.0 / max(0.05, settings.enrich.provider_rate_per_sec)
        self._last = 0.0
        self._client = httpx.Client(
            headers={
                "User-Agent": settings.http.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{BASE_URL}/",
            },
            timeout=settings.enrich.provider_timeout,
            follow_redirects=True,
            proxy=settings.http.proxy or None,
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, code: str, train_no: str) -> Enrichment | None:
        url = f"{BASE_URL}/checi/{code}.html"
        self._pace()
        try:
            resp = self._client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"请求失败: {exc}") from exc
        if resp.status_code in (403, 429, 503):
            raise ProviderUnavailable(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            return None
        return parse_checi_page(resp.text, code)

    def _pace(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


def parse_checi_page(html: str, code: str = "") -> Enrichment | None:
    text = plain_text(html)
    if len(text) < 200 or "404 Not Found" in text[:200]:
        return None

    total = None
    match = _TOTAL_RE.search(text)
    if match:
        total = float(match.group(1))

    model = _parse_model(text)
    operator = _parse_operator(text)
    mileage_by_name = parse_station_mileages(text)

    if total is None and model is None and not mileage_by_name:
        return None
    return Enrichment(
        source="thirdparty:gaotie_com",
        confidence=CONFIDENCE,
        train_type=model,
        operator=operator,
        total_mileage_km=total,
        stop_mileage_by_name=mileage_by_name,
    )


def parse_station_mileages(text: str) -> dict[str, float]:
    """返回「站名 -> 累计里程(km)」。页面首末年站通常无里程，此处只返回有值的站。"""
    marks = list(_STOP_SPLIT_RE.finditer(text))
    result: dict[str, float] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        segment = text[mark.end() : end]
        mileage = _MILEAGE_RE.search(segment)
        if not mileage:
            continue
        name = _NAME_RE.match(segment)
        if not name:
            continue
        station = normalize_station(name.group(1))
        if station:
            result[station] = float(mileage.group(1))
    return result


def _parse_model(text: str) -> str | None:
    match = _MODEL_RE.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    cr = _CR_RE.search(raw.upper())
    if cr:
        return cr.group(0).replace("型", "").strip("-")
    return raw or None


def _parse_operator(text: str) -> str | None:
    match = _OPERATOR_RE.search(text)
    if not match:
        return None
    label = match.group(1).strip()
    if not label.endswith("公司"):
        label = f"{label}有限公司" if label.endswith("集团") else label.replace("局", "局集团有限公司")
    return label


def normalize_station(name: str | None) -> str:
    value = (name or "").strip()
    if value.endswith("站") and len(value) > 1:
        value = value[:-1]
    return value


def plain_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)
