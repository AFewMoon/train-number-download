"""HTTP 传输层：令牌桶限速、Cookie 引导、重试退避、磁盘缓存。"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .config import HttpSettings
from .util import ensure_dir, now_iso, sha1_key

BASE = "https://kyfw.12306.cn"
STATION_JS_URL = f"{BASE}/otn/resources/js/framework/station_name.js?station_version=1.9361"
TRAIN_LIST_JS_URL = f"{BASE}/otn/resources/js/query/train_list.js"
SEARCH_URL = "https://search.12306.cn/search/v1/train/search"
TIMETABLE_URL = f"{BASE}/otn/czxx/queryByTrainNo"
INIT_URL = f"{BASE}/otn/leftTicket/init"


class FetchError(Exception):
    """请求失败（重试耗尽或响应不可用）。"""


class BlockedError(FetchError):
    """疑似被风控拦截。"""


class TokenBucket:
    """全局令牌桶：以固定速率发放令牌，保证对目标站点的请求节奏。"""

    def __init__(self, rate: float, burst: int = 1) -> None:
        self.rate = max(0.05, rate)
        self.burst = max(1, burst)
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            wait = (1.0 - self._tokens) / self.rate
            self._tokens = 0.0
            self._last = now + wait
            time.sleep(wait)
            return wait


class HttpClient:
    def __init__(self, settings: HttpSettings, cache_dir: Path, logger, base_headers: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.cache_dir = ensure_dir(cache_dir)
        self.logger = logger
        self.bucket = TokenBucket(settings.rate_per_sec, settings.burst)
        self._search_bucket = TokenBucket(settings.search_rate_per_sec, 1)
        self._bootstrap_lock = threading.Lock()
        self._bootstrapped = False
        self._blocked_streak = 0
        self._client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": INIT_URL,
                **(base_headers or {}),
            },
            timeout=settings.timeout,
            follow_redirects=True,
            http2=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str, params: dict[str, Any] | None) -> Path:
        return self.cache_dir / f"{sha1_key(url, json.dumps(params or {}, sort_keys=True))}.json"

    def _read_cache(self, path: Path) -> str | None:
        if not self.settings.cache or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("status") != 200:
            return None
        return payload.get("body")

    def _write_cache(self, path: Path, url: str, status: int, body: str) -> None:
        if not self.settings.cache:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"url": url, "status": status, "body": body, "fetched_at": now_iso()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)

    # -------------------------------------------------------------- bootstrap
    def bootstrap(self, force: bool = False) -> None:
        with self._bootstrap_lock:
            if self._bootstrapped and not force:
                return
            try:
                resp = self._client.get(INIT_URL)
                self.logger.debug("cookie bootstrap status=%s cookies=%s", resp.status_code, list(self._client.cookies.keys()))
            except httpx.HTTPError as exc:  # 引导失败不致命，后续请求仍可能成功
                self.logger.warning("cookie 引导失败: %s", exc)
            self._bootstrapped = True

    # ------------------------------------------------------------------ fetch
    def get_text(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
        use_cache: bool = True,
        allow_block_retry: bool = True,
    ) -> str:
        cache_path = self._cache_path(url, params)
        if use_cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

        self.bootstrap()
        bucket = self._bucket_for(url)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_attempts + 1):
            bucket.acquire()
            headers = {"Referer": referer} if referer else None
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                self.logger.warning("请求异常 %s attempt=%s/%s: %s", url, attempt, self.settings.max_attempts, exc)
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                body = resp.text
                if self._is_blocked_body(body):
                    last_error = BlockedError(f"blocked payload from {url}")
                    self._on_blocked(attempt)
                    continue
                self._write_cache(cache_path, url, resp.status_code, body)
                self._blocked_streak = 0
                return body

            if resp.status_code in (301, 302, 303, 307, 308) or resp.status_code in (403, 429):
                snippet = resp.text[:120].replace("\n", " ")
                last_error = BlockedError(f"HTTP {resp.status_code} from {url}: {snippet}")
                self.logger.warning("疑似风控 HTTP %s %s attempt=%s", resp.status_code, url, attempt)
                self._on_blocked(attempt)
                if allow_block_retry:
                    self.bootstrap(force=True)
                continue

            last_error = FetchError(f"HTTP {resp.status_code} from {url}")
            self.logger.warning("HTTP %s %s attempt=%s", resp.status_code, url, attempt)
            self._sleep_backoff(attempt)

        raise FetchError(str(last_error) if last_error else f"请求失败: {url}")

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
        use_cache: bool = True,
    ) -> Any:
        text = self.get_text(url, params, referer=referer, use_cache=use_cache)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"响应不是合法 JSON: {url} ({text[:120]!r})") from exc

    # ------------------------------------------------------------------ inner
    def _bucket_for(self, url: str) -> TokenBucket:
        return self._search_bucket if "search.12306.cn" in url else self.bucket

    @staticmethod
    def _is_blocked_body(body: str) -> bool:
        if not body:
            return True
        head = body[:400].lower()
        for marker in ("<html", "<!doctype html", "验证码", "captcha", "error.html"):
            if marker in head:
                return True
        return False

    def _on_blocked(self, attempt: int = 1) -> None:
        """被风控时先做单次退避，连续多次再整体冷却，避免立即重试放大风控。"""
        self._blocked_streak += 1
        if self._blocked_streak >= 5:
            pause = max(1, self.settings.pause_minutes)
            self.logger.warning("连续被拦截 %s 次，暂停 %s 分钟后继续", self._blocked_streak, pause)
            time.sleep(pause * 60)
            self._blocked_streak = 0
            return
        delay = min(60.0, 8.0 * attempt) + random.uniform(0, 3)
        self.logger.warning("风控退避 %.1fs 后重试", delay)
        time.sleep(delay)

    def _sleep_backoff(self, attempt: int) -> None:
        delay = (self.settings.backoff_base ** (attempt - 1)) * 0.5
        delay += random.uniform(0, 0.4)
        time.sleep(min(delay, 30))
