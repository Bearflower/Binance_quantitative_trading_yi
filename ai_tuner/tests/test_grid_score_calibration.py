from unittest.mock import patch
import pytest
from ai_tuner.backtest.metrics import MetricsCalculator
from ai_tuner.backtest.grid_backtest import GridBacktestEngine
from ai_tuner.backtest.models import GridParams
from ai_tuner.adapters.grid_adapter import GridAdapter


def test_losing_scenarios_preserve_order_and_precision():
    calc = MetricsCalculator()
    def score(ret):
        return calc._calc_composite_score({'total_return_pct': ret, 'sharpe_ratio': -1.6, 'max_drawdown_pct': .8})
    assert score(-52) > score(-77) > score(-97)
    assert score(-52.00001) != score(-52)


def test_initial_equity_is_margin_and_exposure_is_bounded():
    engine = GridBacktestEngine({})
    params = GridParams(margin=100, leverage=2, single_position_margin=100, current_price=100)
    engine._cache_params(params)
    state = engine._init_state(params)
    assert state.cash == 100
    assert engine._can_fill(state, 1, 100)
    assert not engine._can_fill(state, 3, 100)
    state.position = 3
    assert engine._can_fill(state, -1, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_drawdown", [.2, .3])
async def test_better_score_cannot_override_drawdown_cap(candidate_drawdown):
    adapter = GridAdapter(db_manager=None)
    adapter._system_config = {'backtest': {'drawdown_cap': .2}}
    with patch.object(GridBacktestEngine, 'run', side_effect=[
        {'composite_score': -100, 'max_drawdown_pct': .8},
        {'composite_score': -50, 'max_drawdown_pct': candidate_drawdown},
    ]):
        result = await adapter._validate_new_params(GridParams(), GridParams(), [])
    assert result['passed'] is False
    assert result['status'] == 'rejected'


def test_round_trip_equity_matches_cash_profit_and_fees():
    engine = GridBacktestEngine({'backtest': {'fee': {'maker': .001, 'slippage': 0}}})
    engine._cache_params(GridParams(margin=100, leverage=2, current_price=100, atr=1))
    state = engine._init_state(engine._cached_params)
    engine._execute_buy(state, 100, 1, 0, None, '横盘')
    assert state.cash + state.position * 100 == pytest.approx(99.9)
    engine._execute_sell(state, 110, 1, 1, None, '横盘')
    assert state.position == 0
    assert state.cash == pytest.approx(109.79)
    assert state.total_fee == pytest.approx(.21)


def test_short_round_trip_equity_matches_cash_profit_and_fees():
    engine = GridBacktestEngine({'backtest': {'fee': {'maker': .001, 'slippage': 0}}})
    engine._cache_params(GridParams(margin=100, leverage=2, current_price=100, atr=1))
    state = engine._init_state(engine._cached_params)
    engine._execute_sell(state, 100, 1, 0, None, '横盘')
    assert state.cash + state.position * 100 == pytest.approx(99.9)
    engine._execute_buy(state, 90, 1, 1, None, '横盘')
    assert state.position == 0
    assert state.cash == pytest.approx(109.81)
