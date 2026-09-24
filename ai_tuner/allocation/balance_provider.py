"""
合约账户净资产提供公共模块

统一封装"从交易所获取合约账户净资产（USDT）"的逻辑，供每日分配金额
刷新（daily_refresher）与月度资金分配（monthly_job）共同复用，避免两处
重复代码。

净资产口径：get_account_info().totalMarginBalance（accountEquity / 账户
总权益），包含可用余额 + 持仓保证金 + 未实现盈亏；不使用可用余额
（availableBalance），否则会低估可分配资金、误判各策略占比。
"""

from typing import Optional

import structlog

logger = structlog.get_logger()


async def get_actual_balance(binance_client) -> Optional[float]:
    """
    从交易所获取合约账户净资产（USDT）

    净资产 = get_account_info().totalMarginBalance（账户总权益，取值口径
    与月度分配一致）。

    行为约定：
    1. 有 binance_client 且查询成功 → 返回净资产（float）
    2. 缺 binance_client、查询失败或账户信息缺少 totalMarginBalance →
       返回 None（交由调用方按各自兜底策略处理，保留旧值或使用配置值）

    Args:
        binance_client: 币安客户端（可选），可能为 None

    Returns:
        净资产（USDT）；失败返回 None
    """
    if binance_client is None:
        logger.info("无币安客户端，无法获取合约账户净资产")
        return None

    try:
        account_info = await binance_client.get_account_info()
        # totalMarginBalance 在 PM 账户下即 accountEquity（账户总权益/净资产）
        total_margin_balance = account_info.get("totalMarginBalance")
        if total_margin_balance is None:
            logger.warning("账户信息缺少 totalMarginBalance，返回 None")
            return None
        amount = float(total_margin_balance)
        logger.info(
            "获取合约账户净资产",
            net_asset=amount,
            available_balance=float(
                account_info.get("availableBalance", 0)
            ),
        )
        return amount
    except Exception as e:
        logger.warning("获取合约账户净资产失败", error=str(e))
        return None