"""影子实例配置薄封装

直接复用 shared/instance_config.py 的 parse_instance_config，
不重复实现解析逻辑（架构方案 §1.3）。

同时提供 shadow 模块内共用的 DB 连接与时间格式辅助函数（避免
main.py / export.py 各自复制一份 DATABASE_URL 解析与 _iso_or_none）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse, unquote

from shared.database import DatabaseManager
from shared.instance_config import InstanceConfig, parse_instance_config


# 影子实例固定标识（禁止散落在各模块硬编码）
_SHADOW_INSTANCE_ID = "mtcps_xau_shadow"
_SHADOW_EXECUTION_MODE = "shadow_batch"
_SHADOW_SYMBOL = "XAUUSDT"
_SHADOW_INITIAL_EQUITY_USDT = 300


@dataclass
class ShadowInstanceConfig:
    """影子实例完整运行时配置（包含 profile 列表、虚拟资金等影子专属字段）"""

    instance: InstanceConfig              # 复用 shared.InstanceConfig
    symbol: str                           # XAUUSDT
    initial_virtual_equity_usdt: float   # 300 USDT
    profiles: list                       # ["xau_original", "xau_aggressive"]
    kline_intervals: list                # ["1h", "4h", "1d"]
    cron: str                            # 调度 cron 表达式

    @property
    def instance_id(self) -> str:
        return self.instance.id

    @property
    def execution_mode(self) -> str:
        return self.instance.execution_mode


def parse_shadow_instance_config(
    config: Dict[str, Any],
    *,
    default_cron: str = "15 * * * *",
    default_profiles: list | None = None,
) -> ShadowInstanceConfig:
    """解析影子实例配置

    Args:
        config: profile 的完整 YAML 加载结果（顶层含 instance/strategy/risk/...）
        default_cron: 调度 cron 默认值（与生产错峰，默认 15 分）
        default_profiles: 默认 profile 列表（缺省两版都启）

    Returns:
        组装好的 ShadowInstanceConfig
    """
    # 复用共享解析器（影子实例强制 shadow_batch 会被 parse_instance_config
    # 自动将 real_ordering_enabled/state_reporting_enabled 置 false）
    instance = parse_instance_config(
        config,
        default_id=_SHADOW_INSTANCE_ID,
        default_execution_mode=_SHADOW_EXECUTION_MODE,
        default_capital_strategy_id="",         # 影子不参与资金分配
        default_circuit_breaker_pool="",         # 影子不参与组合熔断
        default_state_reporting_enabled=False,   # 影子不上报状态
        default_real_ordering_enabled=False,     # 影子禁止下单
    )

    symbol = (
        os.getenv("SHADOW_SYMBOL")
        or config.get("instance", {}).get("symbol")
        or _SHADOW_SYMBOL
    )
    initial_equity = float(
        os.getenv("SHADOW_INITIAL_VIRTUAL_EQUITY_USDT")
        or config.get("instance", {}).get("initial_virtual_equity_usdt")
        or _SHADOW_INITIAL_EQUITY_USDT
    )
    profiles = (
        os.getenv("SHADOW_PROFILES", "").split(",")
        if os.getenv("SHADOW_PROFILES")
        else config.get("instance", {}).get("profiles")
        or default_profiles
        or ["xau_original", "xau_aggressive"]
    )
    kline_intervals = (
        os.getenv("SHADOW_KLINE_INTERVALS", "").split(",")
        if os.getenv("SHADOW_KLINE_INTERVALS")
        else config.get("shadow", {}).get("kline_intervals")
        or ["1h", "4h", "1d"]
    )
    cron = (
        os.getenv("SHADOW_CRON")
        or config.get("shadow", {}).get("cron")
        or default_cron
    )

    return ShadowInstanceConfig(
        instance=instance,
        symbol=symbol,
        initial_virtual_equity_usdt=initial_equity,
        profiles=profiles,
        kline_intervals=kline_intervals,
        cron=cron,
    )


# ---------------------------------------------------------------------------
# 共用 DB 连接与时间格式工具（避免 main.py / export.py 各自复制一份）
# ---------------------------------------------------------------------------
def connect_db_from_env() -> DatabaseManager:
    """从 DATABASE_URL 环境变量构造 DatabaseManager。

    期望格式：postgres://user:password@host:port/dbname?schema=research。
    缺 env 直接 SystemExit（禁止静默回退 localhost 默认值）。
    """
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise SystemExit(
            "DATABASE_URL 环境变量未设置。"
            "期望格式: postgres://user:password@host:port/dbname"
        )
    parsed = urlparse(db_url)
    return DatabaseManager(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/") or "trading",
        user=parsed.username or "postgres",
        password=unquote(parsed.password or ""),
    )


def iso_or_none(val: Any) -> Optional[str]:
    """把 datetime / None / 其他值转成 ISO 字符串（UTC aware）。

    pg 返回的 datetime 可能 naive，这里统一转 UTC。
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc).isoformat()
    return str(val)
