"""Pure persistent loss-gate policy for new risk only.

GateHistory.complete certifies continuous, reconciled observations, not just two
endpoints. The collector must establish the exact Asia/Shanghai midnight NAV;
an intraday first sample is never a substitute. A rolling window is the exact
preceding 30 * 24 hours of observed NAV checkpoints, so qualification
requires at least 30 elapsed days of actual coverage. An explicitly enabled inception
segment uses its genuine first NAV and is always labelled separately from 30d.

Store every returned state under the same DB lock as ledger checkpoints. Invalid
data never clears a latch. The explicit reset API preserves daily protection and
records evidence; it does not erase historical high-watermarks.
"""
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .risk_ledger import EquityCheckpoint, LedgerNotReady, aware, decimal_value

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_LIMIT = Decimal("0.03")
ROLLING_LIMIT = Decimal("0.06")


@dataclass(frozen=True)
class GateHistory:
    samples: tuple[EquityCheckpoint, ...]
    complete_from: datetime
    complete_through: datetime
    complete: bool
    daily_complete: bool | None = None


@dataclass(frozen=True)
class LossGateState:
    segment_id: str
    day: date
    day_origin_nav: Decimal | None = None
    daily_latched: bool = False
    rolling_latched: bool = False
    rolling_latch_scope: str | None = None
    revision: int = 0
    as_of: datetime | None = None
    reset_reason: str | None = None
    reset_evidence: str | None = None
    reset_at: datetime | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["day"] = self.day.isoformat()
        for key in ("as_of", "reset_at"):
            data[key] = data[key].isoformat() if data[key] is not None else None
        data["day_origin_nav"] = str(self.day_origin_nav) if self.day_origin_nav is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LossGateState":
        values = dict(data)
        values["day"] = date.fromisoformat(values["day"])
        for key in ("as_of", "reset_at"):
            values[key] = datetime.fromisoformat(values[key]) if values.get(key) is not None else None
        values["day_origin_nav"] = Decimal(values["day_origin_nav"]) if values.get("day_origin_nav") is not None else None
        return cls(**values)


@dataclass(frozen=True)
class GateEvaluation:
    state: LossGateState
    allow_new_risk: bool
    ready: bool
    reasons: tuple[str, ...]
    window_label: str | None
    qualified_30_day: bool
    daily_return: Decimal | None = None
    rolling_drawdown: Decimal | None = None


def _validate_checkpoint(sample: EquityCheckpoint, segment_id: str) -> None:
    aware(sample.as_of, "checkpoint")
    if sample.segment_id != segment_id:
        raise LedgerNotReady("segment_mismatch")
    for field in ("equity", "units", "nav"):
        decimal_value(getattr(sample, field), field, positive=True)
    if sample.equity / sample.units != sample.nav:
        raise LedgerNotReady("inconsistent_checkpoint_nav")


def _validated_history(history: GateHistory, current: EquityCheckpoint):
    aware(history.complete_from, "history_start")
    aware(history.complete_through, "history_end")
    if history.complete_from > history.complete_through or history.complete_through > current.as_of:
        raise LedgerNotReady("invalid_history_coverage")
    by_time = {}
    for sample in history.samples:
        _validate_checkpoint(sample, current.segment_id)
        if sample.as_of > current.as_of:
            raise LedgerNotReady("future_history")
        if sample.as_of in by_time and by_time[sample.as_of] != sample:
            raise LedgerNotReady("conflicting_history")
        by_time[sample.as_of] = sample
    if current.as_of in by_time and by_time[current.as_of] != current:
        raise LedgerNotReady("conflicting_current_checkpoint")
    by_time[current.as_of] = current
    return tuple(sorted(by_time.values(), key=lambda sample: sample.as_of))


def evaluate_loss_gate(previous: LossGateState | None, current: EquityCheckpoint,
                       history: GateHistory, *, data_valid: bool = True,
                       allow_inception_segment: bool = False) -> GateEvaluation:
    """Return explicit readiness and latched state; callers block new risk on false."""
    aware(current.as_of, "checkpoint")
    day = current.as_of.astimezone(SHANGHAI).date()
    state = previous or LossGateState(current.segment_id, day)

    def invalid(reason):
        return GateEvaluation(state, False, False, (reason,), None, False)

    if state.segment_id != current.segment_id:
        return invalid("gate_segment_mismatch")
    if data_valid is not True:
        return invalid("ledger_not_ready")
    try:
        if state.day_origin_nav is not None:
            decimal_value(state.day_origin_nav, "daily_origin", positive=True)
        if (not isinstance(state.day, date) or isinstance(state.day, datetime)
                or not isinstance(state.daily_latched, bool) or not isinstance(state.rolling_latched, bool)
                or type(state.revision) is not int or state.revision < 0):
            raise LedgerNotReady("invalid_gate_state")
        if state.as_of is not None:
            aware(state.as_of, "gate_state")
    except (LedgerNotReady, TypeError, AttributeError):
        return invalid("invalid_persisted_gate_state")
    if state.as_of is not None and current.as_of < state.as_of:
        return invalid("out_of_order_gate_checkpoint")
    try:
        _validate_checkpoint(current, current.segment_id)
        samples = _validated_history(history, current)
    except (LedgerNotReady, TypeError, AttributeError, ValueError):
        return invalid("invalid_nav_history")
    origin_at = datetime.combine(day, time.min, SHANGHAI)
    by_time = {sample.as_of: sample for sample in samples}
    origin = by_time.get(origin_at)
    origin_nav = origin.nav if origin else None
    if state.day == day and state.day_origin_nav is not None:
        if origin_nav is not None and state.day_origin_nav != origin_nav:
            return invalid("conflicting_daily_origin")
        origin_nav = state.day_origin_nav
    coverage_valid = (history.complete is True and history.complete_through == current.as_of)
    daily_coverage = history.complete if history.daily_complete is None else history.daily_complete
    daily_valid = (origin_nav is not None and daily_coverage is True and history.complete_through == current.as_of
                   and history.complete_from <= origin_at)
    reasons = []
    if not daily_valid:
        reasons.append("daily_origin_missing" if origin_nav is None else "daily_history_incomplete")
    daily_latched = state.daily_latched
    if state.day != day and daily_valid:
        daily_latched = False
    daily_return = current.nav / origin_nav - Decimal("1") if daily_valid else None
    if daily_return is not None and daily_return <= -DAILY_LIMIT:
        daily_latched = True
    window_start = current.as_of - timedelta(days=30)
    qualified = (coverage_valid and history.complete_from <= window_start and window_start in by_time)
    label = None
    window_samples = ()
    if qualified:
        label = "rolling_30_day"
        window_samples = tuple(sample for sample in samples if sample.as_of >= window_start)
    elif allow_inception_segment is True and coverage_valid and samples and samples[0].as_of == history.complete_from:
        label = "inception_segment"
        window_samples = samples
    else:
        reasons.append("rolling_30_day_history_incomplete")
    drawdown = None
    rolling_latched = state.rolling_latched
    latch_scope = state.rolling_latch_scope
    if window_samples:
        peak = max(sample.nav for sample in window_samples)
        drawdown = Decimal("1") - current.nav / peak
        if drawdown >= ROLLING_LIMIT:
            rolling_latched = True
            latch_scope = latch_scope or label
    if daily_latched:
        reasons.append("daily_loss_latched")
    if rolling_latched:
        reasons.append(f"{latch_scope or 'rolling_30_day'}_drawdown_latched")
    ready = daily_valid and label is not None
    # A missing new-day origin must leave the previous day and its latch intact.
    updated = replace(state, day=day if daily_valid else state.day,
                      day_origin_nav=origin_nav if daily_valid else state.day_origin_nav,
                      daily_latched=daily_latched, rolling_latched=rolling_latched,
                      rolling_latch_scope=latch_scope, revision=state.revision + 1,
                      as_of=current.as_of)
    return GateEvaluation(updated, ready and not daily_latched and not rolling_latched,
                          ready, tuple(reasons), label, qualified, daily_return, drawdown)


def reset_rolling_latch(state: LossGateState, *, expected_revision: int,
                        reason: str, evidence: str, at: datetime) -> LossGateState:
    """Controlled operation; persist with repository CAS and append-only audit."""
    if expected_revision != state.revision:
        raise ValueError("revision_conflict")
    if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("reset_requires_reason_and_evidence")
    aware(at, "reset")
    if state.as_of is not None and at < state.as_of:
        raise ValueError("reset_before_checkpoint")
    return replace(state, rolling_latched=False, rolling_latch_scope=None,
                   revision=state.revision + 1, reset_reason=reason.strip(),
                   reset_evidence=evidence.strip(), reset_at=at)
