import json
from datetime import date, datetime, timedelta

import pytest

from ai_tuner.ops import digest_outbox as outbox


def test_publish_writes_atomically_and_leaves_no_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    now = datetime(2026, 11, 19, 7, 31, tzinfo=outbox.TZ)

    path = outbox.publish("hrs", "HRS 正文", status="warn", summary="1 项异常", now=now)

    assert path == tmp_path / "hrs.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["key"] == "hrs"
    assert payload["status"] == "warn"
    assert payload["summary"] == "1 项异常"
    assert payload["text"] == "HRS 正文"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("key,status,text", [
    ("unknown", "ok", "正文"),
    ("hrs", "bad", "正文"),
    ("hrs", "ok", "   "),
])
def test_publish_rejects_invalid_payload(tmp_path, monkeypatch, key, status, text):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        outbox.publish(key, text, status=status)


def test_read_all_skips_corrupt_and_stale_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path))
    today = date(2026, 11, 19)
    outbox.publish("hrs", "今日正文", status="ok", summary="正常",
                   now=datetime(2026, 11, 19, 7, 31, tzinfo=outbox.TZ))
    outbox.publish("grid", "昨日正文", status="ok", summary="正常",
                   now=datetime(2026, 11, 18, 7, 32, tzinfo=outbox.TZ))
    (tmp_path / "logs.json").write_text("{broken", encoding="utf-8")

    entries, warnings = outbox.read_all([tmp_path], today)

    assert set(entries) == {"hrs"}
    assert entries["hrs"]["text"] == "今日正文"
    assert any("logs.json" in warning for warning in warnings)
    assert len(warnings) == 1


def test_read_all_prefers_first_directory(tmp_path):
    host = tmp_path / "host"
    container = tmp_path / "container"
    host.mkdir()
    container.mkdir()
    stamp = datetime(2026, 11, 19, 7, 31, tzinfo=outbox.TZ)

    def write(directory, text, when):
        (directory / "hrs.json").write_text(
            json.dumps({"key": "hrs", "generated_at": when.isoformat(),
                        "status": "ok", "summary": "", "text": text}),
            encoding="utf-8",
        )

    write(host, "宿主版本", stamp)
    write(container, "容器版本", stamp + timedelta(seconds=1))

    entries, _ = outbox.read_all([host, container], stamp.date())
    assert entries["hrs"]["text"] == "宿主版本"
