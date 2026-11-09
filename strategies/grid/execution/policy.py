"""Pure mapping from the existing signal states to execution actions."""

from enum import Enum
from typing import Mapping

from strategies.grid.market_state import MarketState


class Action(str, Enum):
    START = "START"
    REPLAN = "REPLAN"
    PAUSE_EXPOSURE = "PAUSE_EXPOSURE"
    PAUSE_ALL = "PAUSE_ALL"
    EXIT = "EXIT"
    WAIT = "WAIT"
    LOCKED = "LOCKED"


EXIT_STATES = {
    MarketState.PRICE_EMERGENCY.name,
    MarketState.TREND_CONFIRMED_1H.name,
    MarketState.EXTREME_STRONG_TREND.name,
    MarketState.NORMAL_STRONG_TREND.name,
}
PAUSE_EXPOSURE_STATES = {
    MarketState.EARLY_WARNING_15M.name,
    MarketState.TREND_ACCELERATING.name,
}
TRADABLE_STATES = {MarketState.OSCILLATION.name, MarketState.WEAK_TREND.name}


def decide(decision: Mapping, account: Mapping, risk: Mapping) -> Action:
    if risk.get("locked") or not account.get("clean", False):
        return Action.LOCKED
    if not decision.get("executable", False):
        return Action.WAIT
    state = decision.get("state")
    if state in EXIT_STATES:
        return Action.EXIT
    if state in PAUSE_EXPOSURE_STATES:
        return Action.PAUSE_EXPOSURE
    if state == MarketState.VOLATILITY_ABNORMAL.name:
        return Action.PAUSE_ALL
    if state in TRADABLE_STATES:
        if not (decision.get("feasibility") or {}).get("feasible", False):
            return Action.WAIT
        return Action.REPLAN if account.get("active_epoch") else Action.START
    return Action.WAIT
