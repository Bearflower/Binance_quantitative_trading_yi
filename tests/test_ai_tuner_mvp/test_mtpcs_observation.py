import json
import sys
import subprocess

from ai_tuner.ops import daily_digest, mtpcs_observation as mtpcs


def deals():
    return {"btc_eth": None, "btc_eth_aggressive": None}


def shadow():
    return dict(first_utc="2026-11-23T06:00:00", last_utc="2026-11-23T07:00:00",
                hours=2, missing_count=0, incomplete_hours_utc=[], alignment={"allowed": 2},
                arms=[dict(variant="btc_eth_aggressive", arm="mean_reversion_aligned",
                           closed=0, candidates=2, open=1, pending=0, net_r=None)])


def test_default_0735_preview_merges_mtpcs_without_sending(monkeypatch, capsys):
    calls = []

    def produce(*, dry_run):
        calls.append(dry_run)
        return dict(text=mtpcs.render(deals(), shadow(), []), status="ok", summary="")

    monkeypatch.setattr(mtpcs, "prepare_digest", produce)
    monkeypatch.setattr(daily_digest, "collect", lambda *_: ({}, []))
    monkeypatch.setattr(daily_digest, "send", lambda *_: calls.append("sent"))
    monkeypatch.setattr(sys, "argv", ["daily_digest.py", "--dry-run"])
    assert daily_digest.main() == 0
    message = capsys.readouterr().out
    assert calls == [True]
    assert "7/7 MTPCS 开仓归因与影子观察" in message
    assert "只加方向一致性 2候选/0结算/1在途" in message
    assert "暂无收益" in message
    assert "净 +0" not in message


def test_first_verified_entry_retained_on_source_failure(tmp_path, monkeypatch):
    cached = {
        "btc_eth": dict(state="verified", intent="trend", direction="LONG",
                        filled_at_utc="2026-11-23T07:11:00+00:00", symbol="ETHUSDT",
                        order_id="123", filled_qty="1"),
        "btc_eth_aggressive": None,
    }
    p = tmp_path / "first_entries.json"
    p.write_text(json.dumps(cached))

    def read(command, **kwargs):
        if command[0] == "docker":
            raise subprocess.TimeoutExpired(command, 1)
        return shadow()

    monkeypatch.setattr(mtpcs, "read_json", read)
    text, errors = mtpcs.prepare(tmp_path, tmp_path / "ledger", persist=True)
    assert "原版 · 已验收" in text and "订单 123" in text
    assert "激进版 · 本轮实盘证据读取失败" in text
    assert errors == ["成交与归因（TimeoutExpired）"]
    assert json.loads(p.read_text()) == cached


def test_successful_observation_publishes_section_and_keeps_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(mtpcs, "STATE", tmp_path / "state")
    monkeypatch.setenv("DIGEST_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setattr(mtpcs, "read_json", lambda command, **kw:
                        {"strategies": deals()} if command[0] == "docker" else shadow())
    assert mtpcs.prepare_digest(dry_run=False) is None
    assert json.loads((tmp_path / "state/first_entries.json").read_text()) == deals()
    entry = json.loads((tmp_path / "outbox/mtpcs.json").read_text())
    assert entry["status"] == "ok"
    assert "等待发布后首笔真实入场成交" in entry["text"]
