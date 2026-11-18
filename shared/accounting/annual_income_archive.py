"""Publish a verified archive prefix with the contiguous live income report."""

import csv
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import zipfile

from .economic_ledger import INCOME_TYPES, build_economic_report
from .dashboard_performance import unavailable_lifecycle_stats

ACCOUNT = "default"
START_MS = 1767196800000  # 2026-01-01 00:00 Asia/Shanghai
END_MS = 1782835200000  # 2026-07-01 00:00 Asia/Shanghai, exclusive
ARCHIVE_PATH = Path("/app/annual-income/pm-income-2026-H1.zip")
ARCHIVE_SHA256 = "618e581db1714f6d98b3641d67d68076c6b4dbb4381828f4a9519e38c3c01fdf"
ARCHIVE_VERSION = "exchange-income-v1-pm-archive-v1"
PROOF_VERSION = "exchange-income-v1-pm-archive-v2"
PROOF_PATH = Path("/app/annual-income/pm-h1-strategy-proof-v1.json")
PROOF_SHA256 = "4b25e412a90f62a74595ec95f7bd0a4f1850a340d353d33ae58d913c258ada4f"
STRATEGIES = frozenset(("btc_eth", "btc_eth_aggressive", "hrs", "new_coin"))



@lru_cache(maxsize=1)
def load_archive():
    payload = ARCHIVE_PATH.read_bytes()
    if hashlib.sha256(payload).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("annual income archive hash mismatch")
    incomes = []
    seen = set()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        if len(files) != 1 or files[0].file_size > 2_000_000:
            raise ValueError("unexpected annual income archive contents")
        with io.TextIOWrapper(archive.open(files[0]), encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["Date(UTC)", "type", "Amount", "Asset", "Symbol", "Transaction ID"]:
                raise ValueError("unexpected annual income archive schema")
            for row in reader:
                kind = row["type"]
                if kind not in INCOME_TYPES:
                    continue
                at = datetime.strptime(row["Date(UTC)"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                amount = Decimal(row["Amount"])
                tran_id = row["Transaction ID"]
                key = (kind, tran_id)
                if (not START_MS <= at.timestamp() * 1000 < END_MS
                        or not amount.is_finite() or not tran_id.isdecimal() or key in seen
                        or not row["Asset"]):
                    raise ValueError("invalid or duplicate annual income archive event")
                seen.add(key)
                incomes.append(dict(account_id=ACCOUNT, tran_id=tran_id, symbol=row["Symbol"],
                                    income_type=kind, income=amount, asset=row["Asset"],
                                    event_time_utc=at, trade_id=None))
    if not incomes:
        raise ValueError("empty annual income archive")
    if not PROOF_PATH.is_file():
        return build_economic_report([], incomes, [])
    payload = PROOF_PATH.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PROOF_SHA256:
        raise ValueError("annual strategy evidence digest mismatch")
    return apply_proof(incomes, json.loads(payload))


def apply_proof(incomes, proof):
    """Apply only archived income identities supported by the reviewed evidence."""
    if proof["version"] != 1 or proof["archive_sha256"] != ARCHIVE_SHA256:
        raise ValueError("annual strategy proof scope mismatch")
    facts = {(row["income_type"], row["tran_id"]): row for row in incomes}
    derived = []
    for row in proof["attributions"]:
        event_time = datetime.fromisoformat(row["event_time_utc"])
        fact = facts.get((row["income_type"], row["tran_id"]))
        if (not fact or row["account_id"] != ACCOUNT or row["strategy"] not in STRATEGIES
                or row["symbol"] != fact["symbol"] or row["asset"] != fact["asset"]
                or Decimal(row["income"]) != fact["income"] or event_time != fact["event_time_utc"]):
            raise ValueError("annual strategy proof does not match income")
        derived.append(dict(row, event_time_utc=event_time, algorithm_version=1,
                            method=row.get("method", "archived_unique_fill_order"),
                            evidence={"source": "verified_archive"}))
    if len({(r["income_type"], r["tran_id"]) for r in derived}) != len(derived):
        raise ValueError("duplicate archived strategy attribution")

    result = build_economic_report([], incomes, [], derived_attributions=derived)
    count = len(derived)
    if result["income_count"] - result["unattributed_count"] != count:
        raise ValueError("archived strategy attribution count mismatch")
    result["archive_attribution_count"] = count
    result["archive_closures"] = proof["closures"]
    return result


def archive_lifecycle_stats(report, strategy_ids):
    """H1 proof supplies only completed positions with all costs reconciled."""
    result = unavailable_lifecycle_stats("no_verified_closures", strategy_ids)
    for key, strategy in [("account", None)] + [("strategies", sid) for sid in strategy_ids]:
        sample = [r for r in report["archive_closures"] if strategy is None or r["strategy"] == strategy]
        if not sample:
            continue
        wins = sum(Decimal(row["pnl"]) > 0 for row in sample)
        losses = sum(Decimal(row["pnl"]) < 0 for row in sample)
        metrics = dict(
            order_count=len({order for row in sample for order in row["order_ids"]}),
            fill_count=sum(len(row["fill_ids"]) for row in sample),
            closed_count=len(sample), win_count=wins, loss_count=losses,
            win_rate=round(100 * wins / len(sample), 2), sample_start=min(r["start"] for r in sample),
            sample_partial=True, reason="archive_verified_sample_only",
            caliber="exchange_verified_archived_position_v1")
        if strategy is None:
            result[key] = metrics
        else:
            result[key][strategy] = metrics
    return result


def combine_archive(prefix, suffix):
    """Accept only a verified, nonoverlapping suffix for the pinned account."""
    if (suffix["account_id"] != ACCOUNT or suffix["start_ms"] != END_MS
            or suffix["end_ms"] <= END_MS or not suffix["account_coverage_complete"]
            or suffix["publication_status"] not in ("published", "stale")):
        raise ValueError("annual income suffix is incomplete")
    keys = {(row["income_type"], row["tran_id"]) for row in prefix["events"]}
    if keys.intersection((row["income_type"], row["tran_id"]) for row in suffix["events"]):
        raise ValueError("annual income archive overlaps live event identities")

    result = dict(suffix, start_ms=START_MS, report_version=ARCHIVE_VERSION,
                  income_archive_end_ms=END_MS, strategy_attribution_complete=False,
                  id_match_and_coverage_complete=False,
                  coverage_gaps=["archive:strategy_attribution"] + suffix["coverage_gaps"])
    for key in ("events", "unattributed"):
        result[key] = prefix[key] + suffix[key]
    for key in ("income_count", "unattributed_count"):
        result[key] = prefix[key] + suffix[key]
    for key in ("account_usdt_total", "unattributed_usdt_total", "unattributed_usdt_absolute"):
        result[key] = str(Decimal(prefix[key]) + Decimal(suffix[key]))
    for section in ("account_components", "other_asset_totals"):
        a, b = prefix[section], suffix[section]
        result[section] = {key: str(Decimal(a.get(key, "0")) + Decimal(b.get(key, "0")))
                           for key in sorted(a.keys() | b.keys())}
    if "archive_attribution_count" in prefix:
        result.update(report_version=PROOF_VERSION,
                      archive_attribution_count=prefix["archive_attribution_count"],
                      archive_closures=prefix["archive_closures"])
        result["by_strategy"] = {}
        for sid in prefix["by_strategy"].keys() | suffix["by_strategy"].keys():
            a, b = prefix["by_strategy"].get(sid, {}), suffix["by_strategy"].get(sid, {})
            result["by_strategy"][sid] = {k: str(Decimal(a.get(k, "0")) + Decimal(b.get(k, "0")))
                                          for k in sorted(a.keys() | b.keys())}
        result["attributed_usdt_total"] = str(Decimal(prefix["attributed_usdt_total"])
                                               + Decimal(suffix["attributed_usdt_total"]))
    if Decimal(result["account_usdt_total"]) != (
            Decimal(result["attributed_usdt_total"]) + Decimal(result["unattributed_usdt_total"])):
        raise ValueError("annual income attribution does not balance")
    result["report_id"] = hashlib.sha256(
        f"{result['report_version']}:{ARCHIVE_SHA256}:{PROOF_SHA256}:{suffix['report_id']}".encode()).hexdigest()
    return result
