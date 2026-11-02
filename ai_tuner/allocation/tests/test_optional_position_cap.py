from types import SimpleNamespace

import pytest
import yaml

from ai_tuner.allocation.config_updater import AllocationConfigUpdater
from ai_tuner.deploy.config_operator import ConfigOperator


class RecordingOperator(ConfigOperator):
    def apply_changes(self, config_path, adjustments):
        self.adjustments = adjustments
        return super().apply_changes(config_path, adjustments)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_id", ["grid", "new_coin"])
async def test_allocation_updates_strategy_without_position_sizing(tmp_path, strategy_id):
    path = tmp_path / f"{strategy_id}.yaml"
    path.write_text("trading:\n  margin: 100\ncapital_limits:\n  monthly_limit: 100\n")
    entry = SimpleNamespace(strategy_id=strategy_id, allocated_amount=300, allocated_ratio=0.3)
    result = SimpleNamespace(entries=[entry], month="2026-11")

    operator = RecordingOperator()
    assert await AllocationConfigUpdater()._update_strategy_configs(
        result,
        {"strategies": [{"strategy_id": strategy_id, "config_path": str(path)}]},
        operator,
    )
    assert "position_sizing.total.account_ratio_cap" not in operator.adjustments
    updated = yaml.safe_load(path.read_text())
    assert updated["capital_limits"]["monthly_limit"] == 300
    assert "position_sizing" not in updated


@pytest.mark.asyncio
async def test_allocation_keeps_existing_position_cap_in_sync(tmp_path):
    path = tmp_path / "btc_eth.yaml"
    path.write_text("position_sizing:\n  total:\n    account_ratio_cap: 0.2\n")
    entry = SimpleNamespace(strategy_id="btc_eth", allocated_amount=300, allocated_ratio=0.3)
    result = SimpleNamespace(entries=[entry], month="2026-11")

    assert await AllocationConfigUpdater()._update_strategy_configs(
        result,
        {"strategies": [{"strategy_id": "btc_eth", "config_path": str(path)}]},
        ConfigOperator(),
    )
    updated = yaml.safe_load(path.read_text())
    assert updated["position_sizing"]["total"]["account_ratio_cap"] == 0.3
    assert updated["capital_limits"]["allocated_ratio"] == 0.3
