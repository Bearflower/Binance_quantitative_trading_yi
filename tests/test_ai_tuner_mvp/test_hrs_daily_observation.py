import datetime as dt
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
    for name in ("acceptance", "modes", "quotes"):
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
