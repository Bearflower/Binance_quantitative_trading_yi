from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import zipfile

import pytest

from shared.accounting import annual_income_archive as archive
from shared.accounting.economic_ledger import build_economic_report


def test_archive_splice_uses_real_income_ids_without_fabricating_attribution(tmp_path, monkeypatch):
    path = tmp_path / "income.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("income.csv", "Date(UTC),type,Amount,Asset,Symbol,Transaction ID\n"
                   "2026-06-30 15:59:59,COMMISSION,-1.25,USDT,ETHUSDT,1234567890123456789\n"
                   "2026-06-30 15:59:59,TRANSFER,40,USDT,,4\n")
    monkeypatch.setattr(archive, "ARCHIVE_PATH", path)
    monkeypatch.setattr(archive, "ARCHIVE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    archive.load_archive.cache_clear()
    try:
        h1 = archive.load_archive()
        event = dict(account_id="default", income_type="FUNDING_FEE", income=Decimal("0.50"),
                     asset="USDT", symbol="ETHUSDT", tran_id="2", trade_id=None,
                     event_time_utc=datetime.fromtimestamp(archive.END_MS / 1000, timezone.utc))
        h2 = build_economic_report([], [event], [])
        h2.update(account_id="default", start_ms=archive.END_MS, end_ms=archive.END_MS + 1000,
                  account_coverage_complete=True, publication_status="published",
                  report_id="tail", coverage_gaps=[])
        year = archive.combine_archive(h1, h2)
        assert year["account_usdt_total"] == "-0.75"
        assert year["unattributed_usdt_total"] == "-0.75"
        assert year["income_count"] == 2
        assert year["strategy_attribution_complete"] is False
        assert h1["events"][0]["tran_id"] == "1234567890123456789"
        gap = dict(h2, start_ms=archive.END_MS + 1)
        with pytest.raises(ValueError, match="incomplete"):
            archive.combine_archive(h1, gap)
        overlap = deepcopy(h2)
        overlap["events"] += h1["events"]
        with pytest.raises(ValueError, match="overlaps"):
            archive.combine_archive(h1, overlap)
    finally:
        archive.load_archive.cache_clear()
