from decimal import Decimal

import pytest

from strategies.grid.execution.policy import Action, decide
from strategies.grid.market_state import MarketState


@pytest.mark.parametrize("state,expected", [
    (MarketState.OSCILLATION, Action.START),
    (MarketState.WEAK_TREND, Action.START),
    (MarketState.EARLY_WARNING_15M, Action.PAUSE_EXPOSURE),
    (MarketState.TREND_ACCELERATING, Action.PAUSE_EXPOSURE),
    (MarketState.VOLATILITY_ABNORMAL, Action.PAUSE_ALL),
    (MarketState.PRICE_EMERGENCY, Action.EXIT),
    (MarketState.TREND_CONFIRMED_1H, Action.EXIT),
    (MarketState.EXTREME_STRONG_TREND, Action.EXIT),
    (MarketState.NORMAL_STRONG_TREND, Action.EXIT),
])
def test_all_market_states(state, expected):
    assert decide({"state": state.name, "executable": True, "feasibility":
                   {"feasible": True}}, {"clean": True, "position": Decimal(0)},
                  {"locked": False}) == expected


def test_unknown_stale_or_infeasible_never_opens():
    base = {"state": "OSCILLATION", "executable": True,
            "feasibility": {"feasible": True}}
    account = {"clean": True, "position": Decimal(0)}
    risk = {"locked": False}
    for change in ({"state": "UNKNOWN"}, {"executable": False},
                   {"feasibility": {"feasible": False}}):
        assert decide({**base, **change}, account, risk) == Action.WAIT
    assert decide(base, account, {"locked": True}) == Action.LOCKED
    assert decide(base, {"clean": False}, risk) == Action.LOCKED
