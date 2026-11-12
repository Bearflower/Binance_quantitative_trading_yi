"""Evidence-only assessments for the two temporary reduced-risk live trials.

This module must never activate policy revisions, submit exchange writes, or
translate missing evidence into a zero-valued performance observation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from typing import Any, Mapping

from apscheduler.triggers.cron import CronTrigger
from shared.accounting.income_integrity import income_account_lock, require_income_integrity
from shared.accounting.published_report import REPORT_VERSION
from shared.accounting.report_reader import read_monthly_economic_report


BEIJING = timezone(timedelta(hours=8))
TRIAL_STRATEGIES = ("btc_eth_aggressive", "hrs")


class NotificationState:
    """Per-job durable delivery fingerprint on the existing AI Tuner logs mount."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("unsafe notification state path")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("notification state unreadable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("signature"), str):
            raise ValueError("notification state malformed")
        return payload

    def changed(self, signature: str) -> bool:
        return self._read().get("signature") != signature

    def mark_delivered(self, signature: str, now: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("unsafe notification state path")
        temporary = self.path.with_name(self.path.name + "." + str(os.getpid()) + ".tmp")
        payload = json.dumps({"signature": signature, "delivered_at": _utc(now).isoformat()},
                             sort_keys=True, separators=(",", ":"))
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("aware datetime required")
    return value.astimezone(timezone.utc)


def daily_window(now: datetime) -> tuple[datetime, datetime]:
    """Previous completed Beijing calendar day, as UTC timestamps."""
    local = _utc(now).astimezone(BEIJING)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (end - timedelta(days=1)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def weekly_window(now: datetime) -> tuple[datetime, datetime]:
    """Previous completed Monday-to-Monday Beijing week, as UTC timestamps."""
    local = _utc(now).astimezone(BEIJING)
    end = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (end - timedelta(days=7)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def _amount(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _yes(value: Any) -> bool:
    return value is True or value == "true"


def inspect_exchange(positions: Any, orders: Any, algos: Any,
                     owned_symbols: set[str]) -> dict:
    """Check a strategy's claimed live positions against full adverse STOP size."""
    empty = {"verified": False, "owned_positions": [], "unprotected_symbols": [],
             "owned_margin": None}
    if not all(isinstance(rows, list) for rows in (positions, orders, algos)):
        return empty
    owned: list[str] = []
    unprotected: list[str] = []
    owned_margin: Decimal | None = Decimal(0)
    for position in positions:
        if not isinstance(position, Mapping):
            return empty
        amount = _amount(position.get("positionAmt"))
        if amount is None:
            return empty
        symbol = position.get("symbol")
        if not amount or symbol not in owned_symbols:
            continue
        mark = _amount(position.get("markPrice"))
        if mark is None or mark <= 0:
            return empty
        owned.append(symbol)
        leverage = _amount(position.get("leverage"))
        contract_size = _amount(position.get("contractSize"))
        if leverage is None or leverage <= 0 or contract_size is None or contract_size <= 0:
            owned_margin = None
        elif owned_margin is not None:
            owned_margin += abs(amount) * mark * contract_size / leverage
        covered = Decimal(0)
        close_all = False
        seen: set[str] = set()
        for order in orders + algos:
            if not isinstance(order, Mapping):
                return empty
            if (order.get("symbol") != symbol
                    or order.get("side") != ("SELL" if amount > 0 else "BUY")
                    or order.get("positionSide", "BOTH") != position.get("positionSide", "BOTH")
                    or order.get("type", order.get("orderType")) not in ("STOP", "STOP_MARKET")):
                continue
            stop = _amount(order.get("stopPrice", order.get("triggerPrice")))
            if stop is None or stop <= 0:
                return empty
            if (amount > 0 and stop >= mark) or (amount < 0 and stop <= mark):
                continue
            identity = str(order.get("orderId", order.get("algoId", "")))
            if not identity or identity in seen:
                return empty
            seen.add(identity)
            if _yes(order.get("closePosition")):
                close_all = True
            elif _yes(order.get("reduceOnly")):
                qty = _amount(order.get("origQty", order.get("quantity")))
                filled = _amount(order.get("executedQty", "0"))
                if qty is None or filled is None or qty < filled or filled < 0:
                    return empty
                covered += qty - filled
        if not close_all and covered < abs(amount):
            unprotected.append(symbol)
    return {"verified": True, "owned_positions": sorted(owned),
            "unprotected_symbols": sorted(unprotected), "owned_margin": owned_margin}


_POLICY_SQL = """SELECT p.* FROM trading.strategy_risk_policy p
JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
WHERE p.account_id=$1 AND p.strategy_id=$2"""
_RESERVATION_SQL = """SELECT count(*) AS count, COALESCE(sum(margin),0) AS margin,
count(*) FILTER (WHERE pressure_risk IS NULL) AS pressure_missing,
sum(pressure_risk) AS pressure_risk
FROM trading.entry_risk_reservation
WHERE account_id=$1 AND strategy_id=$2 AND state<>'released'"""
_CLAIMS_SQL = """SELECT symbol,strategy FROM trading.position_claims
WHERE claim_state IN ('PENDING','ACTIVE')"""
_AGGRESSIVE_SIGNALS_SQL = """SELECT count(*) AS real_signals,
count(*) FILTER (WHERE status='executed') AS executed_signals,
count(*) FILTER (WHERE status='cancelled') AS unresolved_signals,
count(*) FILTER (WHERE status='executed' AND
  NOT (metadata ? 'entry_order_id')) AS executed_without_order_id,
count(*) FILTER (WHERE status='cancelled' AND
  (metadata->>'skip_reason' ILIKE '%take profit or remainder infeasible%'
   OR metadata->>'reject_reason' ILIKE '%take profit or remainder infeasible%'))
  AS infeasible_entry_signals,
0::bigint AS shadow_signals
FROM btc_eth_aggressive.trade_signals WHERE created_at >= $1 AND created_at < $2"""
_HRS_SIGNALS_SQL = """SELECT
count(*) FILTER (WHERE signal_origin IS DISTINCT FROM 'shadow_scoring' AND
  (entry_mode='standard' OR
   (entry_mode='lv_rm' AND signal_origin='nonempty_pool'))) AS real_signals,
count(*) FILTER (WHERE resolution='shadow' OR signal_origin='shadow_scoring' OR
  entry_mode IN ('emm','semi_emm') OR
  (entry_mode='lv_rm' AND signal_origin IS DISTINCT FROM 'nonempty_pool')) AS shadow_signals,
count(*) FILTER (WHERE resolution='executed') AS executed_signals,
count(*) FILTER (WHERE resolution='executed' AND NOT EXISTS (
  SELECT 1 FROM trading.entry_risk_reservation r
  WHERE r.account_id=$3 AND r.strategy_id='hrs'
    AND r.symbol=hrs.hrs_signals.symbol
    AND r.geometry->>'signal_id'=hrs.hrs_signals.id::text
)) AS executed_without_reservation,
count(*) FILTER (WHERE resolution IS NULL AND entry_mode='standard') AS unresolved_signals
FROM hrs.hrs_signals WHERE triggered_at >= $1 AND triggered_at < $2"""


class _SnapshotDB:
    def __init__(self, connection: Any):
        self.connection = connection

    async def fetch_all(self, query: str, *args: Any) -> list:
        return await self.connection.fetch(query, *args)


async def read_trial_income(db: Any, account: str, start_ms: int, end_ms: int,
                            *, include_reviewed_overrides: bool = True) -> dict:
    """Read canonical exchange-income facts under one immutable DB snapshot."""
    async with db.pool.acquire() as connection:
        async with income_account_lock(connection, account):
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                await require_income_integrity(connection, account)
                return await read_monthly_economic_report(
                    _SnapshotDB(connection), account, start_ms, end_ms,
                    include_reviewed_overrides=include_reviewed_overrides,
                    include_derived_attribution=True,
                )


async def collect_snapshot(db: Any, exchange: Any, account: str, strategy: str,
                           start: datetime, end: datetime,
                           *, trial_start: datetime | None = None,
                           income_reader=read_trial_income) -> dict:
    """Collect one fixed trial window. All database statements are SELECTs."""
    if strategy not in TRIAL_STRATEGIES or not account or _utc(start) >= _utc(end):
        raise ValueError("invalid trial identity or window")
    evidence: dict[str, Any] = {
        "account_id": account, "strategy_id": strategy,
        "window_start": _utc(start).isoformat(), "window_end": _utc(end).isoformat(),
        "policy": None, "reservations": None, "exchange": {"verified": False},
        "income": None, "signals": None, "source_errors": [],
    }
    try:
        evidence["policy"] = await db.fetch_one(_POLICY_SQL, account, strategy)
    except Exception as exc:
        evidence["source_errors"].append("policy:" + type(exc).__name__)
    if trial_start is not None and _utc(trial_start) > _utc(start):
        start = _utc(trial_start)
        evidence["window_start"] = start.isoformat()
    has_window = _utc(start) < _utc(end)
    if not has_window:
        evidence["source_errors"].append("trial_window_precedes_policy")
    try:
        evidence["reservations"] = await db.fetch_one(_RESERVATION_SQL, account, strategy)
    except Exception as exc:
        evidence["source_errors"].append("reservations:" + type(exc).__name__)
    if has_window:
        try:
            if strategy == "hrs":
                evidence["signals"] = await db.fetch_one(
                    _HRS_SIGNALS_SQL, _utc(start), _utc(end), account)
            else:
                # The MTPCS signal table uses a PostgreSQL DEFAULT CURRENT_TIMESTAMP
                # on a timestamp-without-time-zone column. Production PostgreSQL runs
                # in UTC; only executed_at is written as Beijing-naive by the app.
                left = _utc(start).replace(tzinfo=None)
                right = _utc(end).replace(tzinfo=None)
                evidence["signals"] = await db.fetch_one(_AGGRESSIVE_SIGNALS_SQL, left, right)
        except Exception as exc:
            evidence["source_errors"].append("signals:" + type(exc).__name__)
    if exchange is not None:
        try:
            claims = await db.fetch_all(_CLAIMS_SQL)
            if account != "default":
                raise ValueError("position_claims has no account identity")
            positions = await exchange.get_position()
            orders = await exchange.get_open_orders()
            algos = await exchange.get_open_algo_orders()
            all_claimed = {row["symbol"] for row in claims}
            owned = {row["symbol"] for row in claims if row["strategy"] == strategy}
            result = inspect_exchange(positions, orders, algos, owned)
            if result["verified"]:
                result["ownership_complete"] = all(
                    _amount(row.get("positionAmt")) == 0 or row.get("symbol") in all_claimed
                    for row in positions
                )
            evidence["exchange"] = result
        except Exception as exc:
            evidence["source_errors"].append("exchange:" + type(exc).__name__)
    if has_window:
        try:
            start_ms, end_ms = int(_utc(start).timestamp() * 1000), int(_utc(end).timestamp() * 1000)
            report = await income_reader(
                db, account, start_ms, end_ms, include_reviewed_overrides=True
            )
            if (not isinstance(report, Mapping)
                    or report.get("account_id") != account
                    or report.get("start_ms") != start_ms
                    or report.get("end_ms") != end_ms
                    or report.get("report_version") != REPORT_VERSION
                    or not isinstance(report.get("report_id"), str)
                    or not report["report_id"]):
                raise ValueError("exchange income report identity mismatch")
            evidence["income"] = report
        except Exception as exc:
            evidence["source_errors"].append("income:" + type(exc).__name__)
    return evidence


def assess_snapshot(evidence: Mapping[str, Any], *, now: datetime, weekly: bool) -> dict:
    """Classify observed safety failures before incomplete data.

    `REVIEW_CANDIDATE` is intentionally unreachable until versioned live
    drawdown, pressure-loss and threshold evidence is supplied by a later
    reviewed data contract. This job is an observer, not a risk authority.
    """
    now = _utc(now)
    safety: list[str] = []
    missing: list[str] = []
    policy = evidence.get("policy")
    if not isinstance(policy, Mapping):
        safety.append("policy_missing")
    else:
        if (policy.get("account_id", evidence.get("account_id")) != evidence.get("account_id")
                or policy.get("strategy_id", evidence.get("strategy_id")) != evidence.get("strategy_id")):
            safety.append("policy_identity_mismatch")
        effective = policy.get("effective_at")
        try:
            effective = _utc(effective)
        except ValueError:
            missing.append("policy_effective_unverified")
        else:
            if effective > now:
                safety.append("policy_not_effective")
        expiry = policy.get("expires_at")
        try:
            expiry = _utc(expiry)
        except ValueError:
            missing.append("policy_expiry_unverified")
        else:
            if expiry <= now:
                safety.append("policy_expired")
            elif expiry - now <= timedelta(hours=48):
                safety.append("policy_expiring_48h")
        multiplier = _amount(policy.get("multiplier"))
        ratio_cap = _amount(policy.get("ratio_cap"))
        ceiling = _amount(policy.get("entry_ceiling"))
        if (policy.get("mode") != "reduced" or policy.get("allow_add") is not False
                or multiplier is None or multiplier > Decimal("0.5") or multiplier < 0
                or ratio_cap is None or ratio_cap > Decimal("0.15") or ratio_cap < 0):
            safety.append("policy_not_reduced")
        if policy.get("freeze_allocation") is not True:
            safety.append("allocation_not_frozen")
        if ceiling is None or ceiling < 0:
            missing.append("policy_ceiling_unverified")
        cohort_ceiling = _amount(evidence.get("cohort_ceiling"))
        if cohort_ceiling is None or cohort_ceiling <= 0:
            missing.append("cohort_ceiling_unverified")
        elif ceiling is not None and ceiling > cohort_ceiling:
            safety.append("ceiling_widened")

    reservations = evidence.get("reservations")
    if not isinstance(reservations, Mapping):
        missing.append("reservations_unverified")
    else:
        margin = _amount(reservations.get("margin"))
        pending_count = reservations.get("count")
        if not isinstance(pending_count, int) or pending_count < 0:
            missing.append("reservation_count_unverified")
        elif pending_count and (not isinstance(reservations.get("pressure_missing"), int)
                                or reservations["pressure_missing"] > 0
                                or _amount(reservations.get("pressure_risk")) is None):
            missing.append("pressure_risk_unverified")
        if margin is None or margin < 0:
            missing.append("reservation_margin_unverified")
        elif isinstance(policy, Mapping):
            ceiling = _amount(policy.get("entry_ceiling"))
            if ceiling is not None and margin > ceiling:
                safety.append("reservation_exceeds_ceiling")

    exchange = evidence.get("exchange")
    if not isinstance(exchange, Mapping) or exchange.get("verified") is not True:
        missing.append("exchange_unverified")
    else:
        unprotected = exchange.get("unprotected_symbols")
        if not isinstance(unprotected, list):
            missing.append("protection_unverified")
        else:
            safety.extend("unprotected_position:" + str(symbol) for symbol in unprotected)
        if exchange.get("ownership_complete") is False:
            missing.append("position_ownership_incomplete")
        owned_positions = exchange.get("owned_positions")
        if not isinstance(owned_positions, list):
            missing.append("owned_positions_unverified")
        elif owned_positions:
            holdings_margin = _amount(exchange.get("owned_margin"))
            if holdings_margin is None or holdings_margin < 0:
                missing.append("holdings_margin_unverified")
            elif isinstance(reservations, Mapping) and isinstance(policy, Mapping):
                reserved = _amount(reservations.get("margin"))
                ceiling = _amount(policy.get("entry_ceiling"))
                if reserved is not None and ceiling is not None and holdings_margin + reserved > ceiling:
                    safety.append("exposure_exceeds_ceiling")

    signals = evidence.get("signals")
    if not isinstance(signals, Mapping) or any(
        not isinstance(signals.get(key), int) or signals[key] < 0
        for key in ("real_signals", "shadow_signals", "executed_signals", "unresolved_signals")
    ):
        missing.append("signals_unverified")
    elif signals["real_signals"] > 0 and evidence.get("execution_funnel_verified") is not True:
        missing.append("execution_funnel_unverified")
    if (isinstance(signals, Mapping) and evidence.get('strategy_id') == 'btc_eth_aggressive'
            and isinstance(signals.get('real_signals'), int)
            and isinstance(signals.get('infeasible_entry_signals'), int)
            and signals['real_signals'] > 0
            and signals['infeasible_entry_signals'] >= signals['real_signals']):
        safety.append('all_signals_entry_infeasible')
    if (isinstance(signals, Mapping) and evidence.get('strategy_id') == 'btc_eth_aggressive'
            and isinstance(signals.get('executed_without_order_id'), int)
            and signals['executed_without_order_id'] > 0):
        missing.append('executed_signal_order_identity_missing')
    if (isinstance(signals, Mapping) and evidence.get('strategy_id') == 'hrs'
            and isinstance(signals.get('executed_without_reservation'), int)
            and signals['executed_without_reservation'] > 0):
        missing.append('executed_signal_reservation_identity_missing')

    income = evidence.get("income")
    net = None
    if (not isinstance(income, Mapping)
            or income.get("account_coverage_complete") is not True
            or income.get("strategy_attribution_complete") is not True):
        missing.append("income_unverified")
    else:
        bucket = income.get("by_strategy", {}).get(evidence.get("strategy_id"), {})
        value = bucket.get("net_usdt", "0")
        parsed = _amount(value)
        if parsed is None:
            missing.append("strategy_net_unverified")
        else:
            net = str(parsed)

    if weekly:
        # No verified per-strategy equity curve, cash-flow bridge, or frozen
        # live-trial thresholds exists yet. These are mandatory for relaxation.
        missing.extend(("drawdown_unavailable", "pressure_loss_unavailable",
                        "thresholds_unfrozen", "two_holdout_months_unverified"))

    reasons = sorted(set(safety + missing))
    status = "SAFETY_REVIEW" if safety else ("BLOCKED_DATA" if missing else "KEEP_REDUCED")
    return {
        "account_id": evidence.get("account_id"),
        "strategy_id": evidence.get("strategy_id"),
        "policy_revision": policy.get("revision") if isinstance(policy, Mapping) else None,
        "status": status,
        "reasons": reasons,
        "net_usdt": net,
        "report_id": income.get("report_id") if isinstance(income, Mapping) else None,
        "signals": dict(signals) if isinstance(signals, Mapping) else None,
        "as_of": now.isoformat(),
    }


class ReducedTrialEvaluator:
    """Run evidence-only calendar reviews from the AI Tuner process."""

    def __init__(self, db: Any, exchange: Any, messenger: Any, state_dir: Path,
                 *, account: str = "default",
                 trial_starts: Mapping[str, datetime] | None = None,
                 cohort_ceilings: Mapping[str, Decimal] | None = None) -> None:
        self.db = db
        self.exchange = exchange
        self.messenger = messenger
        self.state_dir = Path(state_dir)
        self.account = account
        self.trial_starts = dict(trial_starts or {})
        self.cohort_ceilings = dict(cohort_ceilings or {})

    def _write_report(self, period: str, strategy: str, end: datetime,
                      assessment: dict, evidence: dict) -> None:
        folder = self.state_dir / "reports" / (period + "-" + _utc(end).strftime("%Y%m%dT%H%M%SZ"))
        folder.mkdir(parents=True, exist_ok=True)
        if folder.is_symlink():
            raise ValueError("unsafe trial report directory")
        path = folder / (strategy + ".json")
        temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
        policy = evidence.get("policy") or {}
        income = evidence.get("income") or {}
        report = {
            **assessment,
            "window_start": evidence.get("window_start"),
            "window_end": evidence.get("window_end"),
            "policy_expires_at": str(policy.get("expires_at")) if policy else None,
            "policy_mode": policy.get("mode"),
            "reservations": evidence.get("reservations"),
            "exchange": evidence.get("exchange"),
            "income_account_complete": income.get("account_coverage_complete"),
            "income_attribution_complete": income.get("strategy_attribution_complete"),
            "income_gaps": income.get("coverage_gaps"),
            "unattributed_count": income.get("unattributed_count"),
            "source_errors": evidence.get("source_errors", []),
            "decision_scope": "evidence_only_no_policy_write",
        }
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, sort_keys=True, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def _run(self, period: str, *, now: datetime | None = None) -> list[dict]:
        now = _utc(now or datetime.now(timezone.utc))
        start, end = daily_window(now) if period == "daily" else weekly_window(now)
        results = []
        for strategy in TRIAL_STRATEGIES:
            evidence = await collect_snapshot(self.db, self.exchange, self.account,
                                              strategy, start, end,
                                              trial_start=self.trial_starts.get(strategy))
            evidence["cohort_ceiling"] = self.cohort_ceilings.get(
                strategy, evidence.get("cohort_ceiling")
            )
            assessment = assess_snapshot(evidence, now=now, weekly=period == "weekly")
            self._write_report(period, strategy, end, assessment, evidence)
            results.append(assessment)
            state = NotificationState(self.state_dir / "state" / (period + "-" + strategy + ".json"))
            if period == "daily":
                signature = json.dumps([assessment["status"], assessment["policy_revision"],
                                        assessment["reasons"]], sort_keys=True)
                previous = state._read()
                if not state.changed(signature):
                    continue
                if assessment["status"] == "KEEP_REDUCED" and not previous:
                    state.mark_delivered(signature, now)
                    continue
            else:
                signature = end.isoformat()
                if not state.changed(signature):
                    continue
            net = assessment["net_usdt"] if assessment["net_usdt"] is not None else "UNKNOWN"
            signals = assessment.get("signals") or {}
            reservations = evidence.get("reservations") or {}
            exchange = evidence.get("exchange") or {}
            content = (f"策略: {strategy}\n政策 revision: {assessment['policy_revision']}\n"
                       f"窗口: {evidence['window_start']} 至 {evidence['window_end']}\n"
                       f"结论: {assessment['status']}\n交易所净收入: {net} USDT\n"
                       f"实盘/影子信号: {signals.get('real_signals', 'UNKNOWN')}/"
                       f"{signals.get('shadow_signals', 'UNKNOWN')}；记录为已执行: "
                       f"{signals.get('executed_signals', 'UNKNOWN')}\n"
                       f"未释放预留: {reservations.get('count', 'UNKNOWN')}；"
                       f"保证金: {reservations.get('margin', 'UNKNOWN')} USDT；"
                       f"压力风险: {reservations.get('pressure_risk', 'UNKNOWN')}\n"
                       f"交易所已归属持仓: {exchange.get('owned_positions', 'UNKNOWN')}；"
                       f"无全量止损: {exchange.get('unprotected_symbols', 'UNKNOWN')}\n"
                       f"报告 ID: {assessment['report_id'] or 'UNKNOWN'}\n"
                       f"证据/问题: {', '.join(assessment['reasons']) or '无'}\n"
                       "仅供复审，不可自动放宽、续期或下单。")
            title = "【AI Tuner 缩量试跑" + ("周评估" if period == "weekly" else "日巡检") + "】"
            level = "error" if assessment["status"] == "SAFETY_REVIEW" else "warning"
            delivered = await self.messenger.send_alert(title, content, level=level)
            if delivered:
                state.mark_delivered(signature, now)
        return results

    async def run_daily(self, *, now: datetime | None = None) -> list[dict]:
        try:
            return await self._run("daily", now=now)
        except Exception as exc:
            try:
                await self.messenger.send_alert(
                    "【AI Tuner 缩量试跑日巡检失败】",
                    "评估未完成；错误类型: " + type(exc).__name__ + "。请检查服务日志。",
                    level="error",
                )
            except Exception:
                pass
            raise

    async def run_weekly(self, *, now: datetime | None = None) -> list[dict]:
        try:
            return await self._run("weekly", now=now)
        except Exception as exc:
            try:
                await self.messenger.send_alert(
                    "【AI Tuner 缩量试跑周评估失败】",
                    "评估未完成；错误类型: " + type(exc).__name__ + "。请检查服务日志。",
                    level="error",
                )
            except Exception:
                pass
            raise


def register_reduced_trial_jobs(scheduler: Any, evaluator: ReducedTrialEvaluator,
                                config: Mapping[str, Any]) -> None:
    """Register bounded calendar jobs; these callbacks cannot mutate policy."""
    if config.get("enabled") is not True:
        return
    zone = config.get("timezone", "Asia/Shanghai")
    for cadence, method, default in (
        ("daily", evaluator.run_daily, "15 10 * * *"),
        ("weekly", evaluator.run_weekly, "15 11 * * mon"),
    ):
        trigger = CronTrigger.from_crontab(config.get(cadence + "_cron", default), timezone=zone)
        scheduler.add_job(
            method, trigger=trigger, id="reduced_trial_" + cadence,
            name="缩量试跑" + ("日巡检" if cadence == "daily" else "周评估"),
            replace_existing=True, max_instances=1, coalesce=True,
            misfire_grace_time=3600,
        )
