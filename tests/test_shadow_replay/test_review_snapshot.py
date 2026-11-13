import json
import shutil
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from backtest.btc_eth.replay.snapshot import build_snapshot, verify_snapshot


def snapshot(path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"open_time": int((start + timedelta(minutes=i)).timestamp()*1000)} for i in (0, 2)]
    with patch("backtest.btc_eth.replay.snapshot.fetch_klines_1m", return_value=rows), patch("backtest.btc_eth.replay.snapshot.fetch_funding_rate_history", return_value=[]), patch("backtest.btc_eth.replay.snapshot.fetch_exchange_info", return_value={}):
        build_snapshot("XAUUSDT", start, start+timedelta(minutes=3), str(path))


def test_copy_verifies_consumed_files(tmp_path):
    snapshot(tmp_path / "original")
    shutil.copytree(tmp_path / "original", tmp_path / "copy")
    (tmp_path / "copy/xauusdt_1m.jsonl").write_text('{}\n')
    assert not verify_snapshot(str(tmp_path / "copy/manifest.json"))["files_ok"]


def test_gap_list_cannot_hide_actual_gap(tmp_path):
    snapshot(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["gaps"] = []
    path.write_text(json.dumps(manifest))
    assert verify_snapshot(str(path))["gaps"] > 0
