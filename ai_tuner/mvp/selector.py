"""Deterministically rank already-verified qualification results offline."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class QualifiedVersion:
    version: str
    regime: str
    regime_distance: Decimal
    conservative_score: Decimal
    max_drawdown: Decimal
    compatible: bool
    qualified: bool
    historical: bool = False
    stress_safe: bool = False


@dataclass(frozen=True)
class Selection:
    action: str
    selected_version: str | None
    ranked_versions: tuple[str, ...]


_DIRECTIONS = {"down_trend": -1, "ranging": 0, "up_trend": 1}


def _regime_parts(regime: str) -> tuple[int, bool]:
    for suffix, high_volatility in (("_normal", False), ("_high_vol", True)):
        if regime.endswith(suffix):
            direction = regime[:-len(suffix)]
            if direction in _DIRECTIONS:
                return _DIRECTIONS[direction], high_volatility
    raise ValueError(f"unknown market regime: {regime}")


def _regime_priority(regime: str, other: str) -> int | None:
    if regime == "STRESS" or other == "STRESS":
        return 0 if regime == other else None
    direction, high_vol = _regime_parts(regime)
    other_direction, other_high_vol = _regime_parts(other)
    if direction == other_direction:
        return 0 if high_vol == other_high_vol else 1
    if high_vol == other_high_vol and abs(direction - other_direction) == 1:
        return 2
    return None


def select_version(
    versions: Iterable[QualifiedVersion], *, current: str | None, market_regime: str,
    min_advantage: Decimal, max_historical_versions: int,
) -> Selection:
    """Consume verified outcomes. This function has no access to deployment state."""
    if not min_advantage.is_finite() or min_advantage < 0 or max_historical_versions < 0:
        raise ValueError("invalid selection limits")
    if market_regime != "STRESS":
        _regime_parts(market_regime)

    eligible = {}
    ranked = []
    seen = set()
    for item in versions:
        if not item.version or item.version in seen:
            raise ValueError("empty or duplicate version identity")
        seen.add(item.version)
        if item.compatible is not True or item.qualified is not True:
            continue
        values = (item.regime_distance, item.conservative_score, item.max_drawdown)
        if not all(value.is_finite() for value in values) or min(item.regime_distance, item.max_drawdown) < 0:
            raise ValueError("invalid qualification metrics")
        if market_regime == "STRESS" and (item.regime != "STRESS" or item.stress_safe is not True):
            continue
        eligible[item.version] = item
        priority = _regime_priority(market_regime, item.regime)
        if priority is not None:
            ranked.append((priority, item.regime_distance, -item.conservative_score, item.max_drawdown, item.version))

    history_count = 0
    ordered = []
    for *_, version in sorted(ranked):
        item = eligible[version]
        if item.historical and version != current:
            if history_count >= max_historical_versions:
                continue
            history_count += 1
        ordered.append(version)

    baseline = eligible.get(current)
    if not ordered:
        if market_regime == "STRESS":
            return Selection("PAUSE", None, ())
        if baseline:
            return Selection("KEEP", baseline.version, ())
        return Selection("NO_ELIGIBLE", None, ())

    winner = eligible[ordered[0]]
    ranked_versions = tuple(ordered)
    if baseline and (winner.version == current or winner.conservative_score < baseline.conservative_score + min_advantage):
        return Selection("KEEP", baseline.version, ranked_versions)
    return Selection("SWITCH", winner.version, ranked_versions)
