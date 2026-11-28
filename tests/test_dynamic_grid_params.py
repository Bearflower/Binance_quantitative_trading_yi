"""动态网格三层价格边界；非网格市场状态必须交由信号服务处理。"""
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from strategies.grid.grid_calculator import GridCalculator


@pytest.fixture
def calculator():
    config = yaml.safe_load((Path(__file__).parents[1] / 'strategies/grid/config.yaml').read_text())
    return GridCalculator(config)


@pytest.mark.parametrize('state', ['震荡市场', '弱趋势'])
@pytest.mark.parametrize('strength', ['0', '.3', '.5'])
def test_three_price_boundaries_are_independent(calculator, state, strength):
    config = calculator.config['grid']
    params = calculator.calculate_dynamic_grid_params(
        current_price=D('3000'), atr_smooth=D('80'), atr_baseline=D('75'),
        market_state=state, trend_strength=D(strength),
    )
    buffer = D(str(config['weak_trend_stop_loss_buffer'] if state == '弱趋势' else config['stop_loss_buffer']))
    move = buffer / D(str(config['oscillation_move_buffer_divisor']))
    assert params.stop_loss_low < params.stop_move_down_price < params.lower_boundary
    assert params.upper_boundary < params.stop_move_up_price < params.stop_loss_high
    assert params.stop_loss_low == params.lower_boundary - buffer * D('80')
    assert params.stop_loss_high == params.upper_boundary + buffer * D('80')
    assert params.stop_move_down_price == params.lower_boundary - move * D('80')
    assert params.stop_move_up_price == params.upper_boundary + move * D('80')


@pytest.mark.parametrize('state', ['上升趋势', '下降趋势', '未知'])
def test_non_grid_state_cannot_generate_executable_grid(calculator, state):
    with pytest.raises(ValueError, match='signal_bot'):
        calculator.calculate_dynamic_grid_params(D('3000'), D('80'), D('75'), state, D('.3'))
