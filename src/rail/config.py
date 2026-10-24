"""配置加载：把 config/settings.yaml 解析成强类型对象。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import today_str

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def project_root() -> Path:
    """仓库根目录（src/rail/config.py 的上三级）。"""
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class HttpSettings:
    rate_per_sec: float = 1.0
    burst: int = 1
    # search.12306.cn 风控远比 kyfw 严格，单独用更慢的令牌桶
    search_rate_per_sec: float = 0.35
    workers: int = 2
    timeout: float = 25.0
    max_attempts: int = 5
    backoff_base: float = 1.6
    pause_minutes: int = 5
    cache: bool = True
    user_agent: str = DEFAULT_UA
    # 可选 HTTP 代理（如 http://127.0.0.1:7890）；12306 直连即可，第三方站点常需要它
    proxy: str | None = None


@dataclass(slots=True)
class CrawlSettings:
    roots: list[str] = field(default_factory=lambda: ["G", "D", "C", "Z", "T", "K"])
    search_cap: int = 200
    max_prefix_len: int = 5


@dataclass(slots=True)
class EnrichSettings:
    providers: list[str] = field(default_factory=lambda: ["gaotie"])
    provider_rate_per_sec: float = 0.8
    provider_timeout: float = 20.0
    enable_class_rules: bool = True


@dataclass(slots=True)
class Settings:
    root: Path
    date: str
    http: HttpSettings
    crawl: CrawlSettings
    enrich: EnrichSettings
    db_path: Path
    cache_dir: Path
    raw_dir: Path
    csv_dir: Path
    log_path: Path
    data_dir: Path
    docs_dir: Path

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.cache_dir, self.raw_dir, self.csv_dir, self.log_path.parent):
            path.mkdir(parents=True, exist_ok=True)


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def load_settings(config_path: Path | None = None, date: str | None = None) -> Settings:
    root = project_root()
    path = config_path or root / "config" / "settings.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = _as_dict(yaml.safe_load(path.read_text(encoding="utf-8")))

    http_raw = _as_dict(raw.get("http"))
    crawl_raw = _as_dict(raw.get("crawl"))
    enrich_raw = _as_dict(raw.get("enrich"))
    paths_raw = _as_dict(raw.get("paths"))

    def resolve(key: str, default: str) -> Path:
        value = paths_raw.get(key) or default
        candidate = Path(value)
        return candidate if candidate.is_absolute() else root / candidate

    http = HttpSettings(
        rate_per_sec=float(http_raw.get("rate_per_sec", 1.0)),
        burst=int(http_raw.get("burst", 1)),
        search_rate_per_sec=float(http_raw.get("search_rate_per_sec", 0.35)),
        workers=max(1, int(http_raw.get("workers", 2))),
        timeout=float(http_raw.get("timeout", 25.0)),
        max_attempts=max(1, int(http_raw.get("max_attempts", 5))),
        backoff_base=float(http_raw.get("backoff_base", 1.6)),
        pause_minutes=int(http_raw.get("pause_minutes", 5)),
        cache=bool(http_raw.get("cache", True)),
        user_agent=str(http_raw.get("user_agent") or DEFAULT_UA),
        proxy=(http_raw.get("proxy") or os.environ.get("RAIL_PROXY") or None),
    )

    crawl = CrawlSettings(
        roots=[str(x) for x in (crawl_raw.get("roots") or ["G", "D", "C", "Z", "T", "K"])],
        search_cap=int(crawl_raw.get("search_cap", 200)),
        max_prefix_len=int(crawl_raw.get("max_prefix_len", 5)),
    )

    enrich = EnrichSettings(
        providers=[str(x) for x in (enrich_raw.get("providers") or [])],
        provider_rate_per_sec=float(enrich_raw.get("provider_rate_per_sec", 0.8)),
        provider_timeout=float(enrich_raw.get("provider_timeout", 20.0)),
        enable_class_rules=bool(enrich_raw.get("enable_class_rules", True)),
    )

    resolved_date = date or raw.get("date") or today_str()

    return Settings(
        root=root,
        date=str(resolved_date),
        http=http,
        crawl=crawl,
        enrich=enrich,
        db_path=resolve("db", "output/rail.db"),
        cache_dir=resolve("cache", "cache/http"),
        raw_dir=resolve("raw", "output/raw"),
        csv_dir=resolve("csv", "output/csv"),
        log_path=resolve("log", "logs/rail.log"),
        data_dir=root / "data",
        docs_dir=root / "docs",
    )
