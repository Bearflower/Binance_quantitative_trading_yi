import datetime as dt
import sys
from pathlib import Path

import pytest

from ai_tuner.ops import daily_digest, digest_outbox as outbox

NOW = dt.datetime(2026, 11, 19, 7, 35, tzinfo=outbox.TZ)


def entry(key, text, status="ok", summary="正常"):
    return {
        "key": key,
        "generated_at": NOW,
        "status": status,
        "summary": summary,
        "text": text,
        "path": Path("/nonexistent") / f"{key}.json",
    }


def all_entries():
    return {key: entry(key, f"{key} 正文") for key in outbox.SECTION_KEYS}


def test_render_keeps_section_order_and_headers():
    message = daily_digest.render(all_entries(), NOW)

    assert message.startswith("【交易系统每日巡检汇总】2026-11-19 07:35（北京时间）")
    assert "异常概览：全部正常" in message
    positions = [message.index(f"{index}/7 ") for index in range(1, 8)]
    assert positions == sorted(positions)
    assert "1/7 HRS 生产只读巡检（07:31）" in message
    assert "6/7 生产容器日志巡检（07:34）" in message
    assert "7/7 MTPCS 开仓归因与影子观察（07:35）" in message


def test_missing_section_is_reported_in_body_and_overview():
    entries = all_entries()
    entries.pop("grid")

    message = daily_digest.render(entries, NOW)

    assert "【网格实盘与行情巡检】本次未生成（07:32 任务未产出）" in message
    assert "异常概览：网格实盘与行情 未生成｜其余正常" in message


def test_stale_file_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    yesterday = NOW - dt.timedelta(days=1)
    outbox.publish("hrs", "昨日正文", status="ok", now=yesterday)
    entries, _ = outbox.read_all([tmp_path], NOW.date())
    message = daily_digest.render(entries, NOW)
    assert "HRS 生产只读巡检】本次未生成" in message


def test_oversized_digest_splits_into_two_messages():
    entries = all_entries()
    entries["logs"] = entry("logs", "字" * 30000)

    messages = daily_digest.build_messages(entries, NOW)

    assert len(messages) == 2
    assert daily_digest.payload_bytes(messages[0]) <= daily_digest.MAX_PAYLOAD_BYTES
    assert "续 2/2" in messages[1]
    assert "6/7 生产容器日志巡检（07:34）" in messages[1]


def _prepare_outbox(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    now = dt.datetime.now(outbox.TZ)
    for key in outbox.SECTION_KEYS:
        outbox.publish(key, f"{key} 正文", status="ok", now=now)


def test_send_failure_keeps_outbox_and_does_not_archive(monkeypatch, tmp_path):
    _prepare_outbox(monkeypatch, tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("feishu down")

    monkeypatch.setattr(daily_digest, "send", boom)
    monkeypatch.setattr(sys, "argv", [
        "daily_digest.py", "--outbox-dir", str(tmp_path), "--wait-seconds", "0",
    ])

    with pytest.raises(RuntimeError, match="feishu down"):
        daily_digest.main()

    assert (tmp_path / "hrs.json").exists()
    assert not (tmp_path / "archive").exists()


def test_dry_run_sends_nothing_and_archives_nothing(monkeypatch, tmp_path):
    _prepare_outbox(monkeypatch, tmp_path)
    monkeypatch.setattr(daily_digest, "send", lambda *a, **k: pytest.fail("dry-run 不应发送"))
    monkeypatch.setattr(sys, "argv", ["daily_digest.py", "--outbox-dir", str(tmp_path), "--dry-run"])

    assert daily_digest.main() == 0
    assert (tmp_path / "hrs.json").exists()
    assert not (tmp_path / "archive").exists()


def test_successful_send_archives_used_sections(monkeypatch, tmp_path):
    _prepare_outbox(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(daily_digest, "send", lambda _file, message: sent.append(message))
    monkeypatch.setattr(sys, "argv", [
        "daily_digest.py", "--outbox-dir", str(tmp_path), "--wait-seconds", "0",
    ])

    assert daily_digest.main() == 0
    assert len(sent) == 1
    assert not (tmp_path / "hrs.json").exists()
    assert (tmp_path / "archive" / dt.datetime.now(outbox.TZ).strftime("%Y-%m-%d") / "hrs.json").exists()
