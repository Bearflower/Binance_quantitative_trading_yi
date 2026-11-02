"""Historical funding evidence is per symbol and never self-attesting."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from ai_tuner.replay.funding_history import assess_funding_rows, freeze_funding_archive


UTC = timezone.utc
START = datetime(2026, 10, 23, 0, tzinfo=UTC)


def _rows(symbol, hours, *, offset_ms=0):
    return [
        {
            "symbol": symbol,
            "fundingTime": int((START + timedelta(hours=hour)).timestamp() * 1000) + offset_ms,
            "fundingRate": "0.00012345",
        }
        for hour in hours
    ]


def test_four_hour_series_preserves_millisecond_settlement_time():
    result = assess_funding_rows(
        "PUMPUSDT", _rows("PUMPUSDT", (0, 4, 8, 12, 16), offset_ms=4),
        START, START + timedelta(hours=16, milliseconds=5),
    )

    assert result["cadence_hours"] == 4
    assert result["events"][1]["funding_time"] == "2026-10-23T04:00:00.004000Z"
    assert result["gaps"] == ["historical_funding_schedule_unattested"]
    assert result["decision"] == "BLOCKED_DATA"


def test_eight_hour_series_is_not_forced_into_four_hour_schedule():
    result = assess_funding_rows(
        "BTCUSDT", _rows("BTCUSDT", (0, 8, 16, 24)),
        START, START + timedelta(hours=24),
    )

    assert result["cadence_hours"] == 8
    assert len(result["events"]) == 4


def test_missing_middle_settlement_is_a_data_gap():
    result = assess_funding_rows(
        "PUMPUSDT", _rows("PUMPUSDT", (0, 4, 12, 16)),
        START, START + timedelta(hours=16),
    )

    assert "funding_cadence_inconsistent:PUMPUSDT" in result["gaps"]
    assert result["decision"] == "BLOCKED_DATA"


def test_missing_edge_settlement_is_a_data_gap():
    result = assess_funding_rows(
        "PUMPUSDT", _rows("PUMPUSDT", (8, 12, 16)),
        START, START + timedelta(hours=16),
    )

    assert "funding_start_boundary_gap:PUMPUSDT" in result["gaps"]


def test_duplicate_or_wrong_symbol_blocks_instead_of_silent_dedup():
    rows = _rows("BTCUSDT", (0, 8, 16))
    duplicate = assess_funding_rows(
        "BTCUSDT", rows + [rows[1]], START, START + timedelta(hours=16),
    )
    wrong_symbol = assess_funding_rows(
        "BTCUSDT", rows[:-1] + _rows("ETHUSDT", (16,)),
        START, START + timedelta(hours=16),
    )

    assert "funding_duplicate_time:BTCUSDT" in duplicate["gaps"]
    assert "funding_symbol_mismatch:BTCUSDT" in wrong_symbol["gaps"]


def test_freeze_writes_hashed_per_symbol_raw_evidence_without_pass(tmp_path):
    destination = tmp_path / "funding"
    source = {
        "BTCUSDT": _rows("BTCUSDT", (0, 8, 16, 24)),
        "PUMPUSDT": _rows("PUMPUSDT", (0, 4, 8, 12, 16, 20, 24), offset_ms=3),
    }

    result = freeze_funding_archive(
        destination, source, START, START + timedelta(hours=24),
        fetched_at=START + timedelta(days=2),
    )

    assert result["decision"] == "BLOCKED_DATA"
    manifest = json.loads((destination / "funding_manifest.json").read_text())
    assert manifest["source_provenance"] == "unverified"
    assert "source_endpoint" not in manifest
    assert manifest["assessments"]["BTCUSDT"]["cadence_hours"] == 8
    assert manifest["assessments"]["PUMPUSDT"]["cadence_hours"] == 4
    for symbol, info in manifest["files"].items():
        content = (destination / info["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == info["sha256"]
        assert info["rows"] == len(source[symbol])


def test_freeze_never_overwrites_existing_archive(tmp_path):
    destination = tmp_path / "funding"
    source = {"BTCUSDT": _rows("BTCUSDT", (0, 8, 16))}
    first = freeze_funding_archive(
        destination, source, START, START + timedelta(hours=16),
        fetched_at=START + timedelta(days=1),
    )
    before = (destination / "funding_manifest.json").read_bytes()
    second = freeze_funding_archive(
        destination, source, START, START + timedelta(hours=16),
        fetched_at=START + timedelta(days=1),
    )

    assert first["decision"] == "BLOCKED_DATA"
    assert second["gaps"] == ["funding_archive_already_exists"]
    assert (destination / "funding_manifest.json").read_bytes() == before


def test_freeze_caps_rows_before_materializing_unbounded_source(tmp_path):
    destination = tmp_path / "funding"
    row = _rows("BTCUSDT", (0,))[0]
    source = {"BTCUSDT": (row for _ in range(3001))}

    result = freeze_funding_archive(
        destination, source, START, START + timedelta(hours=16),
        fetched_at=START + timedelta(days=1),
    )

    assert result["gaps"] == ["funding_row_limit_exceeded:BTCUSDT"]
    assert not destination.exists()


def test_freeze_rejects_fetch_time_before_window_cutoff(tmp_path):
    destination = tmp_path / "funding"
    result = freeze_funding_archive(
        destination, {"BTCUSDT": _rows("BTCUSDT", (0, 8, 16))},
        START, START + timedelta(hours=16),
        fetched_at=START + timedelta(hours=15),
    )

    assert result["gaps"] == ["funding_fetch_before_as_of"]
    assert not destination.exists()
