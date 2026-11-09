from strategies.btc_eth_aggressive.main import signal_execution_status


def test_signal_status_shows_risk_rejection_saved_as_skip_reason():
    assert signal_execution_status({
        "trade_success": False,
        "skip_reason": "take profit or remainder infeasible without rounding up",
    }) == "⛔被拦截(缩量后止盈或剩余仓位不足交易所最小下单量)"


def test_signal_status_distinguishes_success_and_unknown_failure():
    assert signal_execution_status({"trade_success": True, "skip_reason": "stale"}) == "✅已开仓"
    assert signal_execution_status({"trade_success": False}) == "⏳未开仓"
