import json
import re
from unittest.mock import MagicMock

import pytest

from ai_tuner.engine.response_parser import ResponseParser
from ai_tuner.scheduler.weekly_job import WeeklyTuningJob


def test_grid_prompt_uses_legacy_contract():
    job = WeeklyTuningJob(
        config={}, db_manager=MagicMock(), notification_client=MagicMock(),
        llm_client=MagicMock(), db_handler=MagicMock(), cost_tracker=MagicMock(),
        messenger=MagicMock(), config_operator=MagicMock(), rollback_manager=MagicMock(),
    )
    adapter = MagicMock()
    adapter._strategy_cfg = {}
    adapter.get_current_params.return_value = {}

    system, _ = job._build_prompts("grid", "网格交易策略", adapter, {}, "")

    assert "reasons" in system and "summary" in system
    assert "decision" not in system
    assert "只对目的驱动型" not in system


def test_manifest_strategy_keeps_new_contract():
    job = WeeklyTuningJob(
        config={}, db_manager=MagicMock(), notification_client=MagicMock(),
        llm_client=MagicMock(), db_handler=MagicMock(), cost_tracker=MagicMock(),
        messenger=MagicMock(), config_operator=MagicMock(), rollback_manager=MagicMock(),
    )
    adapter = MagicMock()
    adapter._strategy_cfg = {"parameter_manifest": "manifest.yaml"}
    adapter.get_current_params.return_value = {}

    system, _ = job._build_prompts("hrs", "HRS", adapter, {}, "")

    assert '"decision"' in system
    assert '"objective"' in system


@pytest.mark.parametrize("strategy_id", ["btc_eth_aggressive", "hrs"])
def test_manifest_prompt_json_example_is_valid_hold_envelope(strategy_id):
    """Both candidate prompts must show a JSON example accepted by the real parser."""
    job = WeeklyTuningJob(
        config={}, db_manager=MagicMock(), notification_client=MagicMock(),
        llm_client=MagicMock(), db_handler=MagicMock(), cost_tracker=MagicMock(),
        messenger=MagicMock(), config_operator=MagicMock(), rollback_manager=MagicMock(),
    )
    adapter = MagicMock()
    adapter._strategy_cfg = {"parameter_manifest": "manifest.yaml"}
    adapter.get_current_params.return_value = {}

    system, _ = job._build_prompts(strategy_id, strategy_id, adapter, {}, "")
    example_match = re.search(r"```json\s*(\{.*?\})\s*```", system, flags=re.DOTALL)
    assert example_match is not None
    example = json.loads(example_match.group(1))

    assert isinstance(example["diagnosis"], list)
    assert isinstance(example["target_metrics"], dict)
    assert isinstance(example["expected_impact"], dict)
    parsed = ResponseParser().parse_response(
        json.dumps(example, ensure_ascii=False), contract_mode="new"
    )
    assert "error" not in parsed, parsed
    assert parsed["decision"] == "HOLD"
    assert parsed["adjustments"] == {}
