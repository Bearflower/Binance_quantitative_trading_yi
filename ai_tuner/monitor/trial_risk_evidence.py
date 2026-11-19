"""Evidence-only risk calculations and prospective acceptance criteria."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping

RULE_VERSION = "reduced-trial-review-v1"
# The first full month can only start after this version has been recorded.
MINIMUM_START = "2026-12-01"
MIN_CLOSED_LOTS = 20
MAX_DRAWDOWN = Decimal("0.05")
EXTRA_FEE_RATIO = Decimal("0.50")
EXTRA_SLIPPAGE_RATIO = Decimal("0.002")
MARK_SHOCK_RATIO = Decimal("0.05")
BEIJING = timezone(timedelta(hours=8))


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    return number if number.is_finite() else None


def mark_shock(positions, owned_symbols):
    """Current USDT-M mark shock, no offset, before costs or stop execution."""
    notional = Decimal(0)
    seen = set()
    for row in positions:
        symbol = row["symbol"]
        amount = _decimal(row.get("positionAmt"))
        if amount is None:
            return None
        if symbol not in owned_symbols or amount == 0:
            continue
        mark = _decimal(row.get("markPrice"))
        exposure = _decimal(row.get("notional"))
        if (symbol in seen or not symbol.endswith("USDT") or row.get("positionSide") != "BOTH"
                or mark is None or mark <= 0 or exposure is None or exposure == 0
                or abs(abs(exposure) - abs(amount) * mark) > Decimal("0.01")):
            return None
        seen.add(symbol)
        notional += abs(exposure)
    return notional * MARK_SHOCK_RATIO


def stressed_net(net_usdt, commission_usdt, turnover_usdt):
    """Reprice attributed realized net income using all entry and exit fills."""
    net, fees, turnover = map(_decimal, (net_usdt, commission_usdt, turnover_usdt))
    if net is None or fees is None or turnover is None or fees < 0 or turnover <= 0:
        return None
    return net - fees * EXTRA_FEE_RATIO - turnover * EXTRA_SLIPPAGE_RATIO


def account_initial_margin(evidence, as_of_ms):
    """Read the same signed account payload used by the equity sampler."""
    if not isinstance(evidence, Mapping) or type(as_of_ms) is not int:
        return None
    for point in reversed(evidence.get("equity_points", ())):
        if (point.get("exchange_time_ms") != as_of_ms
                or point.get("source") != "signed:GET /papi/v1/account"):
            continue
        raw = point.get("raw")
        margin = _decimal(raw.get("accountInitialMargin")) if isinstance(raw, Mapping) else None
        return str(margin) if margin is not None and margin >= 0 else None
    return None


def covered_reservation_budget(positions, owned_symbols, intents):
    """Identify the part of the policy reservation already covering intact lots."""
    live = {}
    for pos in positions:
        symbol = pos["symbol"]
        amount = _decimal(pos.get("positionAmt"))
        if amount is None:
            return None
        if symbol not in owned_symbols or not amount:
            continue
        if symbol in live or pos.get("positionSide") != "BOTH":
            return None
        live[symbol] = amount
    rows = {}
    for intent in intents:
        symbol = intent["symbol"]
        if symbol in rows or symbol not in live:
            return None
        rows[symbol] = intent
    if rows.keys() != live.keys():
        return None
    total = Decimal(0)
    for symbol, amount in live.items():
        row = rows[symbol]
        margin = _decimal(row.get("margin"))
        quantities = [_decimal(row.get(key)) for key in ("quantity", "orig_qty", "executed_qty", "fill_qty")]
        if (margin is None or margin <= 0 or row.get("cursor_complete") is not True
                or row.get("side") != ("BUY" if amount > 0 else "SELL")
                or any(value != abs(amount) for value in quantities)):
            return None
        total += margin
    return total


def review_criteria():
    return {
        "version": RULE_VERSION,
        "first_eligible_month_start": MINIMUM_START,
        "required_full_months": 2,
        "min_independent_closed_lots_per_month": MIN_CLOSED_LOTS,
        "minimum_coverage_ratio": "1",
        "net_usdt_strictly_above": "0",
        "stressed_net_usdt_strictly_above": "0",
        "maximum_strategy_drawdown_ratio_exclusive": str(MAX_DRAWDOWN),
        "maximum_account_drawdown_ratio_exclusive": str(MAX_DRAWDOWN),
        "extra_fee_ratio": str(EXTRA_FEE_RATIO),
        "extra_slippage_ratio_per_fill_side": str(EXTRA_SLIPPAGE_RATIO),
        "allowed_policy_breaches": 0,
        "decision_scope": "manual_review_only",
    }


def review_month(evidence):
    """Assess one full local month of identity-bound, externally audited facts."""
    start, end = (evidence.get(key) for key in ("start", "end"))
    if not all(isinstance(stamp, datetime) and stamp.utcoffset() is not None for stamp in (start, end)):
        return {"status": "BLOCKED_DATA", "reasons": ["month_boundary_unverified"]}
    start, end = start.astimezone(BEIJING), end.astimezone(BEIJING)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    if (start != start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            or end != next_month or start.date().isoformat() < MINIMUM_START):
        return {"status": "BLOCKED_DATA", "reasons": ["month_boundary_unverified"]}
    missing = [key for key in ("income_complete", "capital_complete", "funnel_complete",
                                "equity_complete", "pressure_complete", "policy_continuous")
               if evidence.get(key) is not True]
    lots, breaches = (evidence.get(key) for key in ("closed_lots", "policy_breaches"))
    strategy_dd, account_dd = (_decimal(evidence.get(key)) for key in ("strategy_drawdown", "account_drawdown"))
    stressed = stressed_net(evidence.get("net_usdt"), evidence.get("commission_usdt"),
                           evidence.get("turnover_usdt"))
    if (missing or stressed is None or type(lots) is not int or lots < 0
            or type(breaches) is not int or breaches < 0 or any(
                value is None or not 0 <= value <= 1 for value in (strategy_dd, account_dd))):
        return {"status": "BLOCKED_DATA", "reasons": missing or ["metric_evidence_unverified"]}
    reasons = []
    if lots < MIN_CLOSED_LOTS:
        reasons.append("insufficient_closed_lots")
    if _decimal(evidence["net_usdt"]) <= 0 or stressed <= 0:
        reasons.append("profit_gate_failed")
    if max(strategy_dd, account_dd) >= MAX_DRAWDOWN:
        reasons.append("drawdown_gate_failed")
    if breaches:
        reasons.append("policy_breach")
    return {
        "status": "KEEP_REDUCED" if reasons else "MONTH_PASSED",
        "reasons": reasons,
        "stressed_net_usdt": str(stressed),
    }
