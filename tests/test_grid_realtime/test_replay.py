"""replay.py 单元/集成测试：加载、跃迁扫描、驱动、指标、审计指纹、实验、CLI（AC-15/16/24/31）。"""
import json
from decimal import Decimal

import pytest

from factories import make_trade
from strategies.grid.realtime import replay
from strategies.grid.realtime.features import SecondSample
from strategies.grid.realtime.replay import (Fact, FactScanner, ENTRY,
                                            JSON_SHA256, apply_anchor_gap,
                                            build_snapshot, deep_merge,
                                            drive_engine, first_return_breach,
                                            load_research_file, load_samples_db,
                                            load_trades_json, run_audit,
                                            run_experiment, save_results,
                                            scan_facts_db, seconds_from_trades,
                                            summarize)
from strategies.grid.realtime.rules import DOWN, UP, parse_profile
from strategies.grid.realtime.state import AlertEvent

# ───────────────────── 数据加载与 SHA ─────────────────────

def test_load_json_sha_ok():
    trades = load_trades_json(replay.DEFAULT_JSON, JSON_SHA256)
    assert len(trades) > 0 and trades[0].price > 0


def test_load_json_sha_mismatch_raises():
    with pytest.raises(ValueError):
        load_trades_json(replay.DEFAULT_JSON, "0" * 64)


def test_load_json_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_trades_json(tmp_path / "none.json", JSON_SHA256)


def test_apply_anchor_gap_o_c3():
    samples = [
        SecondSample(3000, Decimal("100"), 1000, 1),   # 锚距 2s：保留
        SecondSample(4000, Decimal("100"), 1000, 1)]   # 锚距 3s：None
    apply_anchor_gap(samples, 2)
    assert samples[0].price == Decimal("100")
    assert samples[1].price is None and samples[1].anchor_time_ms == 1000


def test_seconds_from_trades_range():
    trades = [make_trade(1, 1_500, "100"), make_trade(2, 5_500, "101")]
    seconds = seconds_from_trades(trades, 2)
    # 起点 floor(1500)+1s=2000，上界 floor(5500)=5000
    assert [s.sample_ms for s in seconds] == [2000, 3000, 4000, 5000]
    assert seconds[0].price == Decimal("100")
    # t=5000：101 那笔在 5500（晚于决策点），锚仍是 1500、距 3.5s → None
    assert seconds[-1].price is None


# ───────────────────── 跃迁式事实扫描 ─────────────────────

def test_scanner_entry_transitions(snap):
    sc = FactScanner(snap, Decimal("0.75"))
    assert sc.feed(1, Decimal("2630")) == []  # 首笔仅设 prev
    facts = sc.feed(2, Decimal("2623.00"))
    assert len(facts) == 1 and facts[0].kind == ENTRY and facts[0].direction == DOWN
    # 留在非临界缓冲：不重复 entry
    assert sc.feed(3, Decimal("2620")) == []
    sc2 = FactScanner(snap, Decimal("0.75"))
    sc2.feed(1, Decimal("2767"))
    facts = sc2.feed(2, Decimal("2768"))
    assert len(facts) == 1 and facts[0].direction == UP


def test_scanner_critical_transition(snap):
    sc = FactScanner(snap, Decimal("0.75"))
    sc.feed(1, Decimal("2610"))  # u≈0.562
    facts = sc.feed(2, Decimal("2605"))  # u≈0.769
    assert len(facts) == 1 and facts[0].kind == "critical"


def test_scanner_up_critical_and_boundary(snap):
    # UP 临界：u_up 从 .708 跃迁到 .791（仍 <SU）
    sc = FactScanner(snap, Decimal("0.75"))
    sc.feed(1, Decimal("2785"))
    facts = sc.feed(2, Decimal("2787"))
    assert len(facts) == 1
    assert facts[0].kind == "critical" and facts[0].direction == UP
    # UP 终止价：2793 > SU；u>1 不满足 critical，只产 boundary
    sc2 = FactScanner(snap, Decimal("0.75"))
    sc2.feed(1, Decimal("2785"))
    facts = sc2.feed(2, Decimal("2793"))
    assert len(facts) == 1
    assert facts[0].kind == "boundary" and facts[0].direction == UP


def test_scanner_boundary_gap_flags(snap):
    sc = FactScanner(snap, Decimal("0.75"))
    sc.feed(1, Decimal("2630"))  # prev 仍在网格侧（>L）
    facts = sc.feed(2, Decimal("2599"))
    # 同笔同时跨 L（entry）与 SL（boundary）
    boundary = next(f for f in facts if f.kind == "boundary")
    assert boundary.gap and boundary.direction == DOWN
    sc2 = FactScanner(snap, Decimal("0.75"))
    sc2.feed(1, Decimal("2600"))  # prev 已在缓冲
    facts = sc2.feed(2, Decimal("2599"))
    assert len(facts) == 1 and facts[0].gap is False


def _make_aggtrade_db(path):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE agg_trades (agg_trade_id INTEGER, symbol TEXT, "
        "price TEXT, quantity TEXT, first_trade_id INTEGER, "
        "last_trade_id INTEGER, trade_time_ms INTEGER, is_buyer_maker INTEGER, "
        "received_at_ms INTEGER)")
    conn.executemany(
        "INSERT INTO agg_trades VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, "ETHUSDT", "2630", "0.1", 1, 1, 1000, 0, 1000),
         (2, "ETHUSDT", "2623", "0.1", 2, 2, 2000, 0, 2000)])
    conn.commit()
    conn.close()


def test_scan_facts_db_ro(snap, tmp_path):
    db = tmp_path / "t.sqlite"
    _make_aggtrade_db(db)
    facts = scan_facts_db(db, "ETHUSDT", snap, Decimal("0.75"))
    assert len(facts) == 1 and facts[0].kind == ENTRY
    with pytest.raises(FileNotFoundError):
        scan_facts_db(tmp_path / "none", "ETHUSDT", snap, Decimal("0.75"))


def test_load_samples_db(snap, tmp_path):
    import sqlite3
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE price_samples_1s (sample_ms INTEGER, symbol TEXT, "
        "price TEXT, trade_count INTEGER, last_trade_id INTEGER, "
        "anchor_time_ms INTEGER, materialized_at_ms INTEGER)")
    conn.execute("INSERT INTO price_samples_1s VALUES (1000,'ETHUSDT','2630',1,9,900,1)")
    conn.commit()
    conn.close()
    samples = load_samples_db(db, "ETHUSDT")
    assert samples == [SecondSample(1000, Decimal("2630"), 900, 9)]


# ───────────────────── 驱动、涨跌幅基线 ─────────────────────

def test_drive_engine_fact_ordering(profile_raw):
    cfg = parse_profile(profile_raw)
    seconds = [
        SecondSample(1000, Decimal("2630"), 1000, 1),
        SecondSample(2000, Decimal("2623"), 2000, 2)]
    facts = [
        Fact(1500, DOWN, ENTRY, Decimal("2625")),
        Fact(2000, DOWN, "critical", Decimal("2623"))]
    engine = drive_engine(cfg, seconds=seconds, facts=facts, snap=None)
    # 无参考：事实不触发事件，但 entry 在 MISSING 下仍记录
    assert engine.runtimes[DOWN].entries == [1500]


def test_first_return_breach():
    trades = [make_trade(1, 0, "100")]
    trades += [make_trade(i, (i - 1) * 1000, f"{100 - i * 0.1}")
               for i in range(1, 130)]
    # t=60s：94.0/100-1=-0.06 达 -0.5%
    assert first_return_breach(trades, 60_000, Decimal("0.005")) == 60_000
    # 无窗口参考、无跌幅：None
    nohit = [make_trade(1, 0, "100"), make_trade(2, 1000, "100.1")]
    assert first_return_breach(nohit, 60_000, Decimal("0.005")) is None


def test_json_default_typeerror():
    with pytest.raises(TypeError):
        replay._json_default(object())


# ───────────────────── 指标分支 ─────────────────────

def _notice(t: int) -> AlertEvent:
    return AlertEvent("e", "ep", "grid-ev-test", DOWN, "NOTICE", "normal",
                      t, Decimal("2650"))


def test_notice_outcome_classifications(snap):
    inside = Decimal("0.45")
    base = 1_000_000
    mk = lambda t, p: SecondSample(t, Decimal(p), t, 1)
    # 恢复（g≥0.45 → 2700：g=.529）：false_alarm
    secs = [mk(base + 60_000, "2700")]
    out = replay._notice_outcome(_notice(base), base + 900_000, [], secs,
                                 snap, inside)
    assert out == "false_alarm"
    # 入缓冲：valid
    secs = [mk(base + 60_000, "2620")]
    out = replay._notice_outcome(_notice(base), base + 900_000, [], secs,
                                 snap, inside)
    assert out == "valid"
    # UP notice 后进入上方缓冲：valid
    up_notice = AlertEvent("e", "ep", "grid-ev-test", UP, "NOTICE", "normal",
                           base, Decimal("2750"))
    secs = [mk(base + 60_000, "2780")]
    out = replay._notice_outcome(up_notice, base + 900_000, [], secs,
                                 snap, inside)
    assert out == "valid"
    # 停留区间中部（数据覆盖整个窗口）：unresolved
    secs = [mk(base + 60_000, "2650"), mk(base + 900_000, "2650")]
    out = replay._notice_outcome(_notice(base), base + 900_000, [], secs,
                                 snap, inside)
    assert out == "unresolved"
    # 窗口超出数据末端：truncated
    secs = [mk(base + 10_000, "2650")]
    out = replay._notice_outcome(_notice(base), base + 900_000, [], secs,
                                 snap, inside)
    assert out == "truncated"


def test_summarize_empty(profile_raw):
    cfg = parse_profile(profile_raw)
    seconds = [SecondSample(1000, Decimal("2700"), 1000, 1)]
    engine = drive_engine(cfg, seconds=seconds, facts=[], snap=None)
    result = summarize(engine, seconds)
    assert set(result["directions"]) == {"DOWN", "UP"}
    assert result["events_total"] == 0


# ───────────────────── 研究文件、快照 ─────────────────────

def test_build_snapshot_and_deep_merge():
    raw = {
        "reference_id": "r1", "symbol": "ETHUSDT", "calculated_at_ms": 1,
        "effective_at_ms": 1, "grid_lower": "2623.52", "grid_upper": "2767.96",
        "stop_lower": "2599.45", "stop_upper": "2792.03"}
    snap = build_snapshot(raw)
    assert snap.grid_lower == Decimal("2623.52")
    merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}, "b": 4})
    assert merged == {"a": {"x": 1, "y": 3}, "b": 4}


def test_load_research_file_10_profiles():
    snap, profiles = load_research_file(replay.DEFAULT_PROFILES)
    assert len(profiles) == 10
    assert snap.reference_id == "grid-ev-20261007-0905"
    assert "research_seed_02.e60.s1" in profiles


# ───────────────────── 审计指纹（AC-15/16/24） ─────────────────────

def test_run_audit_fixed_fingerprints():
    block = run_audit(replay.DEFAULT_JSON, replay.DEFAULT_PROFILES)
    assert block["entry_ms"] == 1791338474591
    assert block["critical_ms"] == 1791338477251
    assert block["boundary_ms"] == 1791338477997
    assert block["critical_lead_seconds"] == 0.746
    assert block["four_efficiency"] == [0.346498, 0.245597, 0.251059, 0.324966]
    assert block["first_5m_0.5pct_ms"] == 1791338222054
    assert block["first_3m_0.5pct_ms"] == 1791338327096


# ───────────────────── 全库实验固定输出（AC-24/31，约 60s） ─────────────────────

def test_experiment_matches_saved_results():
    data = run_experiment(replay.DEFAULT_DB, replay.DEFAULT_PROFILES)
    fresh = json.dumps(data, ensure_ascii=False, indent=2,
                       default=replay._json_default, sort_keys=True)
    saved = replay.DEFAULT_RESULTS.read_text(encoding="utf-8").rstrip("\n")
    assert json.loads(fresh)["snapshot_id"] == "grid-ev-20261007-0905"
    assert len(json.loads(fresh)["results"]) == 10
    assert fresh == saved


# ───────────────────── CLI ─────────────────────

def test_cli_audit(capsys):
    assert replay.main(["audit"]) == 0
    out = capsys.readouterr().out
    assert "审计对齐" in out and "1791338477997" in out


def test_cli_bad_args_exit_2():
    with pytest.raises(SystemExit) as exc:
        replay.main(["nonexistent"])
    assert exc.value.code == 2


def test_cli_replay(capsys):
    assert replay.main(
        ["replay", "--profile-id", "research_seed_02.e30.s1"]) == 0
    assert "research_seed_02.e30.s1" in capsys.readouterr().out


def test_cli_experiment_writes_output(tmp_path):
    out_file = tmp_path / "results.json"
    assert replay.main(["experiment", "--output", str(out_file)]) == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(data["results"]) == 10


def test_save_results_creates_dirs(tmp_path):
    p = tmp_path / "nested" / "r.json"
    save_results(p, {"x": Decimal("1.5")})
    assert json.loads(p.read_text(encoding="utf-8")) == {"x": "1.5"}
