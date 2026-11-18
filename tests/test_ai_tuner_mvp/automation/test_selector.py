from decimal import Decimal

import pytest

from ai_tuner.mvp.selector import QualifiedVersion, select_version


def version(name, score, regime="ranging_normal", **overrides):
    fields = dict(version=name, regime=regime, regime_distance=Decimal(0),
                  conservative_score=Decimal(score), max_drawdown=Decimal("0.1"),
                  compatible=True, qualified=True)
    return QualifiedVersion(**(fields | overrides))


def select(candidates, current="active", regime="ranging_normal", history=3):
    return select_version(candidates, current=current, market_regime=regime,
                          min_advantage=Decimal(5), max_historical_versions=history)


def test_only_qualified_compatible_versions_within_regime_can_win():
    options = [version("bad_config", 100, compatible=False), version("failed", 99, qualified=False),
               version("adjacent", 80, "up_trend_normal"), version("exact", 75)]
    assert select(options).selected_version == "exact"


def test_minimum_advantage_and_current_version_are_respected():
    current = version("active", 70, "ranging_high_vol")
    assert select([current, version("new", 74)]).action == "KEEP"
    assert select([current, version("new", 75)]).selected_version == "new"


def test_stress_requires_explicitly_qualified_stress_evidence():
    options = [version("ordinary", 100, stress_safe=True), version("unapproved", 80, "STRESS")]
    assert select(options, regime="STRESS").action == "PAUSE"
    options += [version("stress", 70, "STRESS", stress_safe=True)]
    assert select(options, regime="STRESS", current="ordinary").selected_version == "stress"


def test_history_is_capped_and_ties_are_sorted_stably():
    results = [version("b", 70, historical=True), version("a", 70, historical=True)]
    for entries in (results, list(reversed(results))):
        assert select(entries, history=1).ranked_versions == ("a",)


def test_duplicate_version_cannot_reach_admission():
    with pytest.raises(ValueError, match="duplicate"):
        select([version("same", 10), version("same", 20)])
