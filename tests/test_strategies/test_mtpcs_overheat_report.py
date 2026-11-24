from datetime import datetime, timezone
from itertools import product
import json

from ai_tuner.ops.mtpcs_shadow_summary import summarize


def test_report_counts_only_forward_cohort_and_links_blocked_baseline_closes():
    stamps = ["2026-11-24T00:00:00", "2026-11-24T01:00:00", "2026-11-24T02:00:00"]
    rows = []
    for index, at in enumerate(stamps):
        records = [dict(variant=variant, symbol=symbol, candidate={}, arms=[], exits={})
                   for variant, symbol in product(("btc_eth", "btc_eth_aggressive"),
                                                   ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"))]
        if index > 0:
            for record in records[-5:]:
                record["overheat_study"] = {"start_utc": stamps[1], "blocked": None}
        if index == 0:
            records[-1]["exits"] = {"mean_reversion": {"net_r": 100.0}}
        elif index == 1:
            records[-1]["overheat_study"]["blocked"] = True
            records[-2]["overheat_study"]["blocked"] = False
        else:
            records[-1]["exits"] = {"overheat_baseline": {"net_r": -1.1, "overheat_blocked": True}}
            records[-2]["exits"] = {"overheat_baseline": {"net_r": 0.5, "overheat_blocked": False},
                                   "overheat_filter": {"net_r": 0.5}}
        rows.append((at, '{"positions":{}}', json.dumps(records)))
    report = summarize(rows, datetime(2026, 11, 24, 2, 18, tzinfo=timezone.utc))["overheat_study"]
    assert report["start_utc"] == stamps[1]
    assert report["observed_hours"] == 2
    assert report["candidates"] == 2 and report["blocked"] == 1
    assert report["blocked_closes"] == 1 and report["blocked_net_r"] == -1.1
    assert report["baseline"]["closed"] == 2 and report["baseline"]["net_r"] == -0.6
    assert report["filter"]["closed"] == 1 and report["filter"]["net_r"] == 0.5


def test_digest_displays_unsettled_and_settled_overheat_samples():
    from ai_tuner.ops.mtpcs_observation import overheat_lines

    assert "等待首个完整采集小时" in overheat_lines(None)[0]
    lines = overheat_lines(dict(
        start_utc="2026-11-24T01:00:00", observed_hours=2, candidates=4, blocked=2,
        blocked_closes=1, blocked_net_r=-1.1,
        baseline=dict(closed=1, net_r=-1.1, closed_sequence_drawdown_r=1.1),
        filter=dict(closed=0, net_r=None, closed_sequence_drawdown_r=0),
    ))
    assert "拦截 2 个" in lines[0]
    assert "1 笔，净 -1.10R" in lines[1]
    assert "只加过热禁开 0 笔结算" in lines[2]
