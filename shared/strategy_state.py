"""
策略状态持久化工具

提供统一的 strategy_states 表写入接口，用于：
1. 各策略定期保存自身状态（心跳）
2. orphan_cleanup 任务统一检测各策略的存活状态
"""

import json
import structlog
from datetime import datetime
from typing import Dict, Any, Optional

logger = structlog.get_logger()


async def save_strategy_state(
    db,
    strategy_name: str,
    positions: Dict[str, Dict[str, Any]],
    extra_data: Optional[Dict[str, Any]] = None,
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
            VALUES ($1, 'main', $2, NOW())
            ON CONFLICT (strategy_name, state_key)
            DO UPDATE SET state_data = $2, updated_at = NOW()
            """,
            strategy_name,
            json.dumps(state_data, default=str),
        )

        logger.debug(
            "策略状态已保存",
            strategy=strategy_name,
            position_count=len(positions),
        )

    except Exception as e:
        logger.warning(
            "保存策略状态失败",
            strategy=strategy_name,
            error=str(e),
        )