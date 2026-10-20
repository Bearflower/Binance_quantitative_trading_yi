"""批次三新增测试：入场不变量回归、聚合数量正确性、滑点压力三档、decision_summary。

批次三完成任务 A-D 后，补齐 4 类关键回归用例：
  1. 入场后 invariant_violations == []（任务 B 修复的入场不变量误报）
  2. 聚合 trade quantity = 入场总数量（任务 A 修复的 engine 明细缺 entry_quantity）
  3. slippage_pressure 三档：2bps == final_equity，4bps ≤ 2bps（任务 D）
  4. decision_summary / rejection_reason_distribution 数值正确（任务 C/D）
"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.state import VirtualState
from backtest.btc_eth.replay.report import compute_metrics, _aggregate_positions

UTC = timezone.utc


# =====================================================================
# Fixtures（风格对齐 test_engine_exit_rules.py / test_report.py）
# =====================================================================

def _make_engine_shell():
    """不加载 config 文件，直接构造 ReplayEngine 壳。"""
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.states = {
        "xau_original": VirtualState(initial_equity=Decimal("300"), equity=Decimal("300")),
        "xau_aggressive": VirtualState(initial_equity=Decimal("300"), equity=Decimal("300")),
    }
    engine.tick_size = Decimal("0.01")
    engine.step_size = Decimal("0.001")
    engine.min_notional = Decimal("5")
    engine.slippage_baseline = Decimal("2")  # 2 bps
    engine.taker_bps = Decimal("5")
    engine.funding_events = []
    engine.invariant_violations = []
    engine.replay_trades = {"xau_original": [], "xau_aggressive": []}
    engine.pending_limit_orders = {"xau_original": [], "xau_aggressive": []}
    engine.vol_adj_override = Decimal("1.0")
    # 批次三 C：_open_seq（_build_decision_summary 不需要，但是其他路径可能访问）
    engine._open_seq = {"xau_original": 0, "xau_aggressive": 0}
    return engine


def _make_engine_meta_with_trailing_and_decision(
    initial_equity="300",
    decision_summary=None,
):
    """批次三 D 新 engine_meta fixture：含 trailing、snapshot_gaps、decision_summary。"""
    if decision_summary is None:
        decision_summary = {"per_profile": {}, "same_timestamp_decision_diff": {}}
    return {
        "replay_run_id": "abc123def456",
        "engine_version": "replay-v1.0.0",
        "manifest_sha256": "deadbeef",
        "input_cutoff_at": "2026-10-10T00:00:00+00:00",
        "initial_equity": initial_equity,
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
        "snapshot_gaps": 0,
        "trailing": {
            "vol_adj_fallback_used": True,
            "vol_adj_value": "1.0",
            "trailing_flat_close": True,
        },
        "decision_summary": decision_summary,
    }


# =====================================================================
# 任务 B 回归：入场后 invariant_violations == []
# =====================================================================

def test_entry_no_invariant_violations_after_successful_market_fill():
    """批次三 B 回归：一次真实 MARKET 入场后，engine.invariant_violations 必须是空列表。

    批次二遗留 bug：_fill_entry 调 run_all_invariants，后者在 pos_after 非 None 时
    会调 check_no_double_close 返回「平仓后持仓仍存在」伪违规——每次入场都记一条，
    让准入门槛 ⑤ 永远失败。

    本用例构造最小 MARKET 入场流程，直接 assert invariant_violations == []。
    """
    engine = _make_engine_shell()
    pid = "xau_original"
    state = engine.states[pid]

    # 真实的入场构造（MARKET 路径会调 _fill_entry → state.on_entry_approved）
    fill_at = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
    event = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "grade": "A",
        "reference_price": "2000",
    }
    engine._fill_entry(
        profile_id=pid,
        event=event,
        opened_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        fill_at=fill_at,
        fill_price=Decimal("2000"),
        quantity=Decimal("0.03"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss=Decimal("1980"),
        grade="A",
        order_kind="MARKET",
        tp1=Decimal("2020"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2040"),
        tp2_close_ratio=Decimal("0.40"),
        atr=Decimal("10"),
        stop_loss_atr_multiplier="2.0",
        position_id="test_entry_no_violations",
    )

    # 关键断言：入场后 invariant_violations 必须是空列表
    assert engine.invariant_violations == [], (
        f"入场后 invariant_violations 应为空，实际: {engine.invariant_violations}"
    )
    # 同时 state.open_position 应该被正确写入
    assert state.open_position is not None
    assert state.open_position["quantity"] == Decimal("0.03")
    # 批次三 A：entry_quantity 必须等于入场总数量（不随部分平仓变化）
    assert state.open_position["entry_quantity"] == Decimal("0.03")


# =====================================================================
# 任务 A 验证：分批平仓聚合后 quantity == entry_quantity（入场总数量）
# =====================================================================

def test_aggregated_trade_quantity_equals_entry_total_after_partial_and_final_close():
    """批次三 A 验证：同一 position_id 分批 TP1 + 最终止损平仓，
    _aggregate_positions 聚合后的 quantity 必须 == entry_quantity（入场总数量 0.03），
    而不是 TP1 批次的 close_qty（0.009）或最后止损批次的 close_qty（0.021）。
    同时 net_pnl == 两批次 net_pnl 之和。
    """
    entry_total_qty = "0.03"        # 入场总数量
    tp1_close_qty = "0.009"          # TP1 批次平仓量（= 0.03 × 0.30）
    final_close_qty = "0.021"        # 最终止损批次平仓量（= 0.03 - 0.009）
    pid = "partials_pid_1"

    # 两条 exit 明细：同一 position_id，TP1 + 最终 STOP_LOSS
    trades = [
        {
            "position_id": pid,
            "entry_time": "2026-10-01T10:00:00+00:00",
            "entry_price": "2000",
            "exit_time": "2026-10-01T10:30:00+00:00",
            "exit_price": "2020",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": "TP1",
            "quantity": tp1_close_qty,             # 批次平仓量（旧 bug 把这个当入场总数量）
            "entry_quantity": entry_total_qty,     # 批次三 A：入场总数量
            "leverage": "3",
            "margin": "20",
            "realized_pnl": "+0.18",
            "fee": "-0.00054",
            "funding_fee": "0",
            "slippage": "-0.00036",
            "net_pnl": "+0.1791",
            "holding_seconds": "1800",
        },
        {
            "position_id": pid,
            "entry_time": "2026-10-01T10:00:00+00:00",
            "entry_price": "2000",
            "exit_time": "2026-10-01T11:00:00+00:00",
            "exit_price": "1980",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": "STOP_LOSS",
            "quantity": final_close_qty,             # 批次平仓量
            "entry_quantity": entry_total_qty,       # 入场总数量
            "leverage": "3",
            "margin": "20",
            "realized_pnl": "-0.42",
            "fee": "-0.00126",
            "funding_fee": "0",
            "slippage": "-0.00084",
            "net_pnl": "-0.4221",
            "holding_seconds": "3600",
        },
    ]

    aggregated = _aggregate_positions(trades)

    # 核心断言：1 个仓位 → 1 笔聚合 trade
    assert len(aggregated) == 1, f"应有 1 笔聚合 trade，实际 {len(aggregated)}"

    agg = aggregated[0]

    # 批次三 A 关键断言：聚合 quantity == entry_quantity（入场总数量 0.03）
    # 不是 TP1 的 0.009，也不是最终的 0.021
    assert agg["quantity"] == entry_total_qty, (
        f"聚合 quantity 应为入场总数量 {entry_total_qty}，实际 {agg['quantity']}；"
        f"批次二 bug 会把它当成 TP1 平仓量 {tp1_close_qty}"
    )

    # net_pnl == 各批次 net_pnl 之和
    expected_net_pnl = Decimal("0.1791") + Decimal("-0.4221")
    assert Decimal(agg["net_pnl"]) == expected_net_pnl, (
        f"聚合 net_pnl 应为 {expected_net_pnl}，实际 {agg['net_pnl']}"
    )

    # exit_path（批次三 A：exits 明细保留各批次平仓量）
    assert len(agg["exits"]) == 2
    assert agg["exits"][0]["exit_reason"] == "TP1"
    assert agg["exits"][0]["quantity"] == tp1_close_qty
    assert agg["exits"][1]["exit_reason"] == "STOP_LOSS"
    assert agg["exits"][1]["quantity"] == final_close_qty


# =====================================================================
# 任务 D：slippage_pressure 三档验证
# =====================================================================

def test_slippage_pressure_three_tiers_and_baseline_equality():
    """批次三 D 验证：三档滑点压力存在，2bps 档 == final_equity（因为 baseline=2bps），
    4bps 档 ≤ 2bps 档（滑点越高，权益越低）。"""
    trades = [
        {
            "position_id": "slippage_pid",
            "entry_time": "2026-10-01T10:00:00+00:00",
            "entry_price": "2000",
            "exit_time": "2026-10-01T14:00:00+00:00",
            "exit_price": "2100",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": "TP2",
            "quantity": "0.05",
            "entry_quantity": "0.05",
            "leverage": "3",
            "margin": "33.33",
            "realized_pnl": "+5",
            "fee": "-0.025",
            "funding_fee": "0",
            "slippage": "-0.02",
            "net_pnl": "+4.955",
            "holding_seconds": "14400",
        },
    ]
    state = {
        "final_equity": "304.955",
        "realized_pnl": "5",
        "fee_paid": "0.025",
        "funding_fee": "0",
        "slippage_cost": "0.02",
    }
    eq_hist = [
        ("2026-10-01T10:00:00+00:00", "300"),
        ("2026-10-01T14:00:00+00:00", "304.955"),
    ]
    engine_meta = _make_engine_meta_with_trailing_and_decision()

    # replay_cfg: baseline=2bps, tiers=[1,2,4]
    replay_cfg = {
        "slippage": {
            "baseline_bps": 2,
            "tiers_bps": [1, 2, 4],
        },
        "tradfi_active_hours_utc": [7, 20],
    }

    r = {
        "trades": trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": eq_hist,
    }
    m = compute_metrics(r, engine_meta, replay_cfg=replay_cfg)

    slip_pressure = m["slippage_pressure"]

    # 断言 1：三档存在
    assert "1bps" in slip_pressure, "1bps 档缺失"
    assert "2bps" in slip_pressure, "2bps 档缺失"
    assert "4bps" in slip_pressure, "4bps 档缺失"
    assert slip_pressure["baseline_bps"] == 2
    assert slip_pressure["tiers_bps"] == [1, 2, 4]

    # 断言 2：2bps 档 == final_equity（因为 baseline=2bps，此档不加不减）
    assert Decimal(slip_pressure["2bps"]) == Decimal(m["final_equity"]), (
        f"2bps 档应等于 final_equity={m['final_equity']}，实际 {slip_pressure['2bps']}"
    )

    # 断言 3：档位越高，权益越低或相等
    assert Decimal(slip_pressure["4bps"]) <= Decimal(slip_pressure["2bps"]), (
        f"4bps 档 {slip_pressure['4bps']} 应 ≤ 2bps 档 {slip_pressure['2bps']}"
    )
    assert Decimal(slip_pressure["2bps"]) <= Decimal(slip_pressure["1bps"]), (
        f"2bps 档应 ≤ 1bps 档（滑点越高权益越低）"
    )


# =====================================================================
# 任务 C/D：decision_summary / rejection_reason_distribution 数值验证
# =====================================================================

def test_build_decision_summary_and_metrics_consumption():
    """批次三 C/D 验证：构造假事件集 → _build_decision_summary 输出正确，
    再把 per_profile 部分传给 compute_metrics → decision_stats/rejection_reason_distribution 正确。"""
    engine = _make_engine_shell()

    # 假事件集：按 scheduled_at 稳定排序
    events = [
        {"profile_id": "xau_original", "scheduled_at": "2026-10-01T10:00:00+00:00",
         "decision": "candidate", "direction": "LONG", "grade": "A"},
        {"profile_id": "xau_original", "scheduled_at": "2026-10-01T10:05:00+00:00",
         "decision": "no_signal", "direction": "LONG", "grade": "A",
         "reason_codes": ["atr_too_low", "volatility_not_met"]},
        {"profile_id": "xau_original", "scheduled_at": "2026-10-01T10:10:00+00:00",
         "decision": "no_signal", "reason_codes": ["atr_too_low"]},
        {"profile_id": "xau_original", "scheduled_at": "2026-10-01T10:15:00+00:00",
         "decision": "analysis_error"},
        {"profile_id": "xau_aggressive", "scheduled_at": "2026-10-01T10:00:00+00:00",
         "decision": "candidate", "direction": "SHORT", "grade": "S"},
        {"profile_id": "xau_aggressive", "scheduled_at": "2026-10-01T10:05:00+00:00",
         "decision": "no_signal", "direction": "LONG", "grade": "A",
         "reason_codes": ["volatility_not_met"]},
        {"profile_id": "xau_aggressive", "scheduled_at": "2026-10-01T10:10:00+00:00",
         "decision": "candidate", "direction": "LONG", "grade": "S"},
        {"profile_id": "xau_aggressive", "scheduled_at": "2026-10-01T10:15:00+00:00",
         "decision": "no_signal", "reason_codes": []},
    ]
    engine.events = events
    ds = engine._build_decision_summary()

    # —— per_profile 验证 ——
    orig = ds["per_profile"]["xau_original"]
    assert orig["scheduled_runs"] == 4
    assert orig["candidate"] == 1
    assert orig["no_signal"] == 2
    assert orig["analysis_error"] == 1
    # 成功率 = (4 - 1) / 4 = 0.75
    assert orig["analysis_success_rate"] == 0.75
    # no_signal reasons
    assert orig["no_signal_reasons"]["atr_too_low"] == 2
    assert orig["no_signal_reasons"]["volatility_not_met"] == 1

    aggr = ds["per_profile"]["xau_aggressive"]
    assert aggr["scheduled_runs"] == 4
    assert aggr["candidate"] == 2
    assert aggr["no_signal"] == 2
    assert aggr["analysis_error"] == 0
    assert aggr["analysis_success_rate"] == 1.0

    # —— same_timestamp_decision_diff ——
    diff = ds["same_timestamp_decision_diff"]
    assert diff["common_scheduled_count"] == 4  # 两 profile 都有 4 个时间点
    # 逐一对比 (decision, direction, grade) 三元组：
    # 10:00 orig=(candidate,LONG,A) vs aggr=(candidate,SHORT,S) → 不同（direction+grade）
    # 10:05 orig=(no_signal,LONG,A) vs aggr=(no_signal,LONG,A) → 相同
    # 10:10 orig=(no_signal,None,None) vs aggr=(candidate,LONG,S) → 不同
    # 10:15 orig=(analysis_error,None,None) vs aggr=(no_signal,None,None) → 不同
    assert diff["differing_count"] == 3
    assert diff["differing_ratio"] == pytest.approx(0.75, rel=1e-6)
    # 样本至少有 3 条
    assert len(diff["samples"]) == 3

    # —— 无事件边界：total=0 → success_rate=None（不再用 1.0 冒充）——
    engine.events = []
    ds_empty = engine._build_decision_summary()
    empty_orig = ds_empty["per_profile"]["xau_original"]
    assert empty_orig["scheduled_runs"] == 0
    assert empty_orig["analysis_success_rate"] is None
    assert empty_orig["analysis_success_rate_computable"] is False
    assert empty_orig["analysis_sample_note"] == "no_scheduled_events"

    # —— rejection_reason_distribution（unfilled 明细解析）——
    unfilled_trades = [
        # 真实成交的不要计入
        {"position_id": "real_pid", "filled": True, "exit_reason": "TP1",
         "entry_price": "2000", "quantity": "0.05", "net_pnl": "+1",
         "exit_time": "2026-10-01T11:00:00+00:00", "entry_time": "2026-10-01T10:00:00+00:00",
         "direction": "LONG", "symbol": "XAUUSDT", "leverage": "3", "margin": "20",
         "unfilled_reason": None},
        # 门槛拒绝（解析 rejected:['position_limit','insufficient_margin']）
        {"position_id": None, "filled": False, "exit_reason": None,
         "unfilled_reason": "rejected:['position_limit','insufficient_margin']"},
        {"position_id": None, "filled": False, "exit_reason": None,
         "unfilled_reason": "rejected:['daily_trade_limit']"},
        {"position_id": None, "filled": False, "exit_reason": None,
         "unfilled_reason": "expired_unfilled"},
        {"position_id": None, "filled": False, "exit_reason": None,
         "unfilled_reason": "rejected:['position_limit']"},
    ]
    state = {
        "final_equity": "300",
        "realized_pnl": "0",
        "fee_paid": "0",
        "funding_fee": "0",
        "slippage_cost": "0",
    }
    engine_meta = _make_engine_meta_with_trailing_and_decision(decision_summary=ds)
    r = {
        "trades": unfilled_trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": [("2026-10-01T10:00:00+00:00", "300")],
    }
    m = compute_metrics(
        r, engine_meta,
        replay_cfg={"slippage": {"baseline_bps": 2, "tiers_bps": [1, 2, 4]}},
        decision_stats=orig,  # 直接传 orig 部分
    )

    # rejection_reason_distribution
    rej = m["rejection_reason_distribution"]
    assert rej["position_limit"] == 2        # 出现两次
    assert rej["insufficient_margin"] == 1
    assert rej["daily_trade_limit"] == 1
    assert rej["expired_unfilled"] == 1

    # decision_stats 从参数透传
    ds_metrics = m["decision_stats"]
    assert ds_metrics["scheduled_runs"] == 4
    assert ds_metrics["candidate"] == 1
    assert ds_metrics["analysis_success_rate"] == 0.75


# =====================================================================
# 任务 1 补充：MAE/MFE 持仓级聚合断言（此前 report.py 不含 mae/mfe 键致静默归零）
# =====================================================================

def test_mae_mfe_nonzero_after_aggregation():
    """一次「先浮盈后浮亏再平仓」的 LONG 持仓，聚合后 MAE/MFE 应非零且 avg_* > 0。"""
    # 模拟 engine 输出的明细 trade：单批次 LONG 持仓
    # 持仓生命周期：entry=2000, highest=2010, lowest=1990, exit=1990(STOP_LOSS)
    # 持仓级 MFE = 2010-2000 = 10，MAE = 2000-1990 = 10
    trades = [{
        "position_id": "pos_001",
        "entry_time": "2026-10-01T10:00:00+00:00",
        "entry_price": "2000.00",
        "exit_time": "2026-10-01T10:05:00+00:00",
        "exit_price": "1990.00",
        "direction": "LONG",
        "symbol": "XAUUSDT",
        "filled": True,
        "unfilled_reason": None,
        "exit_reason": "STOP_LOSS",
        "quantity": "0.100",
        "entry_quantity": "0.100",
        "leverage": "3",
        "margin": "66.67",
        "grade": "A",
        "realized_pnl": "-1.00",
        "fee": "-0.05",
        "funding_fee": "0",
        "slippage": "-0.02",
        "net_pnl": "-1.07",
        # engine 持仓级 MAE/MFE（running peak 派生）
        "mae": "10.00",    # entry - lowest = 2000 - 1990
        "mfe": "10.00",    # highest - entry = 2010 - 2000
        "holding_seconds": 300,
    }]

    # _aggregate_positions 后应含 mae/mfe
    agg = _aggregate_positions(trades)
    assert len(agg) == 1
    assert agg[0]["mae"] is not None, "聚合 trade 应含 mae 键且非 None"
    assert agg[0]["mfe"] is not None, "聚合 trade 应含 mfe 键且非 None"
    assert Decimal(agg[0]["mae"]) == Decimal("10.00")
    assert Decimal(agg[0]["mfe"]) == Decimal("10.00")

    # compute_metrics 后 avg_mae / avg_mfe 应 > 0
    engine_meta = _make_engine_meta_with_trailing_and_decision()
    state = {
        "realized_pnl": "-1.00",
        "fee_paid": "0.05",
        "funding_fee": "0",
        "slippage_cost": "0.02",
        "final_equity": "298.93",
    }
    profile_result = {
        "trades": trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": [],
    }
    m = compute_metrics(profile_result, engine_meta, replay_cfg={})
    assert m["avg_mae"] != "0" and Decimal(m["avg_mae"]) > Decimal("0"), \
        f"avg_mae 应 > 0，实际 {m['avg_mae']}"
    assert m["avg_mfe"] != "0" and Decimal(m["avg_mfe"]) > Decimal("0"), \
        f"avg_mfe 应 > 0，实际 {m['avg_mfe']}"
    # 单 trade 平均 = 自身值
    assert Decimal(m["avg_mae"]) == Decimal("10.00")
    assert Decimal(m["avg_mfe"]) == Decimal("10.00")


def test_mae_mfe_partial_close_multiple_batches_take_max():
    """多批次平仓（TP1 + STOP_LOSS）聚合后 MAE/MFE 取各批次最大值。"""
    # 批次 1：TP1 部分平仓（持仓期 high 已到 2010，low 1995）
    # 批次 2：STOP_LOSS 全平（持仓期 low 继续下探到 1990）
    trades = [
        {
            "position_id": "pos_002",
            "entry_time": "2026-10-01T10:00:00+00:00",
            "entry_price": "2000.00",
            "exit_time": "2026-10-01T10:03:00+00:00",
            "exit_price": "2010.00",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": "TP1",
            "quantity": "0.050",
            "entry_quantity": "0.100",
            "leverage": "3",
            "margin": "66.67",
            "grade": "B",
            "realized_pnl": "0.50",
            "fee": "-0.02",
            "funding_fee": "0",
            "slippage": "-0.01",
            "net_pnl": "0.47",
            # 批次 1 平仓时 running peak：highest=2010, lowest=1995
            "mae": "5.00",     # entry - lowest = 2000 - 1995
            "mfe": "10.00",    # highest - entry = 2010 - 2000
            "holding_seconds": 180,
        },
        {
            "position_id": "pos_002",  # 同一 position_id → 聚合
            "entry_time": "2026-10-01T10:00:00+00:00",
            "entry_price": "2000.00",
            "exit_time": "2026-10-01T10:05:00+00:00",
            "exit_price": "1990.00",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "filled": True,
            "unfilled_reason": None,
            "exit_reason": "STOP_LOSS",
            "quantity": "0.050",
            "entry_quantity": "0.100",
            "leverage": "3",
            "margin": "66.67",
            "grade": "B",
            "realized_pnl": "-0.50",
            "fee": "-0.02",
            "funding_fee": "0",
            "slippage": "-0.01",
            "net_pnl": "-0.53",
            # 批次 2 平仓时 running peak：highest=2010, lowest=1990
            "mae": "10.00",    # entry - lowest = 2000 - 1990
            "mfe": "10.00",    # highest - entry = 2010 - 2000
            "holding_seconds": 300,
        },
    ]

    agg = _aggregate_positions(trades)
    assert len(agg) == 1
    # MAE/MFE 取各批次最大值
    assert Decimal(agg[0]["mae"]) == Decimal("10.00"), f"聚合 mae 应为 10（批次 2），实际 {agg[0]['mae']}"
    assert Decimal(agg[0]["mfe"]) == Decimal("10.00"), f"聚合 mfe 应为 10（两批次都是），实际 {agg[0]['mfe']}"


def test_mae_mfe_none_safe_aggregation():
    """全部 mae/mfe 为 None 时，聚合结果应为 None 而非崩溃或静默 0。"""
    trades = [{
        "position_id": "pos_003",
        "entry_time": "2026-10-01T10:00:00+00:00",
        "entry_price": "2000.00",
        "exit_time": "2026-10-01T10:05:00+00:00",
        "exit_price": "2010.00",
        "direction": "LONG",
        "symbol": "XAUUSDT",
        "filled": True,
        "exit_reason": "TP2",
        "quantity": "0.100",
        "entry_quantity": "0.100",
        "realized_pnl": "1.00",
        "fee": "-0.05",
        "funding_fee": "0",
        "slippage": "-0.02",
        "net_pnl": "0.93",
        # 极老测试路径 mae/mfe 均为 None
        "mae": None,
        "mfe": None,
        "holding_seconds": 300,
    }]

    agg = _aggregate_positions(trades)
    assert len(agg) == 1
    assert agg[0]["mae"] is None, "全部 None 时聚合 mae 应为 None"
    assert agg[0]["mfe"] is None, "全部 None 时聚合 mfe 应为 None"

    # compute_metrics 在 closed_trades 无有效 mae/mfe 时应回退为 Decimal("0")，不崩溃
    engine_meta = _make_engine_meta_with_trailing_and_decision()
    state = {
        "realized_pnl": "1.00",
        "fee_paid": "0.05",
        "funding_fee": "0",
        "slippage_cost": "0.02",
        "final_equity": "300.93",
    }
    profile_result = {
        "trades": trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": [],
    }
    m = compute_metrics(profile_result, engine_meta, replay_cfg={})
    assert m["avg_mae"] == "0"
    assert m["avg_mfe"] == "0"
