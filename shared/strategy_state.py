"""
策略状态持久化工具

提供统一的 strategy_states 表写入接口，用于：
1. 各策略定期保存自身状态（心跳）
2. orphan_cleanup 任务统一检测各策略的存活状态
3. 各策略当前持仓上报（trading.strategy_open_positions，供数据看板聚合）

持仓上报说明：
- 各策略在统一保存状态点之后，调用 sync_open_positions 将当前持仓写入
  trading.strategy_open_positions 表。
- margin/quantity 口径由各策略自行算好，本工具只负责写库与容错。
"""

import json
import structlog
from datetime import datetime
from typing import Dict, Any, Optional

logger = structlog.get_logger()

# 持仓保证金最小的有效阈值（margin 低于该值视为无持仓，USDT）
MARGIN_EPSILON = 0.00000001


async def save_strategy_state(
    db,
    strategy_name: str,
    positions: Dict[str, Dict[str, Any]],
    extra_data: Optional[Dict[str, Any]] = None,
    state_key: str = "main",
) -> None:
    """
    保存策略状态到 strategy_states 表

    所有策略统一通过此函数写入，确保 orphan_cleanup 能检测到所有策略。

    Args:
        db: 数据库管理器实例（需有 execute 方法）
        strategy_name: 策略名称（如 'btc_eth', 'hrs', 'new_coin', 'grid'）
        positions: 当前持仓字典
            {symbol: {"direction": str, "entry_price": float, "quantity": float, ...}}
        extra_data: 额外数据（可选，会合并到 state_data 中）
        state_key: 状态键（默认 'main'；扩展状态如候选池快照传 'candidate_pool'）
    """
    try:
        state_data = {
            "positions": positions,
            "updated_at": datetime.now().isoformat(),
        }
        if extra_data:
            state_data.update(extra_data)

        await db.execute(
            """
            INSERT INTO strategy_states (strategy_name, state_key, state_data, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (strategy_name, state_key)
            DO UPDATE SET state_data = $3, updated_at = NOW()
            """,
            strategy_name,
            state_key,
            json.dumps(state_data, default=str),
        )

        logger.debug(
            "策略状态已保存",
            strategy=strategy_name,
            state_key=state_key,
            position_count=len(positions),
        )

    except Exception as e:
        logger.warning(
            "保存策略状态失败",
            strategy=strategy_name,
            state_key=state_key,
            error=str(e),
        )


async def sync_open_positions(
    db,
    strategy_id: str,
    positions_margin: Optional[Dict[str, float]] = None,
    positions_qty: Optional[Dict[str, float]] = None,
) -> None:
    """
    同步策略当前持仓到 trading.strategy_open_positions 表（供数据看板聚合）

    写库逻辑：先 DELETE 该策略全部旧行，再对 margin>0 的每个持仓 UPSERT 新行。
    采用"先删后插"保证平仓后旧行一定被清除，数据量小（策略×币种）成本可忽略。

    容错：任何异常（如表不存在、连接失败、单个 UPSERT 失败）都不抛出，
    仅记录 warning，绝不阻断策略主流程。

    Args:
        db: 数据库管理器实例（需有 execute 方法）
        strategy_id: 策略标识（'btc_eth' / 'new_coin' / 'hrs' / 'grid'）
        positions_margin: {symbol: 保证金(margin)}，margin>0 表示该 symbol 有持仓
        positions_qty: {symbol: 持仓数量(quantity)}
    """
    margin_dict = positions_margin or {}
    qty_dict = positions_qty or {}

    try:
        # 1. 先删除该策略在表中的全部旧行，保证平仓/清仓后不残留
        await db.execute(
            "DELETE FROM trading.strategy_open_positions WHERE strategy_id = $1",
            strategy_id,
        )

        # 2. UPSERT 当前所有有效持仓（margin 大于阈值才视为有持仓）
        for symbol, margin in margin_dict.items():
            try:
                margin_float = float(margin)
                if margin_float <= MARGIN_EPSILON:
                    continue
                qty_float = float(qty_dict.get(symbol, 0.0) or 0.0)
                await db.execute(
                    """
                    INSERT INTO trading.strategy_open_positions
                        (strategy_id, symbol, margin, quantity, updated_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (strategy_id, symbol)
                    DO UPDATE SET margin = $3, quantity = $4, updated_at = NOW()
                    """,
                    strategy_id,
                    symbol,
                    margin_float,
                    qty_float,
                )
            except Exception as e:
                # 单条持仓 UPSERT 失败仅警告，不影响其它持仓
                logger.warning(
                    "同步单个持仓上报失败",
                    strategy=strategy_id,
                    symbol=symbol,
                    error=str(e),
                )

        logger.debug(
            "持仓上报同步完成",
            strategy=strategy_id,
            position_count=sum(1 for v in margin_dict.values() if float(v) > MARGIN_EPSILON),
        )

    except Exception as e:
        logger.warning(
            "同步持仓上报失败（已容错，不影响策略主流程）",
            strategy=strategy_id,
            error=str(e),
        )