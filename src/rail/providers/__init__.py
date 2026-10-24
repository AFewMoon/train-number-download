"""第三方补全 provider 接口与注册表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..config import Settings


class ProviderUnavailable(Exception):
    """provider 不可用（站点拒绝、结构变化等），上层应跳过且不计入失败。"""


@dataclass(slots=True)
class StopExtra:
    station_no: int
    mileage_km: float | None = None
    platform: str | None = None
    track: str | None = None


@dataclass(slots=True)
class Enrichment:
    source: str
    confidence: float
    train_type: str | None = None
    operator: str | None = None
    total_mileage_km: float | None = None
    stops: dict[int, StopExtra] = field(default_factory=dict)
    # 站名 -> 里程；入库层按站名对齐，避免第三方站号与 12306 站号口径不一致
    stop_mileage_by_name: dict[str, float] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def fetch(self, code: str, train_no: str) -> Enrichment | None: ...

    def close(self) -> None: ...


def build_providers(settings: Settings, logger) -> list[Provider]:
    providers: list[Provider] = []
    for name in settings.enrich.providers:
        key = name.strip().lower()
        if key in ("gaotie_com", "gaotie.com.cn", "gaotie"):
            # 旧域名 gaotie.cn 已停用（首页返回 nginx 404），统一走 gaotie.com.cn 实现
            from .gaotie_com import GaotieComProvider

            providers.append(GaotieComProvider(settings, logger))
        else:
            logger.warning("未知 provider：%s（已忽略）", name)
    return providers
