import json

import pytest

from strategies.btc_eth.shadow.entry_ledger import advance_overheat_study


SIGNAL = {"entry_intent": "mean_reversion", "direction": "LONG", "initial_stop_loss": 95, "tp1_price": 110}
HOUR = "2026-11-24T01:00:00"
BAR = {"open": 100, "high": 120, "low": 90, "close": 104}


def test_study_persists_blocked_candidate_into_baseline_settlement():
    positions = {}
    first = {"candidate": SIGNAL, "exits": {}}
    advance_overheat_study(positions, "BTCUSDT", first, BAR, 0.0005, HOUR, blocked=True)
    assert first["overheat_study"] == {"start_utc": HOUR, "blocked": True}
    assert positions["btc_eth_aggressive:BTCUSDT:overheat_baseline"]["pending"]["overheat_blocked"] is True
    assert not positions["btc_eth_aggressive:BTCUSDT:overheat_filter"]

    resumed = json.loads(json.dumps(positions))
    following = {"candidate": {}, "exits": {}}
    advance_overheat_study(resumed, "BTCUSDT", following, BAR, 0.0005, HOUR, blocked=None)
    close = following["exits"]["overheat_baseline"]
    assert close["net_r"] == pytest.approx(-1.0195)
    assert close["overheat_blocked"] is True
    assert "overheat_filter" not in following["exits"]


def test_holding_state_and_execution_are_equal_for_allowed_signal():
    positions = {}
    first = {"candidate": SIGNAL, "exits": {}}
    advance_overheat_study(positions, "BTCUSDT", first, BAR, 0.0005, HOUR, blocked=False)
    following = {"candidate": {}, "exits": {}}
    advance_overheat_study(positions, "BTCUSDT", following, BAR, 0.0005, HOUR, blocked=None)
    control, filtered = (following["exits"][arm] for arm in ("overheat_baseline", "overheat_filter"))
    assert control["overheat_blocked"] is False
    assert control["net_r"] == filtered["net_r"]


@pytest.mark.asyncio
async def test_new_cohort_uses_original_gate_on_aggressive_direction_and_resumes_sqlite(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    import sqlite3
    from types import SimpleNamespace

    from strategies.btc_eth.shadow import entry_ledger as collector

    now = datetime.fromisoformat(HOUR)
    start = now + timedelta(hours=1)
    original_config = {"overheat_protection": dict(enabled=True)}
    aggressive_config = {"entry_filters": dict(enabled=False)}
    gate_calls = []

    def gate(direction, indicators, bars, config):
        gate_calls.append((direction, indicators, config))
        return False, "overheat"

    original = SimpleNamespace(atr_filter=None, symbol_extreme_pause_until={},
                               risk_config={"ranging_strategy": original_config}, _check_overheat_ban=gate)
    aggressive = SimpleNamespace(atr_filter=None, symbol_extreme_pause_until={},
                                 risk_config={"ranging_strategy": aggressive_config},
                                 _build_indicators=lambda bars: "same indicators",
                                 _check_direction_alignment=lambda *args: (True, ""))

    async def collect(*args):
        return {}

    async def evaluate(module, strategy, symbol, bars, at):
        return SIGNAL if strategy is aggressive else {}

    monkeypatch.setattr(collector, "collect", collect)
    monkeypatch.setattr(collector, "snapshot", lambda *args: {"1h": [BAR] * 25})
    monkeypatch.setattr(collector, "evaluate", evaluate)
    dbfile = tmp_path / "shadow.sqlite3"
    args = (None, [(None, original), (None, aggressive)], SimpleNamespace(), ["BTCUSDT"])
    with sqlite3.connect(dbfile) as db:
        db.execute("CREATE TABLE hours (at TEXT PRIMARY KEY, state TEXT, records TEXT)")
        await collector.collect_cycle(db, *args, now, 0.0005, start)
        await collector.collect_cycle(db, *args, start, 0.0005, start)
    with sqlite3.connect(dbfile) as db:
        await collector.collect_cycle(db, *args, start + timedelta(hours=1), 0.0005, start)
        rows = [json.loads(value)[1] for value, in db.execute("SELECT records FROM hours ORDER BY at")]
    assert gate_calls == [("LONG", "same indicators", original_config)] * 2
    assert "overheat_study" not in rows[0]
    assert rows[1]["overheat_study"]["start_utc"] == start.isoformat()
    assert rows[2]["exits"]["overheat_baseline"]["overheat_blocked"]
    assert "overheat_filter" not in rows[2]["exits"]
