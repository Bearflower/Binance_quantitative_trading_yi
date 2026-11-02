from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_tuner.allocation.config_updater import AllocationConfigUpdater


@pytest.mark.asyncio
async def test_missing_strategy_config_marks_monthly_update_failed():
    entry = SimpleNamespace(strategy_id="hrs", allocated_amount=100, allocated_ratio=0.1)
    result = SimpleNamespace(entries=[entry], month="2026-11")
    operator = MagicMock()

    success = await AllocationConfigUpdater()._update_strategy_configs(
        result, {"strategies": []}, operator
    )

    assert success is False
    operator.apply_changes.assert_not_called()


@pytest.mark.asyncio
async def test_failed_strategy_config_never_publishes_active_db_row(tmp_path):
    entry = SimpleNamespace(strategy_id="hrs", allocated_amount=100, allocated_ratio=0.1)
    result = SimpleNamespace(entries=[entry], month="2026-11", total_capital=1000, reserve_amount=900)
    config_path = tmp_path / "hrs.yaml"
    config_path.write_text("{}")
    operator = MagicMock()
    operator.apply_changes.side_effect = [True, False]
    updater = AllocationConfigUpdater()
    updater._save_to_db = AsyncMock(return_value=True)
    db = MagicMock()

    success = await updater.update_all(
        result,
        {"strategies": [{"strategy_id": "hrs", "config_path": str(config_path)}]},
        db,
        operator,
        None,
    )

    assert success is False
    updater._save_to_db.assert_not_called()
