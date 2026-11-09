"""The attribution schema must preserve identities and forbid evidence mutation."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SQL_FILES = (
    ROOT / "database/postgres/init-scripts/19-derived-income-attribution.sql",
    ROOT / "database/postgres/migrations/2026-11-09-derived-income-attribution.sql",
)


@pytest.mark.parametrize("path", SQL_FILES)
def test_schema_has_complete_income_identity_and_immutable_evidence(path):
    sql = path.read_text().lower()
    assert "trading.order_strategy_evidence" in sql
    assert "trading.position_flat_baseline" in sql
    assert "trading.derived_income_attribution" in sql
    assert "primary key (account_id, income_type, tran_id)" in sql
    assert "primary key (account_id, symbol, order_id)" in sql
    assert "evidence jsonb not null" in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql
