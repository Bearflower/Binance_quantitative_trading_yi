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


def test_proof_corrects_owner_totals_and_exposes_only_verified_sample():
    time = datetime(2026, 6, 8, tzinfo=timezone.utc)
    income = dict(account_id="default", income_type="FUNDING_FEE", tran_id="10", symbol="BTCUSDT",
                  asset="USDT", income=Decimal("0.25"), event_time_utc=time)
    row = dict(income, strategy="btc_eth", event_time_utc=time.isoformat(), income="0.25")
    closure = dict(strategy="btc_eth", pnl="-1.00", start=time.isoformat(),
                   fill_ids=["1", "2"], order_ids=["1", "2"])
    proof = dict(version=1, archive_sha256=archive.ARCHIVE_SHA256, attributions=[row],
                 closures=[closure])
    prefix = archive.apply_proof([income], proof)
    suffix = build_economic_report([], [], [])
    suffix.update(account_id="default", start_ms=archive.END_MS, end_ms=archive.END_MS + 1000,
                  account_coverage_complete=True, publication_status="published",
                  report_id="tail", coverage_gaps=[])
    report = archive.combine_archive(prefix, suffix)
    assert report["by_strategy"]["btc_eth"]["net_usdt"] == "0.25"
    assert report["unattributed_count"] == 0
    assert report["strategy_attribution_complete"] is False
    stats = archive.archive_lifecycle_stats(report, ["btc_eth", "new_coin"])
    assert stats["account"]["closed_count"] == 1
    assert stats["account"]["win_rate"] == 0
    assert stats["account"]["reason"] == "archive_verified_sample_only"
    assert stats["strategies"]["new_coin"]["win_rate"] is None
    live = dict(account=dict(stats["account"], win_count=1, loss_count=0,
                             sample_start="2026-07-07T02:00:00+00:00"),
                strategies=dict(btc_eth=stats["strategies"]["new_coin"], new_coin=stats["account"]))
    combined = archive.combine_lifecycle_samples(stats, live, ["btc_eth", "new_coin"])
    assert combined["account"]["win_rate"] == 50
    assert combined["account"]["archive_count"] == combined["account"]["live_count"] == 1
    assert combined["strategies"]["btc_eth"]["live_count"] == 0

    with pytest.raises(ValueError, match="does not match"):
        archive.apply_proof([income], dict(proof, attributions=[dict(row, income="0.35")]))


def test_manual_history_keeps_proven_owner_and_exchange_totals():
    opened = datetime(2026, 3, 3, tzinfo=timezone.utc)
    rows = [dict(account_id="default", income_type="COMMISSION", tran_id=str(n),
                 symbol="BTCUSDT", asset="USDT", income=Decimal("-1"),
                 event_time_utc=opened) for n in (1, 2)]
    original = build_economic_report([], rows, [], derived_attributions=[
        dict(rows[0], strategy="btc_eth", method="verified", evidence={"order": "proof"}, algorithm_version=1)
    ])
    original.update(end_ms=archive.END_MS, report_id="year", archive_closures=[])
    decisions = [dict(row, amount="-1", strategy="btc_eth_aggressive",
                      event_time_utc=opened.isoformat(), opening_time_utc=opened.isoformat()) for row in rows]
    supplement = dict(account_id="default", version=1, cutoff_ms=archive.END_MS,
                      attributions=decisions, closures=[])
    updated = archive.apply_manual_history(original, supplement)
    assert updated["manual_attribution_count"] == 1
    assert updated["account_usdt_total"] == original["account_usdt_total"]
    assert {row["tran_id"]: row["strategy"] for row in updated["events"]} == {
        "1": "btc_eth", "2": "btc_eth_aggressive",
    }
    assert updated["automatic_events"][1]["strategy"] is None

    changed = dict(decisions[1], amount="-1.01")
    with pytest.raises(ValueError, match="does not match"):
        archive.apply_manual_history(original, dict(supplement, attributions=[changed]))
