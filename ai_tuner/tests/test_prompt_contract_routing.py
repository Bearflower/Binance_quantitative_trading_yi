from unittest.mock import MagicMock

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
