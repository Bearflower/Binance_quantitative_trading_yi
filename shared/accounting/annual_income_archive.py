"""Publish a verified archive prefix with the contiguous live income report."""

import csv
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import hashlib
import io
from pathlib import Path
import zipfile

from .economic_ledger import INCOME_TYPES, build_economic_report

ACCOUNT = "default"
START_MS = 1767196800000  # 2026-01-01 00:00 Asia/Shanghai
END_MS = 1782835200000  # 2026-07-01 00:00 Asia/Shanghai, exclusive
ARCHIVE_PATH = Path("/app/annual-income/pm-income-2026-H1.zip")
ARCHIVE_SHA256 = "618e581db1714f6d98b3641d67d68076c6b4dbb4381828f4a9519e38c3c01fdf"
ARCHIVE_VERSION = "exchange-income-v1-pm-archive-v1"


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
    return build_economic_report([], incomes, [])


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
    if Decimal(result["account_usdt_total"]) != (
            Decimal(result["attributed_usdt_total"]) + Decimal(result["unattributed_usdt_total"])):
        raise ValueError("annual income attribution does not balance")
    result["report_id"] = hashlib.sha256(
        f"{ARCHIVE_VERSION}:{ARCHIVE_SHA256}:{suffix['report_id']}".encode()).hexdigest()
    return result
