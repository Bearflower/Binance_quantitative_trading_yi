from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


PROBE = Path(__file__).resolve().parents[2] / "release/mtpcs-shadow/healthcheck.py"


def write_ledger(ledger, at, records="[{}]"):
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE hours (at TEXT PRIMARY KEY, records TEXT)")
        db.execute("INSERT INTO hours VALUES (?, ?)", (at, records))


@pytest.mark.parametrize(("now", "hour", "expected"), [
    ("08:14", "07:00", 0),
    ("08:15", "07:00", 1),
    ("08:15", "08:00", 0),
    ("08:15", "09:00", 1),
])
def test_hourly_cadence_and_grace(tmp_path, now, hour, expected):
    ledger = tmp_path / "entries.sqlite3"
    write_ledger(ledger, f"2026-11-24T{hour}:00")
    now_text = datetime.fromisoformat(f"2026-11-24T{now}:00").replace(tzinfo=timezone.utc).isoformat()
    result = subprocess.run([sys.executable, str(PROBE), str(ledger), "--now", now_text],
                            capture_output=True, text=True)
    assert result.returncode == expected
    assert f"2026-11-24T{hour}:00" in result.stdout


@pytest.mark.parametrize("contents", ["missing", "empty", "malformed", "bad-time"])
def test_invalid_ledger_is_unhealthy_without_mutating_it(tmp_path, contents):
    ledger = tmp_path / "entries.sqlite3"
    if contents == "empty":
        write_ledger(ledger, "2026-11-24T08:00:00", "[]")
    if contents == "malformed":
        ledger.write_text("not a database")
    if contents == "bad-time":
        write_ledger(ledger, "not-an-hour")
    original = ledger.read_bytes() if ledger.exists() else None
    result = subprocess.run([sys.executable, str(PROBE), str(ledger), "--now", "2026-11-24T08:15:00+00:00"],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "ledger" in result.stdout
    assert (ledger.read_bytes() if ledger.exists() else None) == original
