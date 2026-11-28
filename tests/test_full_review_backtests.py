"""2026-11-26 全量审查的离线回归用例。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import MagicMock

import pytest

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.state import VirtualState
from backtest.btc_eth.replay.matching import clamp_notional
from backtest.grid.backtest_engine import BacktestExchangeSimulator
from backtest.grid.exchange_simulator import ExchangeSimulator
from backtest.new_coin.backtest_engine import BacktestEngine
from backtest.new_coin.report_generator import ReportGenerator
from backtest.new_coin.position_manager import PositionManager as NewCoinPositions

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def replay(direction="LONG", with_tp=False):
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.profile_ids = ["p"]
    engine.states = {"p": VirtualState()}
    engine.replay_trades = {"p": []}
    engine.invariant_violations = []
    engine.step_size, engine.tick_size = D(".001"), D(".01")
    engine.min_notional = engine.taker_bps = engine.slippage_baseline = D("0")
    engine.funding_events = []
    engine.states["p"].on_entry_approved(
        at=T, symbol="XAUUSDT", direction=direction, quantity=D("2"),
        entry_price=D("100"), margin=D("10"), leverage=1, grade="A",
        stop_loss_price=D("90") if with_tp else None,
        tp1=D("110") if with_tp else None,
        tp1_close_ratio=D(".5") if with_tp else None,
    )
    return engine


def bar(minute, high, low, close):
    return dict(open_time=int((T + timedelta(minutes=minute)).timestamp() * 1000),
                open="100", high=str(high), low=str(low), close=str(close), volume="100")


def test_partial_tp_keeps_scanning_until_later_stop():
    engine = replay(with_tp=True)
    bars = [bar(0, 111, 99, 110), bar(1, 112, 80, 100), bar(2, 121, 100, 120)]
    end = T + timedelta(minutes=3)
    engine._run_position_exit_up_to("p", end, bars, end)
    engine._force_close_remaining_positions(bars, end)
    assert [t["exit_reason"] for t in engine.replay_trades["p"]] == ["TP1", "STOP_LOSS"]
    assert engine.states["p"].equity == D("300")


@pytest.mark.parametrize("direction,high,low", [("LONG", 105, 80), ("SHORT", 120, 95)])
def test_mae_keeps_adverse_extreme(direction, high, low):
    engine = replay(direction)
    bars = [bar(0, high, low, 100), bar(1, 101, 99, 100)]
    end = T + timedelta(minutes=2)
    engine._run_position_exit_up_to("p", end, bars, end)
    engine._force_close_remaining_positions(bars, end)
    assert D(engine.replay_trades["p"][0]["mae"]) == D("20")


def test_min_notional_uses_quantity_step():
    assert clamp_notional(D(".002"), D("2000"), D("5"), D(".001")) == D(".003")
    assert clamp_notional(D(".01"), D("100"), D("5"), D(".02")) == D(".06")


def grid_data():
    return {"1h": [{"timestamp": f"2026-01-01T{h:02}:00:00"} for h in range(5)],
            "4h": [{"timestamp": "2026-01-01T00:00:00", "close": 200},
                   {"timestamp": "2026-01-01T04:00:00", "close": 999}]}


def test_grid_higher_timeframe_waits_for_close():
    exchange = BacktestExchangeSimulator.__new__(BacktestExchangeSimulator)
    exchange.tf_data = grid_data()
    exchange._index_1h_to_4h = exchange._build_tf_index_map("1h", "4h")
    exchange.current_index = 1  # 01:00 开盘的 K 线在 02:00 收盘
    assert exchange.get_mapped_index("4h") == -1
    assert exchange.get_klines_up_to("4h") == []
    assert exchange.get_current_kline("4h") == {}
    exchange.current_index = 3  # 到达 04:00，首根 4h 线已收盘
    assert exchange.get_klines_up_to("4h") == [exchange.tf_data["4h"][0]]


def test_grid_exchange_never_exposes_unclosed_or_pre_start_bars():
    exchange = ExchangeSimulator.__new__(ExchangeSimulator)
    exchange._symbol = "ETHUSDT"
    exchange._klines = {"ETHUSDT": grid_data()}
    exchange._data_loaded = True
    exchange._current_index = -1
    assert exchange.get_klines("ETHUSDT", "4h") == []
    exchange._current_index = 1
    assert exchange.get_klines("ETHUSDT", "4h") == []
    exchange._current_index = 3
    assert len(exchange.get_klines("ETHUSDT", "4h")) == 1
    assert len(exchange.get_klines("ETHUSDT", "1h")) == 4


def test_new_coin_manages_existing_position_after_entry_window():
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = {"kline": {"min_klines_for_analysis": 1},
                     "scoring": {"veto_thresholds": {"listing_hours": 48}}}
    engine.start_date, engine.end_date = "2026-01-01", "2026-01-10"
    engine.position_manager = MagicMock()
    engine.position_manager.has_position.return_value = True
    engine._check_position_management = MagicMock()
    listing = datetime(2026, 1, 1)
    bars = [{"open_time": int((listing + timedelta(hours=h)).timestamp() * 1000), "close": 200}
            for h in (48, 49, 72)]
    engine._process_klines("TESTUSDT", bars, listing)
    assert engine._check_position_management.call_count == 2


def test_new_coin_chart_files_are_created(tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    reporter = ReportGenerator.__new__(ReportGenerator)
    reporter.charts_dir = str(tmp_path)
    reporter._generate_charts(
        [{"time": "2026-01-01", "balance": 300}, {"time": "2026-01-02", "balance": 290}],
        [{"pnl": -10}],
    )
    assert (tmp_path / "equity_curve.png").stat().st_size > 0
    assert (tmp_path / "drawdown_curve.png").stat().st_size > 0


def test_new_coin_stop_after_partial_tp_closes_only_remaining_quantity():
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.position_manager = MagicMock()
    engine.position_manager.get_position.return_value = {
        "entry_time": T, "entry_price": D("100"), "quantity": D("10"),
        "remaining_quantity": D("7"),
    }
    engine.order_executor = MagicMock()
    engine.order_executor.close_position.return_value = True
    engine.order_executor.calculate_pnl.return_value = D("-70")
    engine.balance = D("300")
    engine.trades, engine.equity_curve = [], []
    engine.consecutive_losses = 0
    engine._execute_exit("TESTUSDT", D("110"), T + timedelta(hours=49), "止损", 1.0)
    assert engine.order_executor.close_position.call_args.kwargs["quantity"] == D("7")
    assert engine.trades[0]["quantity"] == D("7")


def test_new_coin_tp_then_stop_conserves_quantity_with_real_position_manager():
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = {}
    engine.position_manager = NewCoinPositions(D("300"), {})
    engine.position_manager.open_position(
        "TESTUSDT", D("100"), T, D("10"), D("110"), [D("95"), D("90")], D("2"))
    engine.order_executor = MagicMock()
    engine.order_executor.close_position.return_value = True
    engine.order_executor.calculate_pnl.side_effect = lambda **args: (args["entry_price"] - args["exit_price"]) * args["quantity"]
    engine.balance = D("300")
    engine.trades, engine.equity_curve = [], []
    engine.consecutive_losses = 0
    engine._check_position_management("TESTUSDT", D("95"), T + timedelta(hours=1))
    assert engine.position_manager.get_position("TESTUSDT")["remaining_quantity"] == D("7")
    engine._check_position_management("TESTUSDT", D("110"), T + timedelta(hours=49))
    assert [trade["quantity"] for trade in engine.trades] == [D("3"), D("7")]
    assert not engine.position_manager.has_position("TESTUSDT")
    assert engine.balance == D("245")
