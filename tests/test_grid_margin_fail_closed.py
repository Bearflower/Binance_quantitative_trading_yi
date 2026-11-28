"""A missing or zero risk budget cannot become configured trading capital."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from strategies.grid.grid_calculator import GridCalculator
from strategies.grid.signal_bot import GridSignalBot


@pytest.fixture
def bot():
    instance = GridSignalBot.__new__(GridSignalBot)
    instance.capital_constraint_enabled = True
    instance.default_margin = Decimal('500')
    return instance


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('value', [Decimal('0'), Decimal('-1'), Decimal('NaN'),
                                  Decimal('Infinity'), Decimal('-Infinity'), 'invalid', None])
def test_unusable_advice_never_becomes_available_capital(bot, value, live):
    if live:
        bot._decision_store = object()
        bot.capital_constraint_enabled = False
    advice = SimpleNamespace(suggested_margin=value)
    margin = bot._select_margin_for_feasibility(advice)
    assert margin == Decimal('0')
    config = yaml.safe_load((Path(__file__).parents[1] / 'strategies/grid/config.yaml').read_text())
    feasibility = GridCalculator(config).resolve_capital_feasibility(
        price=Decimal('1700'), leverage=5, margin=margin, n_struct=10,
        n_profit_max=10, n_min=4, min_quantity=Decimal('0.013'),
    )
    assert feasibility.feasible is False


def test_failed_advisor_does_not_authorize_configured_capital(bot):
    assert bot._select_margin_for_feasibility(None) == Decimal('0')


def test_positive_budget_preserves_its_decimal_precision(bot):
    value = Decimal('17.00000000000000000001')
    assert bot._select_margin_for_feasibility(SimpleNamespace(suggested_margin=value)) == value


def test_explicit_legacy_mode_retains_configured_budget(bot):
    bot.capital_constraint_enabled = False
    assert bot._select_margin_for_feasibility(None) == Decimal('500')
