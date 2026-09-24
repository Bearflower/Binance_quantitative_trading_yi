"""
组合级熔断指数计算任务（data_backend 容器，每小时 03 分调度）

按分池口径计算池等权 1h 涨跌幅并幂等落库 public.market_circuit_breaker_index：
- mtpcs 池：shared/circuit_breaker_config.yaml 的 fixed_pool（固定 5 币）
- hrs 池（include_hrs_pool=true 时）：读 strategy_states 中 HRS 候选池快照
  （state_key='candidate_pool'，由 HRS 容器在每日候选池扫描后写入，见 C3）

单币 1h 涨跌幅先 cap ±per_symbol_ret_cap 再取均值；有效标的不足 MIN_SYMBOLS 跳过该池。
单池失败仅记 error 日志并写入结果 dict，不抛异常中断（仿 equity_snapshot_job 模式）。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import structlog

from shared.circuit_breaker import (
    compute_1h_return,
    compute_cumulative_return,
    floor_index_hour,
    load_circuit_breaker_config,
)

logger = structlog.get_logger()

# 有效标的数下限（默认兜底值，实际值由配置 min_symbols 覆盖，禁止业务硬编码）：
# 参与均值的标的不足该值时该池指数视为不可用（消除早期小样本尖峰）
MIN_SYMBOLS = 3

# 指数 UPSERT SQL（同池同一整点幂等，失败仅记日志不阻断主流程；1h/12h 同整点不同列）
_INDEX_UPSERT_SQL = """
INSERT INTO public.market_circuit_breaker_index
    (pool, index_hour, equal_weight, symbol_count, symbols,
     equal_weight_12h, symbol_count_12h, symbols_12h, updated_at)
VALUES ($1, $2, $3, $4, $5::jsonb,
        $6, $7, $8::jsonb, CURRENT_TIMESTAMP)
ON CONFLICT (pool, index_hour) DO UPDATE SET
    equal_weight = EXCLUDED.equal_weight,
    symbol_count = EXCLUDED.symbol_count,
    symbols = EXCLUDED.symbols,
    equal_weight_12h = EXCLUDED.equal_weight_12h,
    symbol_count_12h = EXCLUDED.symbol_count_12h,
    symbols_12h = EXCLUDED.symbols_12h,
    updated_at = CURRENT_TIMESTAMP
"""


def _cap_return(ret: float, cap: float) -> float:
    """单币涨跌幅截断到 [-cap, +cap]，防极端值污染指数均值"""
    return max(-cap, min(cap, ret))


async def _compute_pool_index(
    binance_client,
    pool_symbols: List[str],
    cap: float,
    min_symbols: int = MIN_SYMBOLS,
) -> Optional[Tuple[float, List[str], float, List[str]]]:
    """计算池等权 1h 瞬时 与 12h 累计 涨幅（单币先 cap，单币失败跳过）

    一次拉取 13 根 1h K 线，同时算 1h 与 12h 累计两个值；两者共用同一 cap 与有效标的口径
    （某币 1h 或 12h 任一算不出即视为该币本次无效，从两个窗口的有效集中剔除）。

    Args:
        binance_client: BinanceClient（需有 get_klines）
        pool_symbols: 池内标的列表
        cap: 单币涨跌幅 cap 上限（正数，±cap）
        min_symbols: 有效标的下限

    Returns:
        (equal_1h, valid_1h, equal_12h, valid_12h)：
        等权 1h 涨幅与参与及标的、等权 12h 累计涨幅与参与标的；
        有效标的不足 min_symbols 时返回 None
    """
    ret_1h: List[float] = []
    ret_12h: List[float] = []
    valid_1h: List[str] = []
    valid_12h: List[str] = []
    for symbol in pool_symbols:
        try:
            klines = await binance_client.get_klines(symbol, "1h", limit=13)
            r1 = compute_1h_return(klines)
            if r1 is not None:
                ret_1h.append(_cap_return(r1, cap))
                valid_1h.append(symbol)
            r12 = compute_cumulative_return(klines, hours=12)
            if r12 is not None:
                ret_12h.append(_cap_return(r12, cap))
                valid_12h.append(symbol)
        except Exception as e:
            # 单币拉取失败仅跳过，不影响其它标的
            logger.warning("单币涨幅计算失败，跳过", symbol=symbol, error=str(e))
    if not ret_1h or not ret_12h:
        return None
    if len(valid_1h) < min_symbols or len(valid_12h) < min_symbols:
        return None
    return (
        sum(ret_1h) / len(ret_1h),
        valid_1h,
        sum(ret_12h) / len(ret_12h),
        valid_12h,
    )


async def _load_hrs_pool_symbols(db) -> List[str]:
    """读取 HRS 候选池快照（strategy_states, state_key='candidate_pool'）

    Returns:
        候选池 symbols 列表；快照缺失/为空/解析失败时返回空列表（跳过该池）
    """
    try:
        row = await db.fetch_one(
            "SELECT state_data FROM strategy_states "
            "WHERE strategy_name = 'hrs' AND state_key = 'candidate_pool'"
        )
        if not row:
            return []
        state_data = row.get("state_data") or {}
        if isinstance(state_data, str):
            state_data = json.loads(state_data)
        return [str(s).strip().upper() for s in (state_data.get("symbols") or []) if s]
    except Exception as e:
        logger.warning("读取 HRS 候选池快照失败", error=str(e))
        return []


async def _compute_and_store_pool(
    db,
    binance_client,
    pool: str,
    pool_symbols: List[str],
    cap: float,
    index_hour,
    min_symbols: int = MIN_SYMBOLS,
) -> dict:
    """计算并幂等落库单池指数（单池失败不抛异常）

    Args:
        db: 数据库管理器（需有 execute）
        binance_client: BinanceClient
        pool: 池标识（'mtpcs' / 'hrs'）
        pool_symbols: 池内标的列表
        cap: 单币涨跌幅 cap
        index_hour: 北京整点（floor_index_hour 生成）
        min_symbols: 有效标的下限（走配置，默认常量兜底）

    Returns:
        结果 dict（含 equal_weight/symbol_count 或 skipped/error）
    """
    try:
        result = await _compute_pool_index(
            binance_client, pool_symbols, cap, min_symbols=min_symbols
        )
        if result is None:
            msg = f"有效标的不足 {min_symbols} 个"
            logger.warning(
                "池指数计算跳过", pool=pool, reason=msg, pool_size=len(pool_symbols)
            )
            return {"pool": pool, "skipped": True, "reason": msg, "pool_size": len(pool_symbols)}
        equal_1h, valid_1h, equal_12h, valid_12h = result
        await db.execute(
            _INDEX_UPSERT_SQL,
            pool,
            index_hour,
            equal_1h,
            len(valid_1h),
            json.dumps(valid_1h),
            equal_12h,
            len(valid_12h),
            json.dumps(valid_12h),
        )
        logger.info(
            "池指数已落库",
            pool=pool,
            index_hour=index_hour.isoformat(),
            equal_weight=round(equal_1h, 6),
            symbol_count=len(valid_1h),
            equal_weight_12h=round(equal_12h, 6),
            symbol_count_12h=len(valid_12h),
        )
        return {
            "pool": pool,
            "equal_weight": equal_1h,
            "symbol_count": len(valid_1h),
            "symbols": valid_1h,
            "equal_weight_12h": equal_12h,
            "symbol_count_12h": len(valid_12h),
            "symbols_12h": valid_12h,
        }
    except Exception as e:
        logger.error("池指数计算失败", pool=pool, error=str(e))
        return {"pool": pool, "error": str(e)}


async def run_breaker_index(data_service) -> dict:
    """执行一次熔断指数计算并落库

    流程：初始化数据服务 → 加载配置（enabled=false 直接跳过）→
    计算当前北京整点指数 → 逐池（mtpcs 固定池 / hrs 候选池）计算与 UPSERT。
    任一步骤失败仅记录日志并返回错误信息，不抛出异常。

    Args:
        data_service: DataService 实例（需具备 _ensure_initialized/_db_manager/_binance_client）

    Returns:
        dict: {index_hour, results: {pool: 结果}}；禁用或异常时含 skipped/error 信息。
    """
    try:
        await data_service._ensure_initialized()
        cfg = load_circuit_breaker_config()
        if not cfg.get("enabled"):
            logger.info("组合级熔断开关关闭，跳过指数计算")
            return {"skipped": True, "reason": "熔断开关关闭"}

        db = data_service._db_manager
        binance_client = data_service._binance_client
        index_hour = floor_index_hour()
        # 阈值/上限全部走配置（缺键走外层 except 记 error，符合 fail-open）
        cap = float(cfg["per_symbol_ret_cap"])
        # 有效标的下限走配置（默认 MIN_SYMBOLS 兜底）
        min_symbols = int(cfg.get("min_symbols", MIN_SYMBOLS))
        results: Dict[str, Any] = {}

        # 1. mtpcs 池（固定池，始终计算，保证熔断最小可用）
        fixed_pool = [str(s).strip().upper() for s in cfg.get("fixed_pool", [])]
        results["mtpcs"] = await _compute_and_store_pool(
            db, binance_client, "mtpcs", fixed_pool, cap, index_hour, min_symbols=min_symbols
        )

        # 2. hrs 池（仅开启时计算；快照缺失/为空则跳过）
        if cfg.get("include_hrs_pool", False):
            hrs_symbols = await _load_hrs_pool_symbols(db)
            if hrs_symbols:
                results["hrs"] = await _compute_and_store_pool(
                    db, binance_client, "hrs", hrs_symbols, cap, index_hour, min_symbols=min_symbols
                )
            else:
                results["hrs"] = {"pool": "hrs", "skipped": True, "reason": "HRS 候选池快照缺失或为空"}
                logger.warning("HRS 候选池快照缺失或为空，跳过 HRS 池指数计算")

        logger.info(
            "熔断指数计算完成",
            index_hour=index_hour.isoformat(),
            results={k: v for k, v in results.items()},
        )
        return {"index_hour": index_hour.isoformat(), "results": results}
    except Exception as e:
        logger.error("熔断指数计算任务异常", error=str(e))
        return {"error": str(e)}
