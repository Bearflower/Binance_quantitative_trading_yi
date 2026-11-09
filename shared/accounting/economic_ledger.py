"""按交易所事件 ID 核对收入与成交，保守归属策略净额。

这是已观察 USDT income 的桥接报告，不含未实现盈亏、划转或账户权益。
任何无法用 trade_id → fill → order_id 唯一证明归属的金额保留在未归属项。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable


INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")


def _decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("收入或成交金额缺失")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("收入或成交金额不是有限数")
    return result


def _id(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def build_economic_report(
    fills: Iterable[dict], incomes: Iterable[dict], trade_records: Iterable[dict],
    reviewed_overrides: Iterable[dict] = (),
    derived_attributions: Iterable[dict] = (),
) -> dict:
    """用唯一成交和订单标识归属；收入事件是金额唯一来源。"""
    fill_by_id: dict[tuple[str, str, str], dict] = {}
    for fill in fills:
        key = (str(fill["account_id"]), str(fill["symbol"]), str(fill["trade_id"]))
        if key in fill_by_id and fill_by_id[key] != fill:
            raise ValueError(f"冲突的 fill trade_id: {key}")
        fill_by_id[key] = fill

    order_owners: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for trade in trade_records:
        order_id = _id(trade.get("order_id"))
        strategy = trade.get("strategy")
        account_id = _id(trade.get("account_id"))
        if account_id and order_id and strategy:
            order_owners[(account_id, str(trade["symbol"]), order_id)].add(str(strategy))

    income_by_id: dict[tuple[str, str, str], dict] = {}
    for income in incomes:
        key = (str(income["account_id"]), str(income["income_type"]), str(income["tran_id"]))
        if key in income_by_id and income_by_id[key] != income:
            raise ValueError(f"冲突的 income tran_id: {key}")
        income_by_id[key] = income

    overrides_by_id: dict[tuple[str, str, str], dict] = {}
    for override in reviewed_overrides:
        key = (str(override["account_id"]), str(override["income_type"]), str(override["tran_id"]))
        if key in overrides_by_id and overrides_by_id[key] != override:
            raise ValueError(f"冲突的 reviewed override tran_id: {key}")
        overrides_by_id[key] = override

    derived_by_id: dict[tuple[str, str, str], dict] = {}
    for derived in derived_attributions:
        key = (str(derived["account_id"]), str(derived["income_type"]), str(derived["tran_id"]))
        if key in derived_by_id and derived_by_id[key] != derived:
            raise ValueError(f"冲突的 derived attribution tran_id: {key}")
        derived_by_id[key] = derived

    def matching(row: dict, income: dict, amount: Decimal, symbol: str, kind: str, asset: str) -> bool:
        return (
            row.get("symbol") == symbol and row.get("income_type") == kind
            and row.get("asset") == asset
            and row.get("event_time_utc") == income.get("event_time_utc")
            and row.get("income") is not None and _decimal(row["income"]) == amount
        )

    account_usdt = Decimal("0")
    unattributed_usdt = Decimal("0")
    by_strategy: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"realized_pnl_usdt": Decimal("0"),
                 "commission_usdt": Decimal("0"), "funding_usdt": Decimal("0"),
                 "net_usdt": Decimal("0")}
    )
    other_assets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    unattributed = []
    events = []
    attribution_conflicts = []
    components = {"realized_pnl_usdt": Decimal("0"), "commission_usdt": Decimal("0"), "funding_usdt": Decimal("0"), "net_usdt": Decimal("0")}

    for income in income_by_id.values():
        amount = _decimal(income.get("income"))
        asset = str(income.get("asset") or "UNKNOWN").upper()
        kind = str(income.get("income_type") or "UNKNOWN")
        symbol = str(income.get("symbol") or "")
        trade_id = _id(income.get("trade_id"))
        reason = None
        owner = None

        if asset != "USDT":
            other_assets[asset] += amount
            reason = "non_usdt_asset"
        else:
            account_usdt += amount
            component = {"REALIZED_PNL": "realized_pnl_usdt", "COMMISSION": "commission_usdt", "FUNDING_FEE": "funding_usdt"}.get(kind)
            if component:
                components[component] += amount
            components["net_usdt"] += amount
            if kind == "FUNDING_FEE":
                reason = "funding_owner_unproven"
            elif kind not in ("REALIZED_PNL", "COMMISSION"):
                reason = "unsupported_income_type"
            elif trade_id is None:
                reason = "missing_trade_id"
            else:
                fill = fill_by_id.get((str(income["account_id"]), symbol, trade_id))
                if fill is None:
                    reason = "fill_not_found"
                else:
                    order_id = _id(fill.get("order_id"))
                    owners = order_owners.get(
                        (str(income["account_id"]), symbol, order_id), set()
                    ) if order_id else set()
                    if len(owners) > 1:
                        reason = "order_owner_ambiguous"
                    elif not owners:
                        reason = "order_owner_missing"
                    else:
                        raw_expected = (
                            fill.get("realized_pnl") if kind == "REALIZED_PNL"
                            else fill.get("commission")
                        )
                        if raw_expected is None:
                            reason = "fill_amount_missing"
                        elif fill.get("commission_asset", "USDT") != "USDT" and kind == "COMMISSION":
                            reason = "fill_commission_non_usdt"
                        elif amount != (
                            _decimal(raw_expected) if kind == "REALIZED_PNL"
                            else -_decimal(raw_expected)
                        ):
                            reason = "fill_income_amount_mismatch"
                        else:
                            owner = next(iter(owners))

            key = (str(income["account_id"]), kind, str(income["tran_id"]))
            override = overrides_by_id.get(key)
            derived = derived_by_id.get(key)
            valid_override = bool(override and matching(override, income, amount, symbol, kind, asset)
                                  and override.get("reviewed_by") and override.get("evidence")
                                  and override.get("strategy"))
            valid_derived = bool(derived and matching(derived, income, amount, symbol, kind, asset)
                                 and derived.get("method") and derived.get("evidence")
                                 and derived.get("algorithm_version") and derived.get("strategy"))
            if valid_override and valid_derived and override["strategy"] != derived["strategy"]:
                attribution_conflicts.append({
                    "account_id": key[0], "income_type": kind, "tran_id": key[2],
                    "manual_strategy": str(override["strategy"]),
                    "derived_strategy": str(derived["strategy"]),
                })
            if owner is None and reason in {
                "funding_owner_unproven", "missing_trade_id", "fill_not_found",
                "order_owner_missing", "order_owner_ambiguous",
            }:
                if valid_override:
                    owner = str(override["strategy"])
                elif override is not None:
                    reason = "override_identity_mismatch"
                elif valid_derived:
                    owner = str(derived["strategy"])
                elif derived is not None:
                    reason = "derived_identity_mismatch"

        events.append({
            "account_id": str(income["account_id"]), "tran_id": str(income["tran_id"]),
            "income_type": kind, "symbol": symbol, "asset": asset, "amount": str(amount),
            "strategy": owner, "reason": None if owner else reason,
            "event_time_utc": income.get("event_time_utc").isoformat() if income.get("event_time_utc") else None,
        })
        if owner is not None:
            bucket = by_strategy[owner]
            field = {
                "REALIZED_PNL": "realized_pnl_usdt",
                "COMMISSION": "commission_usdt",
                "FUNDING_FEE": "funding_usdt",
            }[kind]
            bucket[field] += amount
            bucket["net_usdt"] += amount
        else:
            if asset == "USDT":
                unattributed_usdt += amount
            unattributed.append({
                "account_id": str(income["account_id"]), "tran_id": str(income["tran_id"]),
                "symbol": symbol, "income_type": kind, "asset": asset,
                "amount": str(amount), "reason": reason,
            })

    allocated_total = sum((b["net_usdt"] for b in by_strategy.values()), Decimal("0"))
    if allocated_total + unattributed_usdt != account_usdt:
        raise AssertionError("USDT 收入守恒失败")
    return {
        "account_usdt_total": str(account_usdt),
        "account_components": {key: str(value) for key, value in components.items()},
        "events": sorted(events, key=lambda row: (row["account_id"], row["income_type"], row["tran_id"])),
        "attributed_usdt_total": str(allocated_total),
        "unattributed_usdt_total": str(unattributed_usdt),
        "unattributed_count": len(unattributed),
        "attribution_conflicts": attribution_conflicts,
        "unattributed_usdt_absolute": str(sum((abs(Decimal(row["amount"])) for row in unattributed if row["asset"] == "USDT"), Decimal("0"))),
        "by_strategy": {
            strategy: {key: str(value) for key, value in values.items()}
            for strategy, values in sorted(by_strategy.items())
        },
        "other_asset_totals": {asset: str(value) for asset, value in sorted(other_assets.items())},
        "unattributed": sorted(unattributed, key=lambda row: (row["account_id"], row["income_type"], row["tran_id"])),
        "income_count": len(income_by_id),
    }


def coverage_gaps(
    cursors: Iterable[dict], account_id: str, start_ms: int, end_ms: int,
    symbols: set[str],
) -> list[str]:
    """证明所需收入类型及相关币种成交窗口被完整游标连续覆盖。"""
    if start_ms >= end_ms:
        raise ValueError("时间窗口无效")
    rows = list(cursors)
    required = [
        ("um/income", kind, None, f"income:{kind}") for kind in INCOME_TYPES
    ] + [("userTrades", None, sym, f"fills:{sym}") for sym in sorted(symbols)]
    gaps = []
    for source, kind, symbol, label in required:
        intervals = sorted(
            (int(row["window_start_ms"]), int(row["covered_until_ms"]))
            for row in rows
            if row.get("account_id") == account_id
            and row.get("source") == source
            and row.get("income_type") == kind
            and row.get("symbol") == symbol
            and row.get("page_complete") is True
            and row.get("request_status") == "ok"
            and row.get("covered_until_ms") is not None
        )
        covered = start_ms
        for left, right in intervals:
            if left <= covered:
                covered = max(covered, right)
            if covered >= end_ms:
                break
        if covered < end_ms:
            gaps.append(label)
    return gaps
