"""Performance of verified USDT income over complete Beijing calendar days.

This is a realized-income series. It excludes unrealized PNL and external cash
flows; capital snapshots supply denominators, never replace exchange income.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from shared.performance_metrics import annualized_sharpe, max_drawdown_ratio


BEIJING = timezone(timedelta(hours=8))
CALIBER = "exchange_income_realized_daily_v1"


def _empty(reason, dates=()):
    return {"sharpe": None, "max_drawdown": None, "sample_count": len(dates),
            "window_start": dates[0].isoformat() if dates else None,
            "window_end": dates[-1].isoformat() if dates else None,
            "reason": reason, "caliber": CALIBER}


def _metric(dates, daily_amounts, opening_capital):
    if not dates:
        return _empty("no_completed_days")
    returns = []
    for day in dates:
        capital = opening_capital.get(day)
        if capital is None:
            return _empty("opening_capital_missing", dates)
        capital = Decimal(str(capital))
        if not capital.is_finite() or capital <= 0:
            return _empty("opening_capital_invalid", dates)
        returns.append(float(daily_amounts.get(day, Decimal(0)) / capital))
    cum_return = 0.0
    net_values = [1.0]
    for value in returns:
        cum_return += value
        net_values.append(max(0.0, 1.0 + cum_return))
    sharpe = annualized_sharpe(returns)
    reason = ("sample_below_two_days" if len(returns) < 2 else
              "zero_return_variance" if sharpe is None else None)
    return {"sharpe": sharpe, "max_drawdown": max_drawdown_ratio(net_values),
            "sample_count": len(dates), "window_start": dates[0].isoformat(),
            "window_end": dates[-1].isoformat(), "reason": reason, "caliber": CALIBER}


def unavailable_realized_metrics(reason, report, strategy_ids):
    start = datetime.fromtimestamp(report["start_ms"] / 1000, BEIJING).date()
    end = datetime.fromtimestamp(report["end_ms"] / 1000, BEIJING).date()
    dates = []
    while start < end:
        dates.append(start)
        start += timedelta(days=1)
    return {"account": _empty(reason, dates),
            "strategies": {sid: _empty(reason, dates) for sid in strategy_ids}}


def realized_income_metrics(report, account_capital, strategy_capital, strategy_ids):
    """Compute daily realized-income return, Sharpe and drawdown for a report.

    The reported window is calendar aligned. Current partial days are excluded;
    a zero-income day is included only when the source report covers that day.
    """
    start = datetime.fromtimestamp(report["start_ms"] / 1000, BEIJING)
    end = datetime.fromtimestamp(report["end_ms"] / 1000, BEIJING)
    dates = []
    day = start.date()
    while day < end.date():
        dates.append(day)
        day += timedelta(days=1)
    if report.get("publication_status") != "published":
        reason = "ledger_not_published"
    elif not report.get("account_coverage_complete"):
        reason = "income_coverage_incomplete"
    else:
        reason = None
    if reason:
        return unavailable_realized_metrics(reason, report, strategy_ids)

    account_amounts = defaultdict(Decimal)
    strategy_amounts = defaultdict(lambda: defaultdict(Decimal))
    for event in report.get("events", []):
        if event.get("asset") != "USDT" or not event.get("event_time_utc"):
            continue
        event_day = datetime.fromisoformat(event["event_time_utc"]).astimezone(BEIJING).date()
        if event_day not in dates:
            continue
        amount = Decimal(str(event["amount"]))
        account_amounts[event_day] += amount
        if event.get("strategy"):
            strategy_amounts[event["strategy"]][event_day] += amount
    account = _metric(dates, account_amounts, account_capital)
    if report.get("strategy_attribution_complete"):
        strategies = {sid: _metric(dates, strategy_amounts[sid], strategy_capital.get(sid, {}))
                      for sid in strategy_ids}
    else:
        strategies = {sid: _empty("strategy_attribution_incomplete", dates)
                      for sid in strategy_ids}
    return {"account": account, "strategies": strategies}


def _empty_lifecycle(reason):
    return {"order_count": None, "fill_count": None,
            "closed_count": None, "win_count": None, "loss_count": None,
            "win_rate": None, "sample_start": None, "sample_partial": True,
            "reason": reason, "caliber": "exchange_verified_position_lifecycle_v1"}


def unavailable_lifecycle_stats(reason, strategy_ids):
    return {"account": _empty_lifecycle(reason),
            "strategies": {sid: _empty_lifecycle(reason) for sid in strategy_ids}}


def verified_lifecycle_stats(report, baselines, fills, owners, incomes,
                             covered_symbols, strategy_ids):
    """Count completed net positions from immutable flat anchors and matched fills.

    An unverified symbol contributes no trades. The result is always marked a
    sample because flat anchors can be later than the report's period start.
    """
    if report.get("publication_status") != "published":
        reason = "ledger_not_published"
    elif not report.get("account_coverage_complete"):
        reason = "income_coverage_incomplete"
    elif not report.get("strategy_attribution_complete"):
        reason = "strategy_attribution_incomplete"
    else:
        reason = None
    if reason:
        return unavailable_lifecycle_stats(reason, strategy_ids)

    start = datetime.fromtimestamp(report["start_ms"] / 1000, timezone.utc)
    end = datetime.fromtimestamp(report["end_ms"] / 1000, timezone.utc)
    anchors = {}
    for baseline in baselines:
        stamp = baseline["flat_at"]
        symbol = baseline["symbol"]
        if (start <= stamp < end and Decimal(str(baseline["exchange_qty"])) == 0
                and (symbol not in anchors or stamp < anchors[symbol]["flat_at"])):
            anchors[symbol] = baseline
    if not anchors:
        result = _empty_lifecycle("verified_flat_baseline_missing")
        return {"account": result,
                "strategies": {sid: _empty_lifecycle(result["reason"]) for sid in strategy_ids}}

    owner_map = defaultdict(set)
    for row in owners:
        owner_map[(row["account_id"], row["symbol"], str(row["order_id"]))].add(row["strategy"])
    published = {(str(row.get("income_type")), str(row.get("tran_id"))): row
                 for row in report.get("events", []) if row.get("tran_id") is not None}
    income_map = {}
    for row in incomes:
        key = (row["symbol"], str(row["trade_id"]), row["income_type"])
        amount = Decimal(str(row["income"]))
        event = published.get((row["income_type"], str(row.get("tran_id"))))
        identity = (amount, event.get("strategy") if event else None)
        if (not event or event.get("asset") != "USDT" or event.get("symbol") != row["symbol"]
                or Decimal(str(event.get("amount"))) != amount):
            identity = None
        if key in income_map and income_map[key] != identity:
            income_map[key] = None
        elif key not in income_map:
            income_map[key] = identity

    by_symbol = defaultdict(list)
    for fill in fills:
        if fill["symbol"] in anchors and anchors[fill["symbol"]]["flat_at"] < fill["trade_time_utc"] < end:
            by_symbol[fill["symbol"]].append(fill)

    closures = []
    any_uncovered = False
    for symbol, anchor in anchors.items():
        if symbol not in covered_symbols:
            any_uncovered = True
            continue
        qty = Decimal(0)
        position = None
        symbol_closures = []
        valid = True
        seen = set()
        for fill in sorted(by_symbol[symbol], key=lambda row: (row["trade_time_utc"], int(row["trade_id"]))):
            trade_id = str(fill["trade_id"])
            if trade_id in seen:
                valid = False
                break
            seen.add(trade_id)
            side = str(fill.get("side") or "").upper()
            amount = Decimal(str(fill.get("qty")))
            gross = Decimal(str(fill.get("realized_pnl")))
            fee = Decimal(str(fill.get("commission")))
            if (side not in ("BUY", "SELL") or amount <= 0 or not amount.is_finite()
                    or not gross.is_finite() or not fee.is_finite() or fee < 0
                    or fill.get("commission_asset") != "USDT"):
                valid = False
                break
            candidates = owner_map.get((fill["account_id"], symbol, str(fill["order_id"])), set())
            if len(candidates) != 1:
                valid = False
                break
            owner = next(iter(candidates))
            expected = (("COMMISSION", -fee), ("REALIZED_PNL", gross))
            if any(value != 0 and income_map.get((symbol, trade_id, kind)) != (value, owner)
                   for kind, value in expected):
                valid = False
                break
            delta = amount if side == "BUY" else -amount
            if qty == 0:
                if gross != 0:
                    valid = False
                    break
                position = {"owner": owner, "start": fill["trade_time_utc"],
                            "net": -fee, "symbol": symbol,
                            "order_ids": {str(fill["order_id"])}, "fill_count": 1}
                qty = delta
            elif owner != position["owner"] or (qty > 0) == (delta > 0):
                if owner != position["owner"] or gross != 0:
                    valid = False
                    break
                position["net"] -= fee
                position["order_ids"].add(str(fill["order_id"]))
                position["fill_count"] += 1
                qty += delta
            elif abs(delta) > abs(qty):
                # Reversal commission must be split between two positions.
                valid = False
                break
            else:
                position["net"] += gross - fee
                position["order_ids"].add(str(fill["order_id"]))
                position["fill_count"] += 1
                qty += delta
                if qty == 0:
                    position["end"] = fill["trade_time_utc"]
                    symbol_closures.append(position)
                    position = None
        if valid:
            closures.extend(symbol_closures)

    for closure in closures:
        for event in report.get("events", []):
            if (event.get("asset") == "USDT" and event.get("income_type") == "FUNDING_FEE"
                    and event.get("symbol") == closure["symbol"]
                    and event.get("strategy") == closure["owner"]
                    and event.get("event_time_utc")
                    and closure["start"] < datetime.fromisoformat(event["event_time_utc"]) < closure["end"]):
                closure["net"] += Decimal(str(event["amount"]))

    def summary(rows):
        if not rows:
            return _empty_lifecycle("fill_coverage_incomplete" if any_uncovered else "no_verified_closures")
        wins = sum(row["net"] > 0 for row in rows)
        losses = sum(row["net"] < 0 for row in rows)
        return {"order_count": len(set().union(*(row["order_ids"] for row in rows))),
                "fill_count": sum(row["fill_count"] for row in rows),
                "closed_count": len(rows), "win_count": wins, "loss_count": losses,
                "win_rate": round(wins / len(rows) * 100, 2),
                "sample_start": min(row["start"] for row in rows).isoformat(),
                "sample_partial": True, "reason": "verified_sample_only",
                "caliber": "exchange_verified_position_lifecycle_v1"}

    return {"account": summary(closures),
            "strategies": {sid: summary([row for row in closures if row["owner"] == sid])
                           for sid in strategy_ids}}
