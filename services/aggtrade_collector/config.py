"""aggTrades 落库器配置加载与校验（计划 §1.1~§1.3，禁硬编码）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# 仓库根：services/aggtrade_collector/config.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


@dataclass(frozen=True)
class CollectorConfig:
    """落库器配置（路径均已解析为绝对路径）。"""

    symbol: str
    base_url: str
    agg_trades_path: str
    timeout_seconds: float
    page_limit: int
    slice_hours: int
    max_retries: int
    backoff_base_seconds: float
    page_sleep_min_seconds: float
    page_sleep_max_seconds: float
    rate_limit_wait_seconds: float
    rate_limit_max_wait_seconds: float
    db_path: Path
    agg_trades_retention_hours: int
    busy_timeout_ms: int
    insert_batch_size: int
    lookback_hours: int
    daemon_incremental_interval_seconds: float
    daemon_cleanup_interval_seconds: float
    sample_path: Path
    sample_sha256: str
    manifest_path: Path
    validation_start_beijing: str
    validation_min_full_days: int
    base_dir: Path


def resolve_base_dir(cli_base_dir: Optional[str] = None) -> Path:
    """基准目录解析：CLI > 环境变量 AGGTRADE_BASE_DIR > 仓库根（不按 cwd 猜测）。"""
    raw = cli_base_dir or os.environ.get("AGGTRADE_BASE_DIR")
    return Path(raw).resolve() if raw else _REPO_ROOT


def _resolve_path(value: str, base_dir: Path) -> Path:
    """相对路径相对基准目录解析，绝对路径原样返回。"""
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _require_positive(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"配置项 {name} 必须为正数，实际: {value!r}")
    return float(value)


def _validate(raw: Dict[str, Any]) -> None:
    """按计划口径校验配置，缺项/非法直接报错，不静默回退。"""
    rest, storage = raw["rest"], raw["storage"]
    backfill, sample = raw["backfill"], raw["sample_file"]
    validation, daemon = raw["validation_segment"], raw["daemon"]
    _require_positive("rest.timeout_seconds", rest["timeout_seconds"])
    _require_positive("rest.max_retries", rest["max_retries"])
    _require_positive("rest.backoff_base_seconds", rest["backoff_base_seconds"])
    _require_positive("rest.page_sleep_min_seconds", rest["page_sleep_min_seconds"])
    _require_positive("rest.page_sleep_max_seconds", rest["page_sleep_max_seconds"])
    if rest["page_sleep_min_seconds"] > rest["page_sleep_max_seconds"]:
        raise ValueError("rest.page_sleep_min_seconds 不得大于 page_sleep_max_seconds")
    _require_positive("rest.rate_limit_wait_seconds", rest["rate_limit_wait_seconds"])
    _require_positive("rest.rate_limit_max_wait_seconds",
                      rest["rate_limit_max_wait_seconds"])
    if rest["rate_limit_wait_seconds"] > rest["rate_limit_max_wait_seconds"]:
        raise ValueError(
            "rest.rate_limit_wait_seconds 不得大于 rate_limit_max_wait_seconds")
    if not 1 <= rest["page_limit"] <= 1000:
        raise ValueError("rest.page_limit 必须在 1~1000（币安硬约束）")
    if rest["slice_hours"] != 1:
        raise ValueError("rest.slice_hours 必须为 1（startTime+endTime 窗口 ≤1h 硬约束）")
    _require_positive("storage.agg_trades_retention_hours",
                      storage["agg_trades_retention_hours"])
    _require_positive("storage.busy_timeout_ms", storage["busy_timeout_ms"])
    _require_positive("storage.insert_batch_size", storage["insert_batch_size"])
    _require_positive("backfill.lookback_hours", backfill["lookback_hours"])
    _require_positive("daemon.incremental_interval_seconds",
                      daemon["incremental_interval_seconds"])
    _require_positive("daemon.cleanup_interval_seconds",
                      daemon["cleanup_interval_seconds"])
    if daemon["cleanup_interval_seconds"] < daemon["incremental_interval_seconds"]:
        raise ValueError(
            "daemon.cleanup_interval_seconds 不得小于 incremental_interval_seconds")
    if not str(raw.get("symbol", "")).strip():
        raise ValueError("symbol 不能为空")
    if not sample.get("sha256"):
        raise ValueError("sample_file.sha256 不能为空")
    if not str(validation.get("start_rule_beijing", "")).strip():
        raise ValueError("validation_segment.start_rule_beijing 不能为空")
    _require_positive("validation_segment.min_full_days",
                      validation["min_full_days"])


def load_config(config_path: Optional[str] = None,
                base_dir: Optional[str] = None) -> CollectorConfig:
    """加载 YAML 配置并解析为 CollectorConfig。"""
    path = Path(config_path).resolve() if config_path else _DEFAULT_CONFIG
    resolved_base = resolve_base_dir(base_dir)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    _validate(raw)
    rest, storage = raw["rest"], raw["storage"]
    backfill, sample, manifest, validation = (
        raw["backfill"], raw["sample_file"], raw["manifest"],
        raw["validation_segment"])
    daemon = raw["daemon"]
    return CollectorConfig(
        symbol=raw["symbol"],
        base_url=rest["base_url"],
        agg_trades_path=rest["agg_trades_path"],
        timeout_seconds=float(rest["timeout_seconds"]),
        page_limit=int(rest["page_limit"]),
        slice_hours=int(rest["slice_hours"]),
        max_retries=int(rest["max_retries"]),
        backoff_base_seconds=float(rest["backoff_base_seconds"]),
        page_sleep_min_seconds=float(rest["page_sleep_min_seconds"]),
        page_sleep_max_seconds=float(rest["page_sleep_max_seconds"]),
        rate_limit_wait_seconds=float(rest["rate_limit_wait_seconds"]),
        rate_limit_max_wait_seconds=float(rest["rate_limit_max_wait_seconds"]),
        db_path=_resolve_path(storage["db_path"], resolved_base),
        agg_trades_retention_hours=int(storage["agg_trades_retention_hours"]),
        busy_timeout_ms=int(storage["busy_timeout_ms"]),
        insert_batch_size=int(storage["insert_batch_size"]),
        lookback_hours=int(backfill["lookback_hours"]),
        daemon_incremental_interval_seconds=float(
            daemon["incremental_interval_seconds"]),
        daemon_cleanup_interval_seconds=float(daemon["cleanup_interval_seconds"]),
        sample_path=_resolve_path(sample["path"], resolved_base),
        sample_sha256=str(sample["sha256"]),
        manifest_path=_resolve_path(manifest["path"], resolved_base),
        validation_start_beijing=str(validation["start_rule_beijing"]),
        validation_min_full_days=int(validation["min_full_days"]),
        base_dir=resolved_base,
    )
