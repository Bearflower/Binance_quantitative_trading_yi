"""影子采集器入口（python -m strategies.btc_eth.shadow.main）

装配：
  - ReadOnlyBinanceClient（只读安全边界，fail-closed）
  - KLineService（复用生产行情服务）
  - DatabaseManager（trading_user，只写 research schema）
  - 两 profile 冻结 YAML 配置（xau_original / xau_aggressive）
  - APScheduler cron 调度（默认 15 * * * * 与生产 5/10 分错峰）

职责：
  1. 启动时检测 config_hash 是否变化 → 变化则开启新 run_id
  2. cron 触发 → ShadowCollector.run_once()
  3. run_id 生成：确定性 sha256(run_start_iso + git_commit + sorted config_hashes)[:16]
  4. 进程启动时 upsert active run；进程正常退出时 close_run

不做的事：
  - 绝不调用 Binance 写接口（ReadOnlyBinanceClient 双层 fail-closed）
  - 绝不写 trading.*（repository.py 全部 SQL 硬性 research. 前缀）
  - 绝不调 _check_stateful_execution_eligibility / _calculate_position_size
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.kline_service import KLineService
from shared.readonly_client import ReadOnlyBinanceClient
from shared.config_loader import compute_config_hash

from .collector import (
    ShadowCollector,
    _read_git_info,
)
from .instance import (
    ShadowInstanceConfig,
    parse_shadow_instance_config,
    connect_db_from_env,
)
from .repository import ShadowRepository


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 路径常量（单点，禁止散落在 main.py 各处硬编码）
# ---------------------------------------------------------------------------
_STRATEGIES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # strategies/btc_eth
_PROFILE_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
_DEFAULT_PROFILES = ["xau_original", "xau_aggressive"]
_DEFAULT_CRON = "15 * * * *"


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def _load_profile_configs() -> Dict[str, Dict[str, Any]]:
    """加载全部 profile 冻结 YAML 配置

    Returns:
        {profile_id: config_dict}
    """
    configs: Dict[str, Dict[str, Any]] = {}
    profiles_env = os.getenv("SHADOW_PROFILES")
    profiles = [p.strip() for p in profiles_env.split(",") if p.strip()] if profiles_env else _DEFAULT_PROFILES

    for pid in profiles:
        yaml_path = os.path.join(_PROFILE_CONFIG_DIR, f"{pid}.yaml")
        if not os.path.exists(yaml_path):
            logger.error("profile 配置文件不存在，跳过", profile_id=pid, path=yaml_path)
            continue
        with open(yaml_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        configs[pid] = cfg
        logger.info(
            "已加载 profile 配置",
            profile_id=pid,
            path=yaml_path,
            config_hash=compute_config_hash(cfg)[:16] + "...",
        )
    if not configs:
        raise SystemExit("无可用 profile 配置，影子采集器无法启动")
    return configs


# ---------------------------------------------------------------------------
# run_id 确定性生成
# ---------------------------------------------------------------------------
def _generate_run_id(
    run_start: datetime,
    git_commit: str,
    config_hashes: Dict[str, str],
    *,
    prefix: str = "xau-shadow",
) -> str:
    """确定性生成 run_id：sha256(iso_time|git|sorted_hashes)[:16]

    相同 run_start + 相同 config_hashes + 相同 git → 相同 run_id（幂等）。
    不同任一项 → 不同 run_id（配置变了自然开新 run）。
    """
    sorted_hashes = json.dumps(config_hashes, sort_keys=True, ensure_ascii=False)
    raw = f"{run_start.isoformat()}|{git_commit}|{sorted_hashes}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{run_start.strftime('%Y%m%d%H%M%S')}-{digest}"


# ---------------------------------------------------------------------------
# 依赖装配
# ---------------------------------------------------------------------------
def _build_binance_client(instance_id: str) -> ReadOnlyBinanceClient:
    """构造使用运行时实例标识的只读 Binance 客户端"""
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        logger.warning(
            "BINANCE_API_KEY / BINANCE_API_SECRET 未设置，只读客户端可能无法部分初始化",
        )
    return ReadOnlyBinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        instance_id=instance_id,
    )


def _build_kline_service() -> KLineService:
    """构造 K 线服务客户端"""
    service_url = os.getenv("KLINE_SERVICE_URL", "http://kline-service:8080")
    return KLineService(service_url=service_url)


def _build_runtime_dependencies(
    shadow_cfg: ShadowInstanceConfig,
) -> tuple[ReadOnlyBinanceClient, KLineService]:
    """按运行时实例配置装配外部客户端。"""
    return (
        _build_binance_client(shadow_cfg.instance_id),
        _build_kline_service(),
    )


# ---------------------------------------------------------------------------
# 主调度循环
# ---------------------------------------------------------------------------
async def _build_collector_and_run_once(
    collector: ShadowCollector,
) -> None:
    """cron 调度回调：执行一次完整采集周期"""
    try:
        await collector.run_once()
    except Exception as exc:
        # 调度回调自身不能向上抛，否则 APScheduler 可能跳过后续触发
        logger.error(
            "影子采集周期异常（已吞掉，等待下次 cron 触发）",
            run_id=collector.run_id, error=str(exc), exc_info=True,
        )


async def _prepare_shadow_run(
    repo: ShadowRepository,
    shadow_cfg: ShadowInstanceConfig,
    *,
    now: datetime,
    git_commit: str,
    git_sha: str,
    config_hashes: Dict[str, str],
) -> str:
    """选择可复用的 active run，或按运行时前缀创建新 run。"""
    active_run = await repo.find_active_run(instance_id=shadow_cfg.instance_id)

    if active_run:
        existing_hashes_raw = active_run.get("config_hashes")
        existing_hashes: Dict[str, str] = {}
        if existing_hashes_raw:
            try:
                existing_hashes = (
                    json.loads(existing_hashes_raw)
                    if isinstance(existing_hashes_raw, str)
                    else dict(existing_hashes_raw)
                )
            except Exception:
                existing_hashes = {}
        if (
            existing_hashes == config_hashes
            and (active_run.get("git_commit") or "") == git_commit
        ):
            run_id = active_run["run_id"]
            logger.info("继续使用已存在的 active run", run_id=run_id)
            return run_id

        old_run_id = active_run["run_id"]
        logger.warning(
            "config_hash 或 git 变化，关闭旧 run 开新 run",
            old_run_id=old_run_id,
            new_hashes=config_hashes,
            old_hashes=existing_hashes,
        )
        await repo.close_shadow_run(run_id=old_run_id, status="closed")

    run_id = _generate_run_id(
        now,
        git_commit,
        config_hashes,
        prefix=shadow_cfg.run_id_prefix,
    )
    await repo.upsert_shadow_run(
        run_id=run_id,
        instance_id=shadow_cfg.instance_id,
        symbol=shadow_cfg.symbol,
        started_at=now,
        initial_virtual_equity_usdt=shadow_cfg.initial_virtual_equity_usdt,
        git_commit=git_commit,
        git_sha=git_sha,
        config_hashes=config_hashes,
    )
    if not active_run:
        logger.info("首次启动，已创建新 active run", run_id=run_id)
    return run_id


async def run_forever() -> None:
    """影子采集器主循环"""
    # ---- 1. 加载配置 ----
    profile_configs = _load_profile_configs()
    # 以第一个 profile 的 instance 段作为全局影子实例配置
    global_cfg = next(iter(profile_configs.values()))
    shadow_cfg = parse_shadow_instance_config(global_cfg)

    # ---- 2. 读 git 信息 ----
    git_commit, git_sha = _read_git_info()
    logger.info(
        "git 信息",
        git_commit=git_commit or "(空)",
        git_sha=git_sha or "(空)",
    )

    # ---- 3. 计算 config_hashes ----
    config_hashes = {
        pid: compute_config_hash(cfg) for pid, cfg in profile_configs.items()
    }

    # ---- 4. 检查是否已有 active run（config_hash 变了则开新 run） ----
    db = connect_db_from_env()
    await db.connect()
    repo = ShadowRepository(db)

    now = datetime.utcnow()
    run_id = await _prepare_shadow_run(
        repo,
        shadow_cfg,
        now=now,
        git_commit=git_commit,
        git_sha=git_sha,
        config_hashes=config_hashes,
    )

    # ---- 5. 装配依赖 ----
    binance_client, kline_service = _build_runtime_dependencies(shadow_cfg)

    collector = ShadowCollector(
        run_id=run_id,
        instance_id=shadow_cfg.instance_id,
        symbol=shadow_cfg.symbol,
        profiles=shadow_cfg.profiles,
        profile_configs=profile_configs,
        repository=repo,
        kline_service=kline_service,
        binance_client=binance_client,
        git_commit=git_commit,
        git_sha=git_sha,
        kline_intervals=shadow_cfg.kline_intervals,
        initial_virtual_equity_usdt=shadow_cfg.initial_virtual_equity_usdt,
    )

    # ---- 6. 启动前先跑一次（让初始 active run 有首批事件） ----
    logger.info("启动即执行首轮采集", run_id=run_id)
    await _build_collector_and_run_once(collector)

    # ---- 7. cron 调度 ----
    scheduler = AsyncIOScheduler(timezone="UTC")
    cron_expr = os.getenv("SHADOW_CRON", shadow_cfg.cron)
    parts = cron_expr.split()
    if len(parts) != 5:
        logger.warning("cron 表达式异常，降级默认", cron=cron_expr)
        cron_expr = _DEFAULT_CRON
        parts = cron_expr.split()

    minute, hour, day, month, day_of_week = parts
    scheduler.add_job(
        _build_collector_and_run_once,
        trigger="cron",
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        args=[collector],
        id=f"shadow-{run_id}",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "影子采集器已启动（cron=%s）",
        cron_expr,
        run_id=run_id,
    )

    # ---- 8. 主循环 ----
    try:
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        logger.info("收到 SIGINT，正在优雅关闭...")
    finally:
        scheduler.shutdown(wait=False)
        # 关闭 active run
        await repo.close_shadow_run(run_id=run_id, status="aborted")
        await kline_service.close()
        await db.disconnect()
        logger.info("影子采集器已关闭", run_id=run_id)


def main() -> None:
    """入口：python -m strategies.btc_eth.shadow.main"""
    try:
        asyncio.run(run_forever())
    except Exception as exc:
        logger.critical("影子采集器启动失败", error=str(exc), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
