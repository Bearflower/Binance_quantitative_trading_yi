import datetime as dt
import json
import sqlite3

from ai_tuner.ops import daily_digest, digest_outbox, hrs_observation as hrs


def test_quote_pair_requires_same_signal_and_both_successful(tmp_path):
    now = dt.datetime.now(digest_outbox.TZ)
    path = tmp_path / "receipts.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE scan_state(key,value);
            CREATE TABLE receipts(evidence_id,phase,status);
            CREATE TABLE tasks(due_at);
            INSERT INTO receipts VALUES ('a','entry','ok'),('b','exit_6h','ok'),
                ('c','entry','ok'),('c','exit_6h','ok'),('d','entry','missing');
        """)
        db.execute("INSERT INTO scan_state VALUES ('scanned_at',?)", (now.isoformat(),))
        db.execute("INSERT INTO tasks VALUES (?)", ((now-dt.timedelta(minutes=2)).isoformat(),))
    text, warnings = hrs.quotes(now, path)
    assert "同信号双侧齐全 1，缺失 1，待采 1" in text
    assert warnings == ["盘口待采逾期 1"]


def test_unavailable_sources_visible_in_archive_without_blocking_digest(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    def unavailable(_now):
        raise FileNotFoundError
    for name in ("acceptance", "modes", "quotes", "boundary_scans"):
        monkeypatch.setattr(hrs, name, unavailable)
    preview = hrs.prepare_digest(dry_run=True)
    assert not list(tmp_path.iterdir())
    assert preview["status"] == "warn" and "盘口读取失败" in preview["text"]
    assert "同信号双侧齐全 0" not in preview["text"]
    hrs.prepare_digest(dry_run=False)
    now = dt.datetime.now(digest_outbox.TZ)
    entries, errors = daily_digest.collect([tmp_path], now.date())
    assert not errors and entries["hrs_observation"]["text"] == preview["text"]
    daily_digest.archive(entries, now)
    assert (tmp_path / "archive" / str(now.date()) / "hrs_observation.json").is_file()

def test_acceptance_reports_only_the_five_selected_lifecycles(monkeypatch):
    now = dt.datetime.now(digest_outbox.TZ)
    report = {"generated_at": now.isoformat(), "database_snapshot_coherent": True,
              "technical": {"consecutive_complete_hours": 24, "required_complete_hours": 24},
              "financial": {"required_lifecycles": 5, "observed_closed_lifecycles": 9,
                            "samples": [{"status": "RECONCILED", "engineering_status": "READY"}] * 5}}
    monkeypatch.setattr(hrs, "output", lambda args: json.dumps(report))
    text, warnings = hrs.acceptance(now)
    assert "完整对账闭仓 5/5，实际观察闭仓 9" in text and not warnings


def test_scan_boundary_displays_only_due_windows(monkeypatch):
    now = dt.datetime(2026, 12, 1, 7, 35, tzinfo=digest_outbox.TZ)
    def read(args, timeout):
        assert args[-2:] == ["2026-11-24", "2026-12-01"]
        return json.dumps({"2026-11-24": ["2026-11-23T16:01:04+00:00", "scan-a"], "2026-12-01": None})
    monkeypatch.setattr(hrs, "output", read)
    text, warnings = hrs.boundary_scans(now)
    assert "00:01:04（scan-a）" in text
    assert warnings == ["2026-12-01 容差内缺首扫描"]


def test_mode_report_can_use_a_separately_released_reader(tmp_path, monkeypatch):
    revision = "a" * 40
    release = tmp_path / ".releases/hrs-mode-report" / revision
    release.mkdir(parents=True)
    monkeypatch.setattr(hrs, "ROOT", tmp_path)

    def output(args, timeout=20):
        if args[0] == "docker":
            return revision
        assert args[1] == str(release / "release/hrs-observer/activate.py")
        live = {"verified_net_count": 0, "observed_closed": 0}
        return json.dumps({"full_window_financial_coverage": False,
                           "modes": {key: {"live": live} for key in
                                     ("standard", "emm", "semi_emm", "lv_rm", "unknown")}})

    monkeypatch.setattr(hrs, "output", output)
    assert "近七日实盘" in hrs.modes(dt.datetime.now(hrs.digest_outbox.TZ))[0]
