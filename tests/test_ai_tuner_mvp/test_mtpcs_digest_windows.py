import json
from datetime import datetime, timezone
from itertools import product

from ai_tuner.ops.mtpcs_observation import render
from ai_tuner.ops.mtpcs_shadow_summary import summarize

VARIANTS = ("btc_eth", "btc_eth_aggressive")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
START = "2026-11-24T01:00:00"


def hour(stamp, exits, *, study=False, complete=True):
    records = [
        {"variant": variant, "symbol": symbol, "candidate": {}, "arms": [], "exits": {}}
        for variant, symbol in product(VARIANTS, SYMBOLS)
    ]
    records[-1]["exits"] = {arm: {"net_r": gain} for arm, gain in exits.items()}
    if study:
        for record in records[-5:]:
            record["overheat_study"] = {"start_utc": START, "blocked": None}
    return stamp, '{"positions":{}}', json.dumps(records if complete else records[1:])


def test_complete_hours_define_full_and_common_overheat_cutoffs():
    report = summarize([
        hour("2026-11-23T06:00:00", {"mean_reversion": 1.0}),
        hour(START, {"mean_reversion": -1.0, "overheat_baseline": 2.0}, study=True),
        hour("2026-11-24T02:00:00", {"overheat_filter": -1.0}, study=True),
        hour("2026-11-24T03:00:00", {"mean_reversion": 2.0, "overheat_baseline": -9.0}),
        hour("2026-11-24T04:00:00", {"mean_reversion": -9.0}, study=True, complete=False),
    ], datetime(2026, 11, 24, 4, 40, tzinfo=timezone.utc))

    assert report["full_window"] == {
        "start_utc": "2026-11-23T06:00:00", "cutoff_utc": "2026-11-24T03:00:00",
    }
    study = report["overheat_study"]
    assert study["start_utc"] == START
    assert study["cutoff_utc"] == "2026-11-24T02:00:00"
    assert study["baseline"]["closed"] == study["filter"]["closed"] == 1
    assert study["baseline"]["net_r"] == 2.0
    assert study["filter"]["net_r"] == -1.0

    text = render({}, report, [])
    assert "全量影子账 · 北京时间 2026-11-23 14:00 至 2026-11-24 11:00" in text
    assert "均值回归 0候选/3结算，胜率 66.7%（2/3）" in text
    assert "过热同期对照 · 北京时间 2026-11-24 09:00 至 2026-11-24 10:00（两组共同截止）" in text
    assert "同期激进基线 1 笔结算，胜率 100.0%（1/1）" in text
    assert "只加过热禁开 1 笔结算，胜率 0.0%（0/1）" in text


def test_zero_settlements_show_win_rate_unavailable():
    report = summarize(
        [hour(START, {}, study=True)],
        datetime(2026, 11, 24, 1, 40, tzinfo=timezone.utc),
    )
    text = render({}, report, [])
    assert "同期激进基线 0 笔结算，胜率暂无" in text
    assert "只加过热禁开 0 笔结算，胜率暂无" in text
    assert "均值回归 0候选/0结算，胜率暂无" in text
