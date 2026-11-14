from datetime import datetime, timezone
from unittest.mock import Mock
from backtest.btc_eth.replay.engine import ReplayEngine, stable_sort_key


def test_late_signal_uses_availability_time():
    engine = ReplayEngine.__new__(ReplayEngine)
    engine._settle_pending_limit_orders = Mock()
    engine._run_position_exit_up_to = Mock()
    engine._attempt_entry = Mock()
    event = dict(profile_id='xau_original', decision='candidate', scheduled_at='2026-11-01T10:00:00Z', evaluated_at='2026-11-01T10:20:12Z')
    cutoff = datetime(2026, 11, 1, 11, tzinfo=timezone.utc)
    engine._process_one_event(event, [], cutoff)
    assert engine._attempt_entry.call_args.args[2].minute == 20
    assert stable_sort_key(event)[0].minute == 20


def test_late_signal_after_cutoff_cannot_enter():
    engine = ReplayEngine.__new__(ReplayEngine)
    engine._settle_pending_limit_orders = Mock()
    engine._run_position_exit_up_to = Mock()
    engine._attempt_entry = Mock()
    event = dict(profile_id='xau_original', decision='candidate', scheduled_at='2026-11-01T10:00:00Z', evaluated_at='2026-11-01T11:00:01Z')
    engine._process_one_event(event, [], datetime(2026, 11, 1, 11, tzinfo=timezone.utc))
    engine._attempt_entry.assert_not_called()


def test_real_market_fills_follow_evaluation():
    import json
    from pathlib import Path
    from datetime import timedelta
    from tests.test_shadow_replay.test_replay_determinism import _make_fixture_dir, _run_engine_once
    fixture = _make_fixture_dir()
    path = Path(fixture["events_path"])
    events = [json.loads(line) for line in path.read_text().splitlines()]
    for event in events:
        scheduled = datetime.fromisoformat(event["scheduled_at"])
        event["evaluated_at"] = (scheduled + timedelta(minutes=20)).isoformat()
    events[0].update(decision="candidate", direction="LONG", grade="A",
                     reference_price="2000", intended_order_type="MARKET",
                     intended_entry_price="2000", stop_loss_rule={"atr_value": "10"})
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = _run_engine_once(fixture)
    filled = [trade for pid in ("xau_original", "xau_aggressive") for trade in result[pid]["trades"] if trade.get("filled")]
    assert filled
    for trade in filled:
        assert datetime.fromisoformat(trade["entry_time"]).minute >= 20
