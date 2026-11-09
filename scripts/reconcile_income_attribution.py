"""Shadow-first, exact-evidence reconciliation of exchange income owners.

Default CLI mode only reads and prints a JSON manifest. ``--apply`` is gated by
complete fact cursors and immutable-row checks inside one database transaction.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
import json
import hashlib
import os
import sys
import time

from shared.accounting.derived_attribution import derive_income
from shared.accounting.economic_ledger import INCOME_TYPES, coverage_gaps
from shared.accounting.order_evidence import resolve_order_owner
from shared.accounting.position_replay import replay_position
from scripts.audit_algo_attribution import _fetch_links
from scripts.export_execution_facts import _build_client_from_env
from scripts.report_economic_ledger import month_window_ms


_INCOME_SQL = """SELECT account_id, symbol, tran_id, income_type, income, asset,
trade_id, event_time_utc FROM trading.execution_fact_income WHERE account_id=$1
AND event_time_utc >= $2 AND event_time_utc < $3
AND income_type IN ('REALIZED_PNL','COMMISSION','FUNDING_FEE')"""
_FILL_SQL = """SELECT account_id, symbol, trade_id, order_id, side, qty,
commission, commission_asset, realized_pnl, trade_time_utc
FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=ANY($2::text[])
AND ((trade_time_utc >= $3 AND trade_time_utc < $4)
OR trade_id = ANY($5::bigint[]))"""
_TRADE_SQL = """SELECT id, account_id, symbol, order_id, strategy
FROM trading.trade_records WHERE account_id=$1 AND symbol=ANY($2::text[])
AND order_id IS NOT NULL"""
_CONDITION_SQL = """SELECT account_id, symbol, algo_id, strategy_name
FROM btc_eth.condition_orders WHERE account_id=$1 AND symbol=ANY($2::text[])
AND algo_id IS NOT NULL"""
_MANUAL_SQL = """SELECT account_id, income_type, tran_id, strategy, symbol, income,
asset, event_time_utc FROM trading.reviewed_income_attribution WHERE account_id=$1
AND event_time_utc >= $2 AND event_time_utc < $3"""
_EXISTING_SQL = """SELECT account_id, income_type, tran_id, strategy, symbol, income,
asset, event_time_utc, method, evidence, algorithm_version
FROM trading.derived_income_attribution WHERE account_id=$1
AND event_time_utc >= $2 AND event_time_utc < $3"""
_BASELINE_SQL = """SELECT account_id, symbol, flat_at, exchange_qty, evidence
FROM trading.position_flat_baseline WHERE account_id=$1
AND symbol=ANY($2::text[]) AND flat_at < $3
ORDER BY symbol, flat_at DESC"""
_CURSOR_SQL = """SELECT source, account_id, income_type, symbol, window_start_ms,
covered_until_ms, page_complete, request_status
FROM trading.execution_fact_cursor WHERE account_id=$1
AND window_start_ms < $3 AND covered_until_ms >= $2"""


def _key(row):
    return (str(row["account_id"]), str(row["income_type"]), str(row["tran_id"]))


def _identity_matches(left, right):
    return (_key(left) == _key(right)
            and str(left["symbol"]) == str(right["symbol"])
            and str(left["asset"]) == str(right["asset"])
            and left["event_time_utc"] == right["event_time_utc"]
            and Decimal(str(left["income"])) == Decimal(str(right["income"])))


def _json_equal(left, right):
    if isinstance(left, str):
        left = json.loads(left)
    if isinstance(right, str):
        right = json.loads(right)
    return left == right


def _same_derivation(old, new):
    if old.get("method") != new["method"] or old.get("algorithm_version") != new["algorithm_version"]:
        return False
    left, right = old.get("evidence"), new["evidence"]
    if isinstance(left, str):
        left = json.loads(left)
    if new["method"] == "verified_position_interval" and isinstance(left, dict):
        left = {key: value for key, value in left.items()
                if key not in {"verification_cutoff", "verification_qty"}}
        right = {key: value for key, value in right.items()
                 if key not in {"verification_cutoff", "verification_qty"}}
    return _json_equal(left, right)


def verified_fact_cutoff(cursors, account_id, start_ms, target_ms, symbols):
    """Return the common contiguous exchange-fact watermark."""
    required = [("um/income", kind, None) for kind in INCOME_TYPES]
    required += [("userTrades", None, symbol) for symbol in sorted(symbols)]
    cutoffs = []
    for source, kind, symbol in required:
        covered = start_ms
        intervals = sorted(
            (int(row["window_start_ms"]), int(row["covered_until_ms"]))
            for row in cursors
            if row.get("account_id") == account_id
            and row.get("source") == source
            and row.get("income_type") == kind
            and row.get("symbol") == symbol
            and row.get("page_complete") is True
            and row.get("request_status") == "ok"
            and row.get("covered_until_ms") is not None)
        for left, right in intervals:
            if left <= covered:
                covered = max(covered, right)
        cutoffs.append(min(covered, target_ms))
    return min(cutoffs, default=start_ms)


async def verified_position_at_cutoff(client, symbol, cutoff_ms):
    """Prove a PM position at a past fact watermark by ruling out gap fills."""
    positions = await client._request("GET", "/papi/v1/um/positionRisk", {}, signed=True)
    observed_ms = int(time.time() * 1000)
    if not isinstance(positions, list) or observed_ms <= cutoff_ms:
        return None, "position_snapshot_invalid", None
    if observed_ms - cutoff_ms > 6 * 24 * 60 * 60 * 1000:
        return None, "post_cutoff_gap_too_wide", None
    matching = [row for row in positions if row.get("symbol") == symbol]
    if len(matching) > 1 or any(row.get("positionSide", "BOTH") != "BOTH" for row in matching):
        return None, "hedge_or_duplicate_position", None
    try:
        qty = Decimal(str(matching[0]["positionAmt"])) if matching else Decimal(0)
    except (KeyError, ValueError, ArithmeticError):
        return None, "position_quantity_invalid", None
    if not qty.is_finite():
        return None, "position_quantity_invalid", None
    gap = await client._request("GET", "/papi/v1/um/userTrades",
                                {"symbol": symbol, "startTime": cutoff_ms,
                                 "endTime": observed_ms, "limit": 1000}, signed=True)
    if not isinstance(gap, list) or len(gap) >= 1000:
        return None, "post_cutoff_fill_coverage_incomplete", None
    if gap:
        return None, "post_cutoff_fill", None
    proof = {"snapshot_observed_ms": observed_ms, "cutoff_ms": cutoff_ms,
             "position_side": "BOTH", "gap_trade_count": 0,
             "position_payload_sha256": hashlib.sha256(json.dumps(
                 matching, sort_keys=True, default=str).encode()).hexdigest()}
    return qty, None, proof


def _order_map(account_id, symbol, fills, trade_records, algo_links, condition_orders):
    rows, by_id = {}, {}
    for fill in fills:
        order_id = fill.get("order_id")
        if order_id is None:
            continue
        strategy, proof = resolve_order_owner(account_id, symbol, order_id,
                                               trade_records, algo_links, condition_orders)
        if strategy is None:
            continue
        row = {"account_id": account_id, "symbol": symbol,
               "order_id": int(order_id), "strategy": strategy,
               "source_kind": proof["source_kind"],
               "source_id": str(proof.get("algo_id") or proof.get("source_ids", [""])[0]),
               "evidence": proof}
        rows[(account_id, symbol, str(order_id))] = row
        by_id[str(order_id)] = row
    return rows, by_id


def build_replay_by_symbol(account_id, baselines, fills, trade_records,
                           algo_links, condition_orders, exchange_qty,
                           coverage_end, fill_coverage):
    """Replay only from an existing exchange-confirmed zero baseline."""
    result = {}
    for symbol, baseline in baselines.items():
        scoped = [row for row in fills if str(row.get("account_id")) == account_id
                  and str(row.get("symbol")) == symbol
                  and baseline["flat_at"] < row.get("trade_time_utc", baseline["flat_at"]) < coverage_end]
        _, owners = _order_map(account_id, symbol, scoped, trade_records,
                                algo_links, condition_orders)
        result[(account_id, symbol)] = replay_position(
            baseline, scoped, owners, coverage_end, exchange_qty[symbol],
            coverage_complete=fill_coverage.get(symbol, False))
    return result


def plan_reconciliation(incomes, fills, trade_records, algo_links,
                        condition_orders, manual_rows, existing_rows,
                        replay_by_symbol):
    """Build candidates without database or exchange side effects."""
    manual = {_key(row): row for row in manual_rows}
    existing = {_key(row): row for row in existing_rows}
    by_symbol = defaultdict(list)
    for fill in fills:
        by_symbol[(str(fill["account_id"]), str(fill["symbol"]))].append(fill)
    order_rows, derived_rows, rejected, conflicts = {}, [], [], []
    for income in incomes:
        account, symbol = str(income["account_id"]), str(income["symbol"])
        scoped_fills = by_symbol[(account, symbol)]
        by_trade = {str(fill["trade_id"]): fill for fill in scoped_fills}
        scoped_rows, order_evidence = _order_map(
            account, symbol, scoped_fills, trade_records, algo_links, condition_orders)
        order_rows.update(scoped_rows)
        replay = replay_by_symbol.get((account, symbol))
        derived, reason = derive_income(income, by_trade, order_evidence, replay)
        if derived is None:
            rejected.append({"account_id": account, "income_type": income["income_type"],
                             "tran_id": str(income["tran_id"]), "symbol": symbol,
                             "reason": reason})
            continue
        key = _key(income)
        reviewed = manual.get(key)
        if reviewed:
            if not _identity_matches(reviewed, income) or str(reviewed["strategy"]) != derived["strategy"]:
                conflicts.append({"account_id": account, "income_type": key[1],
                                  "tran_id": key[2], "reason": "manual_owner_conflict"})
            continue
        old = existing.get(key)
        if old:
            if not _identity_matches(old, derived) or str(old["strategy"]) != derived["strategy"]:
                conflicts.append({"account_id": account, "income_type": key[1],
                                  "tran_id": key[2], "reason": "derived_owner_conflict"})
            elif not _same_derivation(old, derived):
                conflicts.append({"account_id": account, "income_type": key[1],
                                  "tran_id": key[2], "reason": "derived_evidence_conflict"})
            continue
        derived_rows.append(derived)
    return {"order_rows": list(order_rows.values()), "derived_rows": derived_rows,
            "rejected": rejected, "conflicts": conflicts,
            "examined": len(incomes)}


def apply_plan(connection, plan, *, coverage_complete):
    """Preflight synchronously, then return an atomic async writer coroutine."""
    if plan["conflicts"]:
        raise ValueError("owner conflict blocks attribution writes")
    if not coverage_complete:
        raise ValueError("fact cursor coverage incomplete")

    async def commit():
        async with connection.transaction(isolation="serializable"):
            for row in plan.get("baseline_rows", []):
                await connection.execute(
                    "INSERT INTO trading.position_flat_baseline "
                    "(account_id,symbol,flat_at,exchange_qty,evidence) "
                    "VALUES ($1,$2,$3,0,$4::jsonb) ON CONFLICT DO NOTHING",
                    row["account_id"], row["symbol"], row["flat_at"],
                    json.dumps(row["evidence"], sort_keys=True))
                saved = await connection.fetchrow(
                    "SELECT exchange_qty,evidence FROM trading.position_flat_baseline "
                    "WHERE account_id=$1 AND symbol=$2 AND flat_at=$3",
                    row["account_id"], row["symbol"], row["flat_at"])
                if not saved or Decimal(str(saved["exchange_qty"])) != 0 or not _json_equal(saved["evidence"], row["evidence"]):
                    raise ValueError("immutable flat baseline conflict")
            for row in plan["order_rows"]:
                await connection.execute(
                    "INSERT INTO trading.order_strategy_evidence "
                    "(account_id,symbol,order_id,strategy,source_kind,source_id,evidence) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING",
                    row["account_id"], row["symbol"], row["order_id"], row["strategy"],
                    row["source_kind"], row["source_id"], json.dumps(row["evidence"], sort_keys=True))
                saved = await connection.fetchrow(
                    "SELECT strategy,source_kind,source_id,evidence FROM trading.order_strategy_evidence "
                    "WHERE account_id=$1 AND symbol=$2 AND order_id=$3",
                    row["account_id"], row["symbol"], row["order_id"])
                if (not saved or saved["strategy"] != row["strategy"]
                        or saved["source_kind"] != row["source_kind"]
                        or saved["source_id"] != row["source_id"]
                        or not _json_equal(saved["evidence"], row["evidence"])):
                    raise ValueError("immutable order evidence conflict")
            for row in plan["derived_rows"]:
                fact = await connection.fetchrow(
                    "SELECT account_id,income_type,tran_id,symbol,income,asset,event_time_utc "
                    "FROM trading.execution_fact_income WHERE account_id=$1 AND income_type=$2 AND tran_id=$3",
                    row["account_id"], row["income_type"], row["tran_id"])
                if not fact or not _identity_matches(fact, row):
                    raise ValueError("exchange income identity changed")
                manual = await connection.fetchrow(
                    "SELECT account_id,income_type,tran_id,strategy,symbol,income,asset,event_time_utc "
                    "FROM trading.reviewed_income_attribution WHERE account_id=$1 AND income_type=$2 AND tran_id=$3",
                    row["account_id"], row["income_type"], row["tran_id"])
                if manual:
                    if not _identity_matches(manual, row) or manual["strategy"] != row["strategy"]:
                        raise ValueError("manual owner conflict")
                    continue
                await connection.execute(
                    "INSERT INTO trading.derived_income_attribution "
                    "(account_id,income_type,tran_id,strategy,symbol,income,asset,event_time_utc,method,evidence,algorithm_version) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11) ON CONFLICT DO NOTHING",
                    row["account_id"], row["income_type"], row["tran_id"], row["strategy"],
                    row["symbol"], row["income"], row["asset"], row["event_time_utc"],
                    row["method"], json.dumps(row["evidence"], sort_keys=True), row["algorithm_version"])
                saved = await connection.fetchrow(
                    "SELECT account_id,income_type,tran_id,strategy,symbol,income,asset,event_time_utc,method,evidence,algorithm_version "
                    "FROM trading.derived_income_attribution WHERE account_id=$1 AND income_type=$2 AND tran_id=$3",
                    row["account_id"], row["income_type"], row["tran_id"])
                if (not saved or not _identity_matches(saved, row)
                        or saved["strategy"] != row["strategy"]
                        or saved["method"] != row["method"]
                        or saved["algorithm_version"] != row["algorithm_version"]
                        or not _json_equal(saved["evidence"], row["evidence"])):
                    raise ValueError("immutable derived owner conflict")
        return {"order_rows_seen": len(plan["order_rows"]),
                "income_rows_seen": len(plan["derived_rows"])}
    return commit()


async def _run(month, account_id, apply, requested_end_ms=None):
    from shared.database import DatabaseManager

    password = os.environ.get("DATABASE_PASSWORD")
    if not password:
        raise RuntimeError("DATABASE_PASSWORD is required")
    start_ms, calendar_end = month_window_ms(month)
    latest_safe_ms = int(time.time() * 1000) - 1000
    if requested_end_ms is not None and requested_end_ms > latest_safe_ms:
        raise ValueError("end-ms is later than the safe observation time")
    target_ms = min(calendar_end, requested_end_ms or latest_safe_ms)
    if start_ms >= target_ms:
        raise ValueError("month has not started")
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    db = DatabaseManager(host=os.environ.get("DATABASE_HOST", "localhost"),
                         port=int(os.environ.get("DATABASE_PORT", "5432")),
                         database=os.environ.get("DATABASE_NAME", "trading_platform"),
                         user=os.environ.get("DATABASE_USER", "trading_user"),
                         password=password, min_pool_size=1, max_pool_size=2)
    await db.connect()
    client = None
    try:
        if requested_end_ms is None:
            target = datetime.fromtimestamp(target_ms / 1000, timezone.utc)
            async with db.pool.acquire() as connection:
                async with connection.transaction(isolation="repeatable_read", readonly=True):
                    preliminary = [dict(row) for row in await connection.fetch(
                        _INCOME_SQL, account_id, start, target)]
                    preliminary_symbols = {str(row["symbol"]) for row in preliminary if row.get("symbol")}
                    preliminary_cursors = [dict(row) for row in await connection.fetch(
                        _CURSOR_SQL, account_id, start_ms, target_ms)]
                    preliminary_symbols.update(str(row["symbol"]) for row in preliminary_cursors
                                               if row.get("source") == "userTrades"
                                               and row.get("symbol")
                                               and row.get("window_start_ms", 0) >= start_ms)
            end_ms = verified_fact_cutoff(preliminary_cursors, account_id,
                                          start_ms, target_ms, preliminary_symbols)
        else:
            end_ms = target_ms
        if end_ms <= start_ms:
            if apply:
                raise ValueError("fact cursor coverage incomplete")
            return {"account_id": account_id, "month": month, "mode": "shadow",
                    "status": "waiting_for_fact_coverage", "examined": 0,
                    "income_candidates": 0, "rejected": [], "conflicts": []}
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
        async with db.pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                incomes = [dict(row) for row in await connection.fetch(_INCOME_SQL, account_id, start, end)]
                symbols = sorted({str(row["symbol"]) for row in incomes if row.get("symbol")}
                                 | (preliminary_symbols if requested_end_ms is None else set()))
                baseline_rows = [dict(row) for row in await connection.fetch(
                    _BASELINE_SQL, account_id, symbols, end)]
                baselines = {}
                for row in baseline_rows:
                    baselines.setdefault(str(row["symbol"]), row)
                earliest = min([start] + [row["flat_at"] for row in baselines.values()])
                trade_ids = sorted({int(row["trade_id"]) for row in incomes if row.get("trade_id") is not None})
                fills = [dict(row) for row in await connection.fetch(
                    _FILL_SQL, account_id, symbols, earliest, end, trade_ids)]
                trades = [dict(row) for row in await connection.fetch(_TRADE_SQL, account_id, symbols)]
                conditions = [dict(row) for row in await connection.fetch(_CONDITION_SQL, account_id, symbols)]
                manual = [dict(row) for row in await connection.fetch(_MANUAL_SQL, account_id, start, end)]
                existing = [dict(row) for row in await connection.fetch(_EXISTING_SQL, account_id, start, end)]
                earliest_ms = int(earliest.timestamp() * 1000)
                cursors = [dict(row) for row in await connection.fetch(
                    _CURSOR_SQL, account_id, earliest_ms, end_ms)]
        links = []
        if symbols:
            client, code = _build_client_from_env()
            if client is None:
                raise RuntimeError(f"PM client unavailable: {code}")
            if not client.use_unified_account:
                raise RuntimeError("PM account required")
            links = await _fetch_links(client, symbols, earliest_ms, end_ms)
            links = [{**row, "account_id": account_id} for row in links]
        gaps = coverage_gaps(cursors, account_id, start_ms, end_ms, set(symbols))
        position_qty, position_rejections, new_baselines = {}, {}, []
        if client is not None:
            for symbol in symbols:
                qty, reason, proof = await verified_position_at_cutoff(client, symbol, end_ms)
                if reason:
                    position_rejections[symbol] = reason
                    continue
                position_qty[symbol] = qty
                if qty == 0 and symbol not in baselines and not gaps:
                    new_baselines.append({"account_id": account_id, "symbol": symbol,
                                          "flat_at": end, "exchange_qty": Decimal(0),
                                          "evidence": proof})
        replay_baselines = {symbol: row for symbol, row in baselines.items()
                            if symbol in position_qty}
        fill_coverage = {
            symbol: "fills:" + symbol not in coverage_gaps(
                cursors, account_id,
                int(baseline["flat_at"].timestamp() * 1000), end_ms, {symbol})
            for symbol, baseline in replay_baselines.items()
        }
        replay = build_replay_by_symbol(account_id, replay_baselines, fills,
                                        trades, links, conditions, position_qty,
                                        end, fill_coverage)
        plan = plan_reconciliation(incomes, fills, trades, links, conditions,
                                   manual, existing, replay)
        plan["baseline_rows"] = new_baselines
        result = {"account_id": account_id, "month": month,
                  "start_ms": start_ms, "end_ms": end_ms,
                  "coverage_gaps": gaps, "algo_links_observed": len(links),
                  "orders_proven": len(plan["order_rows"]),
                  "candidates": [{"account_id": row["account_id"],
                                   "income_type": row["income_type"],
                                   "tran_id": str(row["tran_id"]),
                                   "symbol": row["symbol"],
                                   "strategy": row["strategy"],
                                   "method": row["method"],
                                   "income": str(row["income"])}
                                  for row in plan["derived_rows"]],
                  "position_rejections": position_rejections,
                  "new_flat_baselines": len(new_baselines),
                  "income_candidates": len(plan["derived_rows"]),
                  "rejected": plan["rejected"], "conflicts": plan["conflicts"],
                  "examined": plan["examined"], "mode": "shadow"}
        if apply:
            async with db.pool.acquire() as connection:
                result["write_result"] = await apply_plan(connection, plan, coverage_complete=not gaps)
            result["mode"] = "apply"
        return result
    finally:
        if client is not None:
            await client.close()
        await db.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Exchange income owner shadow reconciliation")
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument("--month")
    period.add_argument("--current-month", action="store_true")
    parser.add_argument("--account-id", default=os.environ.get("TRADE_ACCOUNT_ID", "default"))
    parser.add_argument("--apply", action="store_true", help="insert only after full coverage and zero conflicts")
    parser.add_argument("--end-ms", type=int, help="exclusive fact watermark, no later than now minus one second")
    args = parser.parse_args()
    try:
        month = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m") if args.current_month else args.month
        print(json.dumps(asyncio.run(_run(month, args.account_id, args.apply,
                                         args.end_ms)),
                         ensure_ascii=False, sort_keys=True, indent=2, default=str))
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f"reconciliation failed: {exc}\n")


if __name__ == "__main__":
    main()
