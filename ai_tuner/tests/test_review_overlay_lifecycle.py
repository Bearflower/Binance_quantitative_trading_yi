from ai_tuner.deploy.config_operator import ConfigOperator
from shared.config_loader import load_strategy_config


def test_disjoint_proposals_preserve_values_and_rollback(tmp_path):
    (tmp_path / "config.yaml").write_text("risk: {max: 1, min: 0}\n")
    path = str(tmp_path / "config.yaml")
    writer = ConfigOperator()
    assert writer.apply_overrides(path, {"risk.max": {"from": 1, "to": 2}})
    assert writer.apply_overrides(path, {"risk.min": 1})
    assert load_strategy_config(str(tmp_path))["risk"] == {"min": 1, "max": 2}
    writer.rollback_overlay(path)
    assert load_strategy_config(str(tmp_path))["risk"] == {"min": 0, "max": 2}
    writer.rollback_overlay(path)
    assert load_strategy_config(str(tmp_path))["risk"] == {"min": 0, "max": 1}


def test_stale_proposal_or_prior_value_cannot_replace_active(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("risk: {max: 1}\n")
    writer = ConfigOperator()
    version = writer.publish_overrides(str(config), {"risk.max": 2}, expected_version="base")
    assert version
    assert not writer.publish_overrides(str(config), {"risk.max": 3}, expected_version="base")
    assert not writer.publish_overrides(str(config), {"risk.max": {"from": 1, "to": 3}})
    assert load_strategy_config(str(tmp_path))["risk"]["max"] == 2


def test_bar_returns_include_unrealized_and_fees():
    from ai_tuner.backtest.metrics import MetricsCalculator
    values = MetricsCalculator._calc_segmented_returns(
        {0: "上涨", 1: "横盘"}, [100, 109, 108], 100
    )
    assert values == {"上涨": 9, "下跌": 0, "横盘": -1}


def test_grid_window_excludes_gap_and_duplicate():
    from datetime import datetime, timedelta
    from ai_tuner.adapters.grid_adapter import GridAdapter
    start = datetime(2026, 1, 5)
    end = start + timedelta(hours=2)
    bar = {"open_time": start}
    assert not GridAdapter._covers_hours([bar, bar], start, end)
    assert GridAdapter._covers_hours([bar, {"open_time": start + timedelta(hours=1)}], start, end)


async def test_sunday_grid_excludes_in_progress_bar(monkeypatch):
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock
    from ai_tuner.adapters.grid_adapter import GridAdapter

    class Sunday(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 15, 23, 55)

    monkeypatch.setattr("ai_tuner.adapters.grid_adapter.datetime", Sunday)
    adapter = GridAdapter(MagicMock())
    monkeypatch.setattr(adapter, "_read_config", lambda: {})
    monkeypatch.setattr(adapter, "_query_weekly_trades", AsyncMock(return_value=[]))
    monkeypatch.setattr(adapter, "_simulate", AsyncMock(return_value=[]))
    await adapter.collect()
    assert adapter._simulate.await_args.args[:2] == (
        datetime(2026, 11, 9), datetime(2026, 11, 15, 23),
    )


async def test_http_rollback_retry_preserves_prior_version(tmp_path):
    from unittest.mock import AsyncMock, Mock
    from ai_tuner.main import StratTuneAI

    config = tmp_path / "config.yaml"
    config.write_text("risk: {max: 1}\n")
    service = StratTuneAI.__new__(StratTuneAI)
    service.config = {"strategies": [{"strategy_id": "grid", "config_path": str(config)}]}
    service.config_operator = ConfigOperator()
    service.db_handler = AsyncMock()
    service.messenger = AsyncMock()
    first = service.config_operator.publish_overrides(str(config), {"risk.max": 2})
    second = service.config_operator.publish_overrides(str(config), {"risk.max": 3})
    request = Mock(json=AsyncMock(return_value={"strategy_id": "grid", "expected_version": second}))
    assert (await service._handle_rollback(request)).status == 200
    assert (await service._handle_rollback(request)).status == 409
    assert (config.parent / "tuning_overrides" / ".active").read_text().strip() == first
    service.db_handler.record_overlay_rollback.assert_awaited_once()
