from datetime import datetime
from decimal import Decimal

from strategies.grid.execution.store import DecisionStore, migrate
from strategies.grid.market_state import MarketAnalysis, MarketState
from strategies.grid.signal_bot import GridSignal, GridSignalBot


def _signal(now_ms, close_times):
    analysis = MarketAnalysis(
        state=MarketState.OSCILLATION, trend_strength=Decimal("0.1"),
        adx_1h=Decimal("20"), adx_4h=Decimal("20"),
        ema20_1h=Decimal("2000"), ema50_1h=Decimal("2000"),
        current_price=Decimal("2000"), atr_smooth=Decimal("10"),
        confidence=Decimal("0.5"), last_closed_kline_ms=close_times,
    )
    return GridSignal("ETHUSDT", analysis, None, datetime.now(), "message", True, "")


def test_handoff_persists_decision_independent_of_notification(tmp_path, monkeypatch):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    bot = GridSignalBot.__new__(GridSignalBot)
    bot._decision_store = DecisionStore(path)
    bot._metadata = {}
    bot.config = {"trading": {"margin": 500}}
    now = 1_800_000_000_000
    monkeypatch.setattr("strategies.grid.signal_bot._now_ms", lambda: now)
    signal = _signal(now, {"15m": now - 60_000, "1h": now - 60_000,
                           "4h": now - 60_000})
    first = bot._publish_decision(signal)
    assert first == bot._publish_decision(signal)
    item = bot._decision_store.next_decision()
    assert item["decision_id"] == first
    assert item["executable"] is True
    assert item["market_analysis"]["current_price"] == "2000"
    assert bot._decision_store.cursor() is None
    bot._decision_store.close()


def test_stale_or_missing_kline_is_nonexecutable(tmp_path, monkeypatch):
    path = tmp_path / "execution.sqlite3"
    migrate(path)
    bot = GridSignalBot.__new__(GridSignalBot)
    bot._decision_store = DecisionStore(path)
    bot._metadata = {}
    bot.config = {"trading": {"margin": 500}}
    now = 1_800_000_000_000
    monkeypatch.setattr("strategies.grid.signal_bot._now_ms", lambda: now)
    for interval, stale_age in (("15m", 31 * 60_000),
                                ("1h", 91 * 60_000),
                                ("4h", 301 * 60_000)):
        times = {"15m": now - 60_000, "1h": now - 60_000,
                 "4h": now - 60_000}
        times[interval] = now - stale_age
        decision_id = bot._publish_decision(_signal(now, times))
        row = bot._decision_store.conn.execute(
            "SELECT payload_json FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        assert '"executable":false' in row[0]
    bot._decision_store.close()
