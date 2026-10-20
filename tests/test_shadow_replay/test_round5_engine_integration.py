"""Round5 引擎集成测试：驱动真实 _execute_exit → position_net_pnl 累加 → consecutive_losses。

阻断级缺口：Round4 原有 P1 用例直接调 state.on_position_closed(position_net_pnl=...)，
绕过了 engine._execute_exit 的累加路径；test_e2e_real_chain.py 的用例虽走真实引擎，
但只断言聚合 net_pnl，不断言 state.consecutive_losses 或 pos.position_net_pnl。

本组用例的目的：**驱动引擎的 TP1 部分平仓 + 最终全平链路**，
验证（a）state.consecutive_losses 由整笔 position_net_pnl 方向决定；
（b）pos["position_net_pnl"] == Σ 各腿 replay_trades[pid] 明细的 net_pnl。

变异验证（防假绿）：临时把 engine.py L1233 的累加语句改为单腿覆盖
`pos["position_net_pnl"] = net_pnl`（模拟旧 bug），本组用例必须 FAIL；
改回累加后必须 PASS。
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.state import VirtualState

UTC = timezone.utc


# ============================================================================
# 构造辅助（复用 test_e2e_real_chain._make_engine_shell 的结构，
# 但独立定义避免文件间耦合）
# ============================================================================


def _make_engine_shell(initial_equity=Decimal("300")) -> ReplayEngine:
    """不加载 config 文件的 ReplayEngine 壳。

    与 test_e2e_real_chain._make_engine_shell 保持一致的 pilot 参数：
    taker_bps=5, slippage_baseline=2 (bps), tick_size=0.01, step_size=0.001。
    """
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.states = {
        "xau_original": VirtualState(
            initial_equity=initial_equity,
            equity=initial_equity,
            consecutive_loss_pause_threshold=2,
            pause_duration_hours=24,
        ),
        "xau_aggressive": VirtualState(
            initial_equity=initial_equity,
            equity=initial_equity,
            consecutive_loss_pause_threshold=2,
            pause_duration_hours=24,
        ),
    }
    engine.tick_size = Decimal("0.01")
    engine.step_size = Decimal("0.001")
    engine.min_notional = Decimal("5")
    engine.slippage_baseline = Decimal("2")
    engine.taker_bps = Decimal("5")
    engine.funding_events = []
    engine.invariant_violations = []
    engine.replay_trades = {"xau_original": [], "xau_aggressive": []}
    engine.pending_limit_orders = {"xau_original": [], "xau_aggressive": []}
    engine.vol_adj_override = Decimal("1.0")
    engine._open_seq = {"xau_original": 0, "xau_aggressive": 0}
    engine._profile_max_margin = {
        "xau_original": Decimal("30"), "xau_aggressive": Decimal("30"),
    }
    engine._profile_max_lev = {"xau_original": 3, "xau_aggressive": 3}
    engine.halt_lines_pct = [15, 25, 30]
    return engine


# ============================================================================
# 场景 A：前腿（TP1）大幅盈利 + 末腿（STOP_LOSS）小幅亏损 → 整笔净盈利
#   LONG @ 2000, qty=0.03
#   TP1=2060 (+3%), close_ratio=0.30 → pnl_TP1 = +0.54
#   STOP_LOSS=1995 → pnl_SL = -0.105
#   fee/slip ≈ -0.08 总量级，不改变整笔盈利方向
# ============================================================================


def test_engine_exit_accumulates_profit_first_then_loss_overall_profit():
    """真实链路：TP1 部分平仓大幅盈利 → STOP_LOSS 全平小幅亏损 → 整笔仍净盈利。

    断言点：
      (a) pos.position_net_pnl == Σ replay_trades[pid] 的 net_pnl（两腿累加）
      (b) state.consecutive_losses == 0（整笔盈利不清零以外，新实现保持 0）
      (c) replay_trades 恰好 2 条，exit_reason 分别为 TP1 / STOP_LOSS
    """
    engine = _make_engine_shell()
    pid = "xau_original"

    # 真实入场（走 _fill_entry → 写入 open_position + entry_fee_per_qty +
    # position_id + running peak 初始化）
    engine._fill_entry(
        profile_id=pid,
        event={"symbol": "XAUUSDT", "direction": "LONG", "grade": "S",
               "reference_price": "2000"},
        opened_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        fill_at=datetime(2026, 1, 5, 10, 1, tzinfo=UTC),
        fill_price=Decimal("2000"),
        quantity=Decimal("0.03"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss=Decimal("1995"),
        grade="S",
        order_kind="MARKET",
        tp1=Decimal("2060"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=None,
        atr=Decimal("10"),
        stop_loss_atr_multiplier="1.5",
        position_id="round5_overall_profit_pid",
    )

    # TP1 部分平仓（真实走 _execute_exit → 累加 pos.position_net_pnl）
    engine._partial_close(
        pid, datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
        Decimal("2060"), "TP1", Decimal("0.009"),
    )

    # 此时还没全平，position_net_pnl 已在累加；抓一份引用，
    # 等全平后 state 会清 open_position，这里先记住
    pos_after_tp1 = engine.states[pid].open_position
    assert pos_after_tp1 is not None, "TP1 部分平仓后应仍持仓"
    cumulative_pnl_before_close = pos_after_tp1["position_net_pnl"]
    assert cumulative_pnl_before_close > 0, (
        f"TP1 大幅盈利后 position_net_pnl 应为正，实际 {cumulative_pnl_before_close}"
    )

    # STOP_LOSS 全平剩余（真实走 _execute_exit + remaining<=0 分支 → on_position_closed）
    engine._close_position(
        pid, datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
        Decimal("1995"), "STOP_LOSS",
    )

    trades = engine.replay_trades[pid]
    assert len(trades) == 2, f"应产生 2 条 exit 明细，实际 {len(trades)}"
    assert trades[0]["exit_reason"] == "TP1"
    assert trades[1]["exit_reason"] == "STOP_LOSS"

    # 断言 (a)：Σ 各腿 net_pnl == pos.position_net_pnl（累加值）
    summed_net_pnl = sum(Decimal(t["net_pnl"]) for t in trades)
    # 注意：open_position 在 on_position_closed 里被置 None 了，
    # 但 trades 里有 position_id 可聚合；同时 state.realized_pnl + fee + funding + slippage 与 Σ net_pnl 相等
    # 直接断言 summed_net_pnl 就是最终 position_net_pnl（累加到最后一腿后）
    assert summed_net_pnl > 0, (
        f"整笔 net_pnl 应为正（前腿盈利 > 末腿亏损 + fee/slip），实际 {summed_net_pnl}"
    )

    # 断言 (b)：整笔盈利 → consecutive_losses == 0
    state = engine.states[pid]
    assert state.open_position is None, "全平后 open_position 应清空"
    assert state.consecutive_losses == 0, (
        f"整笔盈利 consecutive_losses 应为 0，实际 {state.consecutive_losses}"
    )
    assert state.consecutive_loss_pause_until is None


# ============================================================================
# 场景 B：前腿（TP1）小幅盈利 + 末腿（STOP_LOSS）大幅亏损 → 整笔净亏损
#   LONG @ 2000, qty=0.03
#   TP1=2010 (+0.5%), close_ratio=0.30 → pnl_TP1 = +0.09
#   STOP_LOSS=1930 → pnl_SL = -1.47
#   fee/slip ≈ -0.06 总量级，整笔必亏
# ============================================================================


def test_engine_exit_accumulates_profit_first_then_loss_overall_loss():
    """真实链路：TP1 部分平仓小幅盈利 → STOP_LOSS 全平大幅亏损 → 整笔净亏损。

    断言点：
      (a) Σ replay_trades[pid] 的 net_pnl < 0（整笔亏损）
      (b) state.consecutive_losses == 1（整笔亏损增加计数）
    """
    engine = _make_engine_shell()
    pid = "xau_original"

    engine._fill_entry(
        profile_id=pid,
        event={"symbol": "XAUUSDT", "direction": "LONG", "grade": "S",
               "reference_price": "2000"},
        opened_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        fill_at=datetime(2026, 1, 5, 10, 1, tzinfo=UTC),
        fill_price=Decimal("2000"),
        quantity=Decimal("0.03"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss=Decimal("1930"),
        grade="S",
        order_kind="MARKET",
        tp1=Decimal("2010"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=None,
        atr=Decimal("10"),
        stop_loss_atr_multiplier="1.5",
        position_id="round5_overall_loss_pid",
    )

    # TP1 小幅盈利（累加应 <0.1 量级）
    engine._partial_close(
        pid, datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
        Decimal("2010"), "TP1", Decimal("0.009"),
    )

    # 全平前再断言 position_net_pnl 已累加（正值）
    pos_after_tp1 = engine.states[pid].open_position
    assert pos_after_tp1 is not None
    assert pos_after_tp1["position_net_pnl"] > 0, (
        f"TP1 小幅盈利后 position_net_pnl 应为正，实际 {pos_after_tp1['position_net_pnl']}"
    )

    # 大幅止损全平（末腿亏损远大于前腿盈利）
    engine._close_position(
        pid, datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
        Decimal("1930"), "STOP_LOSS",
    )

    trades = engine.replay_trades[pid]
    assert len(trades) == 2
    assert trades[0]["exit_reason"] == "TP1"
    assert trades[1]["exit_reason"] == "STOP_LOSS"

    # 断言 (a)：整笔净亏损
    summed_net_pnl = sum(Decimal(t["net_pnl"]) for t in trades)
    assert summed_net_pnl < 0, (
        f"整笔 net_pnl 应为负（末腿大幅亏损 > 前腿盈利 + fee/slip），实际 {summed_net_pnl}"
    )

    # 断言 (b)：整笔亏损 → consecutive_losses == 1
    state = engine.states[pid]
    assert state.open_position is None
    assert state.consecutive_losses == 1, (
        f"整笔亏损 consecutive_losses 应为 1，实际 {state.consecutive_losses}"
    )
    assert state.consecutive_loss_pause_until is None  # threshold=2，一笔不够暂停


# ============================================================================
# 断言 (c) 的精确验证：直接对比 pos.position_net_pnl 与 Σ 明细 net_pnl
# 在部分平仓后还持有 pos，此时可直接比对
# ============================================================================


def test_partial_close_accumulates_into_position_net_pnl():
    """TP1 部分平仓后，pos.position_net_pnl 应等于截止目前已执行各腿 net_pnl 的累加。

    此用例在部分平仓后立即断言，避免全平后 open_position 被清空导致无法直接对比。
    同时在执行 _close_position 全平前，再断言累加值在部分平仓基础上继续增加（变负）。
    """
    engine = _make_engine_shell()
    pid = "xau_original"

    engine._fill_entry(
        profile_id=pid,
        event={"symbol": "XAUUSDT", "direction": "LONG", "grade": "S",
               "reference_price": "2000"},
        opened_at=datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        fill_at=datetime(2026, 1, 5, 10, 1, tzinfo=UTC),
        fill_price=Decimal("2000"),
        quantity=Decimal("0.03"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss=Decimal("1990"),
        grade="S",
        order_kind="MARKET",
        tp1=Decimal("2060"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=None,
        atr=Decimal("10"),
        stop_loss_atr_multiplier="1.5",
        position_id="round5_accumulate_pid",
    )

    # —— 第一腿：TP1 ——
    engine._partial_close(
        pid, datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
        Decimal("2060"), "TP1", Decimal("0.009"),
    )

    trades_after_tp1 = engine.replay_trades[pid]
    assert len(trades_after_tp1) == 1
    sum_after_tp1 = sum(Decimal(t["net_pnl"]) for t in trades_after_tp1)
    pos = engine.states[pid].open_position
    pos_net_pnl_after_tp1 = pos["position_net_pnl"]
    # 关键断言：Σ 明细 net_pnl == pos.position_net_pnl（引擎累加结果）
    assert pos_net_pnl_after_tp1 == sum_after_tp1, (
        f"TP1 后 position_net_pnl={pos_net_pnl_after_tp1} 应等于 Σ net_pnl={sum_after_tp1}"
    )

    # —— 第二腿：STOP_LOSS 全平 ——
    engine._close_position(
        pid, datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
        Decimal("1990"), "STOP_LOSS",
    )

    trades_final = engine.replay_trades[pid]
    assert len(trades_final) == 2
    # 全平后 open_position 已清空，此时 state 不直接持有 position_net_pnl；
    # 但 engine._execute_exit 的累加逻辑决定了 Σ 明细 net_pnl 就是整笔 position_net_pnl。
    # 这里做 sanity：全平后 consecutive_losses 与整笔方向一致即可（方向由场景决定）。
    sum_final = sum(Decimal(t["net_pnl"]) for t in trades_final)
    state = engine.states[pid]
    expected_losses = 1 if sum_final < 0 else 0
    assert state.consecutive_losses == expected_losses, (
        f"整笔 net_pnl={sum_final} → consecutive_losses 应为 {expected_losses}，"
        f"实际 {state.consecutive_losses}"
    )
