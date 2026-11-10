"""Trial review never treats missing exchange evidence as permission to add risk."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncio
import pytest

from ai_tuner.monitor.reduced_trial import (
    assess_snapshot, daily_window, weekly_window, inspect_exchange,
    collect_snapshot,
    NotificationState,
    ReducedTrialEvaluator,
    register_reduced_trial_jobs,
    read_trial_income,
)


UTC = timezone.utc
NOW = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)


def snapshot():
    return {
        "strategy_id": "hrs", "account_id": "default",
        "policy": {"account_id": "default", "strategy_id": "hrs",
                   "revision": 3, "mode": "reduced", "multiplier": Decimal("0.5"),
                   "allow_add": False, "entry_ceiling": Decimal("26.18"),
                   "ratio_cap": Decimal("0.15"),
                   "effective_at": NOW - timedelta(days=3),
                   "freeze_allocation": True,
                   "expires_at": NOW + timedelta(days=3)},
        "reservations": {"count": 0, "margin": Decimal("0")},
        "cohort_ceiling": Decimal("26.18"),
        "exchange": {"verified": True, "owned_positions": [], "unprotected_symbols": []},
        "signals": {"real_signals": 0, "shadow_signals": 0,
                    "executed_signals": 0, "unresolved_signals": 0},
        "income": {"account_coverage_complete": True,
                   "strategy_attribution_complete": True,
                   "by_strategy": {"hrs": {"net_usdt": "-1.25"}},
                   "report_id": "test-report", "coverage_gaps": []},
    }


def test_windows_are_fixed_beijing_calendar_periods():
    assert daily_window(NOW) == (
        datetime(2026, 11, 8, 16, tzinfo=UTC),
        datetime(2026, 11, 9, 16, tzinfo=UTC),
    )
    assert weekly_window(NOW) == (
        datetime(2026, 11, 1, 16, tzinfo=UTC),
        datetime(2026, 11, 8, 16, tzinfo=UTC),
    )


def test_expired_policy_requires_safety_review():
    evidence = snapshot()
    evidence["policy"]["expires_at"] = NOW
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "policy_expired" in result["reasons"]


def test_future_or_unfrozen_policy_requires_safety_review():
    evidence = snapshot()
    evidence["policy"]["effective_at"] = NOW + timedelta(hours=1)
    evidence["policy"]["freeze_allocation"] = False
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "policy_not_effective" in result["reasons"]
    assert "allocation_not_frozen" in result["reasons"]


def test_policy_ceiling_widened_above_frozen_cohort_requires_review():
    evidence = snapshot()
    evidence["policy"]["entry_ceiling"] = Decimal("30")
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "ceiling_widened" in result["reasons"]


def test_unprotected_owned_position_requires_safety_review():
    evidence = snapshot()
    evidence["exchange"]["owned_positions"] = ["ONUSDT"]
    evidence["exchange"]["unprotected_symbols"] = ["ONUSDT"]
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "unprotected_position:ONUSDT" in result["reasons"]


def test_missing_exchange_evidence_blocks_data_not_healthy():
    evidence = snapshot()
    evidence["exchange"] = {"verified": False}
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "BLOCKED_DATA"
    assert "exchange_unverified" in result["reasons"]


def test_incomplete_income_blocks_net_and_relaxation():
    evidence = snapshot()
    evidence["income"]["strategy_attribution_complete"] = False
    result = assess_snapshot(evidence, now=NOW, weekly=True)
    assert result["status"] == "BLOCKED_DATA"
    assert result["net_usdt"] is None


def test_weekly_with_missing_drawdown_and_thresholds_never_recommends_relaxation():
    result = assess_snapshot(snapshot(), now=NOW, weekly=True)
    assert result["status"] == "BLOCKED_DATA"
    assert "drawdown_unavailable" in result["reasons"]
    assert "thresholds_unfrozen" in result["reasons"]
    assert result["net_usdt"] == "-1.25"


def test_budget_exceeded_counts_unreleased_reservation():
    evidence = snapshot()
    evidence["reservations"]["margin"] = Decimal("26.19")
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "reservation_exceeds_ceiling" in result["reasons"]


def test_unpriced_pending_pressure_risk_blocks_trial_review():
    evidence = snapshot()
    evidence["reservations"] = {"count": 1, "margin": Decimal("1"),
                                "pressure_missing": 1, "pressure_risk": None}
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "BLOCKED_DATA"
    assert "pressure_risk_unverified" in result["reasons"]


def test_open_position_without_verified_margin_blocks_budget_conclusion():
    evidence = snapshot()
    evidence["exchange"]["owned_positions"] = ["ONUSDT"]
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "BLOCKED_DATA"
    assert "holdings_margin_unverified" in result["reasons"]


def test_verified_holdings_plus_reservation_above_ceiling_requires_review():
    evidence = snapshot()
    evidence["exchange"]["owned_positions"] = ["ONUSDT"]
    evidence["exchange"]["owned_margin"] = Decimal("20")
    evidence["reservations"]["margin"] = Decimal("7")
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "exposure_exceeds_ceiling" in result["reasons"]


def test_missing_policy_is_safety_review():
    evidence = snapshot()
    evidence["policy"] = None
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "SAFETY_REVIEW"
    assert "policy_missing" in result["reasons"]


def test_missing_signal_ledger_blocks_coverage_conclusion():
    evidence = snapshot()
    evidence["signals"] = None
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "BLOCKED_DATA"
    assert "signals_unverified" in result["reasons"]


def test_real_signal_without_order_funnel_evidence_blocks_health_claim():
    evidence = snapshot()
    evidence["signals"]["real_signals"] = 1
    result = assess_snapshot(evidence, now=NOW, weekly=False)
    assert result["status"] == "BLOCKED_DATA"
    assert "execution_funnel_unverified" in result["reasons"]


def test_claimed_position_needs_full_adverse_stop():
    positions = [{"symbol": "ONUSDT", "positionAmt": "3", "positionSide": "BOTH",
                  "entryPrice": "1", "markPrice": "1"}]
    no_stop = inspect_exchange(positions, [], [], {"ONUSDT"})
    assert no_stop["verified"] is True
    assert no_stop["unprotected_symbols"] == ["ONUSDT"]
    stop = {"symbol": "ONUSDT", "side": "SELL", "positionSide": "BOTH",
            "orderType": "STOP_MARKET", "triggerPrice": "0.8", "closePosition": "true",
            "algoId": 123}
    protected = inspect_exchange(positions, [], [stop], {"ONUSDT"})
    assert protected["unprotected_symbols"] == []


def test_partial_stop_does_not_count_as_full_protection():
    positions = [{"symbol": "ONUSDT", "positionAmt": "3", "positionSide": "BOTH",
                  "entryPrice": "1", "markPrice": "1"}]
    stop = {"symbol": "ONUSDT", "side": "SELL", "positionSide": "BOTH",
            "type": "STOP", "stopPrice": "0.8", "reduceOnly": "true",
            "origQty": "2", "executedQty": "0", "orderId": 124}
    result = inspect_exchange(positions, [stop], [], {"ONUSDT"})
    assert result["unprotected_symbols"] == ["ONUSDT"]


def test_exchange_margin_only_verified_with_complete_contract_fields():
    position = {"symbol": "ONUSDT", "positionAmt": "3", "positionSide": "BOTH",
                "entryPrice": "1", "markPrice": "2", "leverage": "3", "contractSize": "1"}
    stop = {"symbol": "ONUSDT", "side": "SELL", "positionSide": "BOTH",
            "type": "STOP", "stopPrice": "1", "closePosition": "true", "orderId": 125}
    result = inspect_exchange([position], [stop], [], {"ONUSDT"})
    assert result["owned_margin"] == Decimal("2")
    del position["contractSize"]
    assert inspect_exchange([position], [stop], [], {"ONUSDT"})["owned_margin"] is None


def test_exchange_inventory_missing_never_reports_verified():
    assert inspect_exchange(None, [], [], set())["verified"] is False


def test_missing_mark_price_cannot_certify_stop_protection():
    positions = [{"symbol": "ONUSDT", "positionAmt": "1", "entryPrice": "1"}]
    assert inspect_exchange(positions, [], [], {"ONUSDT"})["verified"] is False


def test_collector_uses_policy_claims_and_exchange_income_reader_without_writes():
    class DB:
        async def fetch_one(self, query, *args):
            if "strategy_risk_policy" in query:
                return snapshot()["policy"]
            if "entry_risk_reservation" in query:
                return {"count": 0, "margin": Decimal("0")}
            if "income_ingestion_attempt" in query:
                return {"unresolved": 0}
            if "hrs.hrs_signals" in query:
                assert "resolution='executed'" in query
                return {"real_signals": 1, "shadow_signals": 2,
                        "executed_signals": 0, "unresolved_signals": 1}
            raise AssertionError(query)

        async def fetch_all(self, query, *args):
            if "position_claims" in query:
                return [{"symbol": "ONUSDT", "strategy": "hrs"}]
            raise AssertionError(query)

    class Exchange:
        async def get_position(self):
            return [{"symbol": "ONUSDT", "positionAmt": "1", "positionSide": "BOTH",
                     "entryPrice": "1", "markPrice": "1"}]

        async def get_open_orders(self):
            return []

        async def get_open_algo_orders(self):
            return [{"symbol": "ONUSDT", "side": "SELL", "positionSide": "BOTH",
                     "orderType": "STOP_MARKET", "triggerPrice": "0.8",
                     "closePosition": "true", "algoId": 123}]

    async def income_reader(db, account, start_ms, end_ms, **kwargs):
        assert db.__class__ is DB
        assert account == "default"
        assert kwargs == {"include_reviewed_overrides": True}
        assert start_ms < end_ms
        return {**snapshot()["income"], "account_id": account,
                "start_ms": start_ms, "end_ms": end_ms,
                "report_version": "exchange-income-v1"}

    start, end = daily_window(NOW)
    evidence = asyncio.run(collect_snapshot(
        DB(), Exchange(), "default", "hrs", start, end,
        income_reader=income_reader,
    ))
    assert evidence["signals"]["real_signals"] == 1
    assert evidence["signals"]["shadow_signals"] == 2
    assert evidence["exchange"]["unprotected_symbols"] == []
    assert assess_snapshot(evidence, now=NOW, weekly=False)["status"] == "BLOCKED_DATA"


def test_collector_rejects_income_report_for_wrong_account():
    class DB:
        async def fetch_one(self, query, *args):
            if "strategy_risk_policy" in query:
                return snapshot()["policy"]
            if "entry_risk_reservation" in query:
                return {"count": 0, "margin": Decimal(0)}
            return {"real_signals": 0, "shadow_signals": 0,
                    "executed_signals": 0, "unresolved_signals": 0}

    async def wrong_reader(db, account, start_ms, end_ms, **kwargs):
        return {**snapshot()["income"], "account_id": "other",
                "start_ms": start_ms, "end_ms": end_ms,
                "report_version": "exchange-income-v1"}

    evidence = asyncio.run(collect_snapshot(
        DB(), None, "default", "hrs", *daily_window(NOW),
        income_reader=wrong_reader,
    ))
    assert evidence["income"] is None
    assert "income:ValueError" in evidence["source_errors"]


def test_collector_missing_exchange_stays_unknown():
    class DB:
        async def fetch_one(self, query, *args):
            if "strategy_risk_policy" in query:
                return snapshot()["policy"]
            if "entry_risk_reservation" in query:
                return {"count": 0, "margin": Decimal("0")}
            if "income_ingestion_attempt" in query:
                return {"unresolved": 0}
            return {"real_signals": 0, "shadow_signals": 0,
                    "executed_signals": 0, "unresolved_signals": 0}

        async def fetch_all(self, query, *args):
            return []

    async def income_reader(*args, **kwargs):
        return snapshot()["income"]

    evidence = asyncio.run(collect_snapshot(
        DB(), None, "default", "hrs", *daily_window(NOW),
        income_reader=income_reader,
    ))
    assert evidence["exchange"]["verified"] is False
    assert assess_snapshot(evidence, now=NOW, weekly=False)["status"] == "BLOCKED_DATA"


def test_income_reader_uses_one_read_only_repeatable_snapshot(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    class Connection:
        def __init__(self):
            self.transaction_args = None
            self.lock_calls = []

        async def execute(self, query, *args):
            self.lock_calls.append((query, args))

        async def fetchval(self, query, *args):
            return 0

        @asynccontextmanager
        async def transaction(self, **kwargs):
            self.transaction_args = kwargs
            yield self

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        @asynccontextmanager
        async def acquire(self):
            yield self.conn

    class DB:
        def __init__(self, conn):
            self.pool = Pool(conn)

    async def reader(snapshot_db, account, start_ms, end_ms, **kwargs):
        assert snapshot_db.connection is conn
        return {"report_id": "same-snapshot"}

    monkeypatch.setattr("ai_tuner.monitor.reduced_trial.read_monthly_economic_report", reader)
    conn = Connection()
    result = asyncio.run(read_trial_income(DB(conn), "default", 1, 2,
                                           include_reviewed_overrides=True))
    assert result == {"report_id": "same-snapshot"}
    assert conn.transaction_args == {"isolation": "repeatable_read", "readonly": True}
    assert len(conn.lock_calls) == 2


def test_trial_window_starts_at_frozen_cohort_time_across_policy_revisions():
    start, end = daily_window(NOW)
    effective = start + timedelta(hours=12)

    class DB:
        async def fetch_one(self, query, *args):
            if "strategy_risk_policy" in query:
                return {**snapshot()["policy"],
                        "effective_at": effective + timedelta(hours=2)}
            if "entry_risk_reservation" in query:
                return {"count": 0, "margin": Decimal(0)}
            return {"real_signals": 0, "shadow_signals": 0,
                    "executed_signals": 0, "unresolved_signals": 0}

    async def income_reader(db, account, start_ms, end_ms, **kwargs):
        assert start_ms == int(effective.timestamp() * 1000)
        return snapshot()["income"]

    evidence = asyncio.run(collect_snapshot(
        DB(), None, "default", "hrs", start, end,
        trial_start=effective, income_reader=income_reader,
    ))
    assert evidence["window_start"] == effective.isoformat()


def test_aggressive_signal_window_uses_database_utc_naive_created_at():
    """created_at is a PostgreSQL default timestamp, with production DB in UTC."""
    start, end = daily_window(NOW)

    class DB:
        async def fetch_one(self, query, *args):
            if "btc_eth_aggressive.trade_signals" in query:
                assert args == (start.replace(tzinfo=None), end.replace(tzinfo=None))
                return {"real_signals": 0, "shadow_signals": 0,
                        "executed_signals": 0, "unresolved_signals": 0}
            return None

    evidence = asyncio.run(collect_snapshot(
        DB(), None, "default", "btc_eth_aggressive", start, end,
        income_reader=lambda *a, **k: None,
    ))
    assert evidence["signals"]["real_signals"] == 0


def test_notification_state_deduplicates_daily_and_weekly_after_restart(tmp_path):
    path = tmp_path / "trial" / "daily-hrs.json"
    state = NotificationState(path)
    assert state.changed("expiry:revision3") is True
    state.mark_delivered("expiry:revision3", NOW)
    assert NotificationState(path).changed("expiry:revision3") is False
    assert NotificationState(path).changed("recovered:revision4") is True


def test_notification_state_rejects_corrupt_file(tmp_path):
    path = tmp_path / "trial" / "weekly-hrs.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        NotificationState(path).changed("2026-11-09")


def test_daily_job_persists_report_and_only_sends_on_change(tmp_path, monkeypatch):
    async def fake_collect(*args, **kwargs):
        result = snapshot()
        result["strategy_id"] = args[3]
        result["policy"]["strategy_id"] = args[3]
        result["window_start"], result["window_end"] = args[4].isoformat(), args[5].isoformat()
        result["income"] = {**result["income"], "by_strategy": {args[3]: {"net_usdt": "0"}}}
        return result

    class Messenger:
        def __init__(self):
            self.sent = []

        async def send_alert(self, title, content, level="warning"):
            self.sent.append((title, content, level))
            return True

    monkeypatch.setattr("ai_tuner.monitor.reduced_trial.collect_snapshot", fake_collect)
    messenger = Messenger()
    job = ReducedTrialEvaluator(object(), object(), messenger, tmp_path)
    first = asyncio.run(job.run_daily(now=NOW))
    second = asyncio.run(job.run_daily(now=NOW))
    assert len(first) == len(second) == 2
    assert messenger.sent == []  # first healthy observation is quiet
    assert len(list((tmp_path / "reports").rglob("*.json"))) == 2


def test_weekly_job_sends_once_per_completed_window(tmp_path, monkeypatch):
    async def fake_collect(*args, **kwargs):
        result = snapshot()
        result["strategy_id"] = args[3]
        result["policy"]["strategy_id"] = args[3]
        result["window_start"], result["window_end"] = args[4].isoformat(), args[5].isoformat()
        result["income"] = {**result["income"], "by_strategy": {args[3]: {"net_usdt": "0"}}}
        return result

    class Messenger:
        def __init__(self):
            self.sent = []

        async def send_alert(self, title, content, level="warning"):
            self.sent.append((title, content, level))
            return True

    monkeypatch.setattr("ai_tuner.monitor.reduced_trial.collect_snapshot", fake_collect)
    messenger = Messenger()
    job = ReducedTrialEvaluator(object(), object(), messenger, tmp_path)
    first = asyncio.run(job.run_weekly(now=NOW))
    second = asyncio.run(job.run_weekly(now=NOW))
    assert len(first) == len(second) == 2
    assert all(row["status"] == "BLOCKED_DATA" for row in first)
    assert len(messenger.sent) == 2
    assert all("不可自动放宽" in content for _, content, _ in messenger.sent)


def test_scheduler_registers_beijing_daily_and_monday_weekly_jobs():
    class Scheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, callback, trigger, **kwargs):
            self.jobs.append((callback, trigger, kwargs))

    class Job:
        async def run_daily(self):
            pass

        async def run_weekly(self):
            pass

    scheduler = Scheduler()
    register_reduced_trial_jobs(scheduler, Job(), {
        "enabled": True, "timezone": "Asia/Shanghai",
        "daily_cron": "15 10 * * *", "weekly_cron": "15 11 * * mon",
    })
    assert [item[2]["id"] for item in scheduler.jobs] == [
        "reduced_trial_daily", "reduced_trial_weekly",
    ]
    next_weekly = scheduler.jobs[1][1].get_next_fire_time(
        None, datetime(2026, 11, 8, 16, tzinfo=UTC)
    )
    assert next_weekly.astimezone(timezone(timedelta(hours=8))) == datetime(
        2026, 11, 9, 11, 15, tzinfo=timezone(timedelta(hours=8))
    )


def test_scheduler_failure_sends_error_alert_without_forging_report(tmp_path, monkeypatch):
    class Messenger:
        def __init__(self):
            self.alerts = []

        async def send_alert(self, title, content, level="warning"):
            self.alerts.append((title, content, level))
            return True

    async def broken(*args, **kwargs):
        raise ValueError("secret-details-should-not-leak")

    messenger = Messenger()
    job = ReducedTrialEvaluator(object(), object(), messenger, tmp_path)
    monkeypatch.setattr(job, "_run", broken)
    with pytest.raises(ValueError):
        asyncio.run(job.run_daily(now=NOW))
    assert len(messenger.alerts) == 1
    assert "ValueError" in messenger.alerts[0][1]
    assert "secret-details" not in messenger.alerts[0][1]
    assert not (tmp_path / "reports").exists()
