import json
import sys
import subprocess

from ai_tuner.ops import daily_digest, mtpcs_observation as mtpcs


def deals():
    return {"btc_eth": None, "btc_eth_aggressive": None}


def empty_archive():
    return dict(matched={"btc_eth": 0, "btc_eth_aggressive": 0}, written=0,
                persisted=0, covered_until_ms=1795439500000, up_to_date=True)


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
    assert "7/8 MTPCS 开仓归因与影子观察" in message
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
        if command[:4] == ["docker", "exec", "-i", "ai-tuner"]:
            return empty_archive()
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
    def run(command, **kwargs):
        if command[:4] == ["docker", "exec", "-i", "ai-tuner"]:
            return empty_archive()
        return {"strategies": deals()} if command[0] == "docker" else shadow()

    monkeypatch.setattr(mtpcs, "read_json", run)
    assert mtpcs.prepare_digest(dry_run=False) is None
    assert json.loads((tmp_path / "state/first_entries.json").read_text()) == deals()
    entry = json.loads((tmp_path / "outbox/mtpcs.json").read_text())
    assert entry["status"] == "ok"
    assert "等待发布后首笔真实入场成交" in entry["text"]


def test_eta_reports_remaining_days_and_handles_zero_rate():
    report = shadow()
    report["recent_complete_days"] = 5
    report["arms"][0]["closed"] = 10
    report["arms"][0]["recent_closed"] = 5
    assert mtpcs.eta(report["arms"][0], report) == "剩20笔，约20天"
    report["arms"][0]["recent_closed"] = 0
    assert mtpcs.eta(report["arms"][0], report) == "剩20笔，估时待结算"
    report["arms"][0]["closed"] = 30
    assert mtpcs.eta(report["arms"][0], report) == "已达初看样本"


def test_eta_shows_replay_reference_only_when_observed_days_are_insufficient():
    report = shadow()
    report["recent_complete_days"] = 1
    baseline = dict(variant="btc_eth_aggressive", arm="mean_reversion", closed=3)
    assert mtpcs.eta(baseline, report) == "剩27笔，历史回放约9天"
    trend = dict(variant="btc_eth_aggressive", arm="trend_fibonacci", closed=0)
    assert mtpcs.eta(trend, report) == "剩30笔，估时待候选"


def test_daily_conclusion_uses_yesterday_and_reports_partial_coverage():
    report = shadow()
    report.update(recent_complete_days=0, as_of_day="2026-11-24", daily={
        "date": "2026-11-23", "covered_hours": 10,
        "trend_candidates": 0, "reversion_closed": 0,
        "alignment_passed": 6, "alignment_blocked": 0, "bands": [0, 0],
    })
    text = mtpcs.render(deals(), report, [])
    assert "还要等多久" in text
    assert "11-23 采集 10/24 小时" in text
    assert "方向一致性通过 6、拦截 0" in text
    assert "趋势候选 0" in text and "回归结算 0" in text
    assert "距最早复评日 69 天" in text


def test_shadow_rollup_uses_only_complete_previous_days_for_daily_rate():
    from datetime import datetime, timedelta, timezone
    from ai_tuner.ops.mtpcs_shadow_summary import summarize

    origin = datetime(2026, 11, 23, 16)
    rows = []
    for offset in range(25):
        at = origin + timedelta(hours=offset)
        records = [
            dict(variant=variant, symbol=symbol, candidate={"market_state": "RANGING"},
                 arms=[], exits={})
            for variant in mtpcs.STRATEGIES
            for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
        ]
        records[-1].update(arms=["mean_reversion"], alignment_passed=False,
                           alignment_reason="方向一致性拒绝",
                           exits={"mean_reversion": {"net_r": 0.25 if offset < 24 else 3.0}})
        rows.append((at.isoformat(), '{"positions":{}}', json.dumps(records)))

    report = summarize(rows, datetime(2026, 11, 24, 17, 5, tzinfo=timezone.utc))
    assert report["daily"]["date"] == "2026-11-24"
    assert report["daily"]["covered_hours"] == 24
    assert report["daily"]["alignment_blocked"] == 24
    assert report["daily"]["reversion_by_variant"]["btc_eth_aggressive"]["net_r"] == 6.0
    assert report["recent_complete_days"] == 1
    item = next(r for r in report["arms"]
                if (r["variant"], r["arm"]) == ("btc_eth_aggressive", "mean_reversion"))
    assert item["closed"] == 25 and item["recent_closed"] == 24

    # One missing hour takes the day out of the rate, retaining its actual tally.
    incomplete = summarize(rows[1:], datetime(2026, 11, 24, 17, 5, tzinfo=timezone.utc))
    assert incomplete["daily"]["covered_hours"] == 23
    assert incomplete["recent_complete_days"] == 0


def test_order_archive_runs_after_both_first_entries_are_cached(tmp_path, monkeypatch):
    cached = {
        key: dict(state="verified", intent="trend", direction="LONG", order_id=str(i + 1),
                  filled_at_utc="2026-11-23T07:11:00+00:00", symbol="ETHUSDT", filled_qty="1")
        for i, key in enumerate(mtpcs.STRATEGIES)
    }
    (tmp_path / "first_entries.json").write_text(json.dumps(cached))
    archive_calls = []

    def run(command, **kwargs):
        if command[0] == "docker":
            assert command[3] == "ai-tuner"
            archive_calls.append(command[-2:])
            return dict(matched={"btc_eth": 1, "btc_eth_aggressive": 1}, written=0,
                        persisted=2, covered_until_ms=1795439500000, up_to_date=True)
        return shadow()

    monkeypatch.setattr(mtpcs, "read_json", run)
    text, errors = mtpcs.prepare(tmp_path, tmp_path / "ledger", persist=False)
    assert errors == [] and "订单归档" in text
    assert archive_calls == [["", "preview"]]
    checkpoint = tmp_path / "order_archive_checkpoint.json"
    assert not checkpoint.exists()

    mtpcs.prepare(tmp_path, tmp_path / "ledger", persist=True)
    assert json.loads(checkpoint.read_text()) == {"covered_until_ms": 1795439500000}
    mtpcs.prepare(tmp_path, tmp_path / "ledger", persist=True)
    assert archive_calls[-2:] == [["", "persist"], ["1795439500000", "persist"]]


def test_archive_failure_retains_only_last_successful_checkpoint(tmp_path, monkeypatch):
    (tmp_path / "order_archive_checkpoint.json").write_text('{"covered_until_ms": 1795439500000}')
    checkpoint = tmp_path / "order_archive_checkpoint.json"

    def run(command, **kwargs):
        if command[:4] == ["docker", "exec", "-i", "ai-tuner"]:
            raise subprocess.TimeoutExpired(command, 1)
        if command[0] == "docker":
            return {"strategies": deals()}
        return shadow()

    monkeypatch.setattr(mtpcs, "read_json", run)
    text, errors = mtpcs.prepare(tmp_path, tmp_path / "ledger", persist=True)
    assert errors == ["订单归档（TimeoutExpired）"]
    assert "订单归档（TimeoutExpired）" in text
    assert json.loads(checkpoint.read_text()) == {"covered_until_ms": 1795439500000}
