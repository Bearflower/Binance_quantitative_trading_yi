from datetime import datetime
from types import SimpleNamespace

import pytest

from strategies.btc_eth.strategy import BTCEthStrategy as OriginalStrategy
from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggressiveStrategy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy_class",
    [OriginalStrategy, AggressiveStrategy],
    ids=["original", "aggressive"],
)
async def test_post_market_state_eligibility_uses_passed_market_behavior(strategy_class):
    strategy = object.__new__(strategy_class)
    strategy.symbol_config = {}
    strategy.frequency_controller = SimpleNamespace(
        daily_trades={},
        symbol_daily_trades={},
        symbol_last_trade_time={},
    )

    can_trade, reason = await strategy._check_stateful_execution_eligibility(
        "BTCUSDT",
        datetime(2026, 10, 20, 12, 0, 0),
        market_state_name="STRONG_TREND",
        market_behavior={"max_daily_trades": 0},
    )

    assert can_trade is False
    assert reason == "频率限制: STRONG_TREND已达每日最大交易数0笔"
