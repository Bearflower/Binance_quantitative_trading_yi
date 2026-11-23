import pytest

from strategies.btc_eth.shadow.entry_ledger import advance, arms_for


def test_trend_trial_arms_and_direction_filter_are_distinct():
    trend = {"entry_intent": "trend", "retracement_48h": {"fibonacci": True, "ordinary": False}}
    assert arms_for("btc_eth_aggressive", trend, "LONG") == ("trend", "trend_fibonacci")
    reversal = {"entry_intent": "mean_reversion"}
    assert arms_for("btc_eth_aggressive", reversal, "LONG") == ("mean_reversion", "mean_reversion_aligned")
    assert arms_for("btc_eth_aggressive", reversal, None) == ("mean_reversion",)


def test_execution_starts_next_bar_and_books_fees_once():
    signal = {"direction": "LONG", "initial_stop_loss": 95, "tp1_price": 110}
    bar = {"open": 100, "high": 120, "low": 90, "close": 104}
    state, event = advance({}, signal, bar, 0.0005)
    assert event is None
    assert "pending" in state
    state, event = advance(state, None, bar, 0.0005)
    assert event["reason"] == "stop"
    assert event["net_r"] == pytest.approx(-1.0195)
    assert state == {}
    state, event = advance(state, None, bar, 0.0005)
    assert event is None
