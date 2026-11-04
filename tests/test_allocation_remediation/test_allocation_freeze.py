from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from ai_tuner.allocation.monthly_job import MonthlyAllocationJob
from ai_tuner.allocation.daily_refresher import DailyAllocationRefresher
from ai_tuner.allocation.config_updater import AllocationConfigUpdater


@pytest.mark.asyncio
async def test_monthly_freeze_precedes_all_reads_writes_and_collection():
    job=MonthlyAllocationJob({'capital_allocation':{'enabled':True}},AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock())
    job._ensure_table=AsyncMock()
    job.pnl_collector=NS(collect_all_realized_pnl=AsyncMock())
    job.calculator=NS(calculate=AsyncMock())
    assert await job.run_monthly_allocation() is None
    job._ensure_table.assert_not_called()
    assert job.db_manager.mock_calls==[]
    job.pnl_collector.collect_all_realized_pnl.assert_not_called()
    job.calculator.calculate.assert_not_called()


@pytest.mark.asyncio
async def test_daily_freeze_preserves_amount_even_after_nav_growth():
    db=AsyncMock();balance=AsyncMock(return_value=1000000)
    refresher=DailyAllocationRefresher({'capital_allocation':{'enabled':True}},db,AsyncMock())
    refresher._get_actual_balance=balance
    assert await refresher.run_daily_refresh() is None
    balance.assert_not_called()
    assert db.mock_calls==[]


@pytest.mark.asyncio
async def test_direct_config_updater_freeze_has_no_writer_or_file_changes(tmp_path):
    config=tmp_path/'config.yaml';config.write_text('allocated_amount: 100\n')
    updater=AllocationConfigUpdater()
    updater._update_tuner_config=AsyncMock(return_value=True)
    updater._update_strategy_configs=AsyncMock(return_value=True)
    updater._save_to_db=AsyncMock(return_value=True)
    assert await updater.update_all(NS(month='2026-11'),{},AsyncMock(),AsyncMock(),AsyncMock()) is False
    updater._update_tuner_config.assert_not_called()
    updater._update_strategy_configs.assert_not_called()
    updater._save_to_db.assert_not_called()
    assert config.read_text()=='allocated_amount: 100\n'

@pytest.mark.asyncio
@pytest.mark.parametrize('balance',[None,0,-1,float('nan'),float('inf')])
async def test_failed_balance_has_no_config_capital_fallback(balance,monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr('ai_tuner.allocation.monthly_job.allocation_frozen',AsyncMock(return_value=False))
    job=MonthlyAllocationJob({'capital_allocation':{'enabled':True,'total_capital':1000,'reserve_ratio':.1,'rank_ratios':[.3]}},AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock())
    job._ensure_table=AsyncMock();job._check_idempotency=AsyncMock(return_value=False)
    job._check_is_first_month=AsyncMock(return_value=False)
    job.pnl_collector=NS(collect_all_realized_pnl=AsyncMock(return_value={'hrs':{'pnl':1}}))
    job._get_actual_balance=AsyncMock(return_value=balance)
    job.calculator=NS(calculate=MagicMock())
    assert await job.run_monthly_allocation('2026-10','2026-11') is None
    job.calculator.calculate.assert_not_called()

@pytest.mark.asyncio
async def test_freeze_read_errors_fail_closed_before_any_writer(monkeypatch):
    for module in ('monthly_job','daily_refresher','config_updater'):
        monkeypatch.setattr(f'ai_tuner.allocation.{module}.allocation_frozen',AsyncMock(side_effect=RuntimeError('unavailable')))
    db=AsyncMock();api=AsyncMock()
    job=MonthlyAllocationJob({},db,None,None,None,None,api)
    job._ensure_table=AsyncMock()
    refresher=DailyAllocationRefresher({},db,api)
    updater=AllocationConfigUpdater();updater._save_to_db=AsyncMock()
    assert await job.run_monthly_allocation() is None
    assert await refresher.run_daily_refresh() is None
    assert await updater.update_all(NS(month='2026-11'),{},db,None,None) is False
    assert db.mock_calls==[] and api.mock_calls==[]
    job._ensure_table.assert_not_called();updater._save_to_db.assert_not_called()

@pytest.mark.asyncio
async def test_updater_retained_downstream_flow_only_with_explicit_test_gate(monkeypatch):
    # Exercises both edges of the newly added gate; production helper is always true.
    monkeypatch.setattr('ai_tuner.allocation.config_updater.allocation_frozen',AsyncMock(return_value=False))
    updater=AllocationConfigUpdater()
    updater._update_tuner_config=AsyncMock(return_value=True)
    updater._update_strategy_configs=AsyncMock(return_value=True)
    updater._save_to_db=AsyncMock(return_value=True)
    assert await updater.update_all(NS(month='2026-11'),{},None,None,None)
    updater._save_to_db.assert_awaited_once()
