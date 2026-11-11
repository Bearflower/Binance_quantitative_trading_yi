from decimal import Decimal

import pytest

from strategies.grid.execution.contracts import decode_grid_params, decode_feasibility
from strategies.grid.grid_calculator import GridMode


def test_decodes_typed_grid_fields_from_durable_signal():
    params = decode_grid_params({
        "lower_boundary": "1900", "upper_boundary": "2100", "grid_count": 4,
        "grid_mode": "ARITHMETIC", "stop_loss_low": "1850",
        "stop_loss_high": "2150", "profit_rate": "0.02", "grid_spacing": "50",
        "stop_move_up_price": None, "stop_move_down_price": None,
    })
    feasibility = decode_feasibility({
        "feasible": True, "grid_count": 4, "qty_per_grid": "0.02",
        "binding": "STRUCT", "required_margin": None,
        "required_leverage": None,
    })
    assert params.grid_mode == GridMode.ARITHMETIC
    assert params.lower_boundary == Decimal("1900")
    assert feasibility.qty_per_grid == Decimal("0.02")


def test_rejects_unknown_mode_or_missing_fields():
    with pytest.raises(ValueError):
        decode_grid_params({"grid_mode": "UNKNOWN"})
    with pytest.raises(ValueError):
        decode_feasibility({"feasible": True})
