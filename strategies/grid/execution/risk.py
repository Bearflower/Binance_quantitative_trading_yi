"""Pure, fail-closed equity risk check for the ETHUSDT order executor.

The caller must fetch account equity and complete transfer evidence from the
ordinary USD-M endpoints. This module never calls the shared client's
``get_income_history`` method, which silently returns [] for ordinary accounts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
DAILY_LOSS_RATIO = Decimal("0.03")
PEAK_DRAWDOWN_RATIO = Decimal("0.15")


def beijing_day(epoch_ms: int) -> str:
    if not isinstance(epoch_ms, int) or epoch_ms <= 0:
        raise ValueError("observed_at_ms must be positive UTC epoch milliseconds")
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(
        BEIJING).date().isoformat()


def beijing_day_start_ms(epoch_ms: int) -> int:
    if not isinstance(epoch_ms, int) or epoch_ms <= 0:
        raise ValueError("epoch_ms must be positive")
    local = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(BEIJING)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _positive(value) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss_usdt: Decimal
    max_drawdown_usdt: Decimal

    def __post_init__(self):
        if not _positive(self.max_daily_loss_usdt):
            raise ValueError("max_daily_loss_usdt must be positive")
        if not _positive(self.max_drawdown_usdt):
            raise ValueError("max_drawdown_usdt must be positive")


@dataclass(frozen=True)
class RiskEvidence:
    observed_at_ms: int
    baseline_day: str
    baseline_equity_usdt: Optional[Decimal]
    peak_equity_usdt: Optional[Decimal]
    equity_usdt: Optional[Decimal]
    baseline_verified: bool
    income_complete: bool
    transfer_count: int
    income_window_start_ms: Optional[int]
    income_window_end_ms: Optional[int]


class RiskStatus(str, Enum):
    ALLOW = "ALLOW"
    EXIT_LOCKED = "EXIT_LOCKED"
    UNKNOWN_LOCKED = "UNKNOWN_LOCKED"


@dataclass(frozen=True)
class RiskAssessment:
    status: RiskStatus
    reasons: Tuple[str, ...]
    daily_loss_usdt: Optional[Decimal] = None
    drawdown_usdt: Optional[Decimal] = None
    next_peak_equity_usdt: Optional[Decimal] = None


def assess_risk(evidence: RiskEvidence, limits: RiskLimits) -> RiskAssessment:
    """Return a latch instruction; never reset an unverified daily baseline.

    The absolute USDT caps and percentage caps are independent; whichever is
    reached first stops opening. Missing equity, an unverifiable Beijing-day
    baseline, an incomplete income window, or any transfer is unknown risk.
    """
    try:
        day = beijing_day(evidence.observed_at_ms)
    except (ValueError, OverflowError):
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("invalid_observation_time",))

    if evidence.baseline_day != day or evidence.baseline_verified is not True:
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("unverified_day_baseline",))
    if evidence.income_complete is not True:
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("income_incomplete",))
    if (not isinstance(evidence.income_window_start_ms, int)
            or not isinstance(evidence.income_window_end_ms, int)
            or evidence.income_window_start_ms > beijing_day_start_ms(evidence.observed_at_ms)
            or evidence.income_window_end_ms < evidence.observed_at_ms):
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("income_window_incomplete",))
    if (not isinstance(evidence.transfer_count, int)
            or evidence.transfer_count < 0):
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("transfer_evidence_invalid",))
    if evidence.transfer_count:
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("account_transfer",))
    if (not _positive(evidence.baseline_equity_usdt)
            or not _positive(evidence.peak_equity_usdt)
            or not _positive(evidence.equity_usdt)):
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("equity_missing_or_invalid",))

    baseline = evidence.baseline_equity_usdt
    peak = evidence.peak_equity_usdt
    equity = evidence.equity_usdt
    if peak < baseline:
        return RiskAssessment(RiskStatus.UNKNOWN_LOCKED, ("peak_below_baseline",))

    next_peak = max(peak, equity)
    daily_loss = max(Decimal(0), baseline - equity)
    drawdown = max(Decimal(0), peak - equity)
    reasons = []
    if daily_loss / baseline >= DAILY_LOSS_RATIO:
        reasons.append("daily_pct")
    if drawdown / peak >= PEAK_DRAWDOWN_RATIO:
        reasons.append("drawdown_pct")
    if daily_loss >= limits.max_daily_loss_usdt:
        reasons.append("daily_usdt")
    if drawdown >= limits.max_drawdown_usdt:
        reasons.append("drawdown_usdt")
    return RiskAssessment(
        RiskStatus.EXIT_LOCKED if reasons else RiskStatus.ALLOW,
        tuple(reasons), daily_loss, drawdown, next_peak)
