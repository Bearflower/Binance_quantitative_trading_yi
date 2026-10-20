"""回放引擎退出规则专项回归测试（缺陷 1 修复）。

缺陷 1 根因：_fill_entry 未把 tp1/tp2/trailing/time_stop 写入 open_position，
导致 _check_and_close_on_bar 四条退出规则永远不触发。
修复后：MARKET/LIMIT 两条路径都传全 6 字段，state.on_entry_approved 也收齐。

本测试不依赖完整 ReplayEngine 构造（其 __init__ 需要加载 config/snapshot 文件），
而是直接实例化一个 ReplayEngine 壳对象并手工注入 self.states，
再调用 _check_and_close_on_bar / _partial_close / _close_position。
"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.state import VirtualState

UTC = timezone.utc


def _make_engine_shell():
    """不加载任何 config 文件，直接构造 ReplayEngine 壳。

    ReplayEngine 实例上 _check_and_close_on_bar 只依赖：
      - self.states[profile_id]（VirtualState 实例，含 open_position）
      - self.tick_size / self.step_size / self.min_notional（舍入）
      - self.slippage_baseline / self.taker_bps（费用/滑点）
      - self.funding_events（空列表即可）
      - self.invariant_violations（空列表）
      - self.replay_trades[profile_id]（空列表）
      - self.vol_adj_override（trailing 波动率调节因子，阻断 #3 新增）
    """
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.states = {
        "xau_original": VirtualState(
            initial_equity=Decimal("300"),
            equity=Decimal("300"),
        ),
        "xau_aggressive": VirtualState(
            initial_equity=Decimal("300"),
            equity=Decimal("300"),
        ),
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
    # 阻断 #3：trailing 波动率调节因子（从 config 读，测试壳固定 1.0）
    engine.vol_adj_override = Decimal("1.0")
    return engine


def _long_pos(**overrides):
    """LONG 入场持仓基线，entry_price=2000。

    阻断 #3：默认补齐 trailing 运行态字段——highest_price 初始 = entry_price，
    trailing_activated/tp1_hit 默认 False，atr/_stop_loss_atr_multiplier 默认 None。
    这些字段由引擎 state.on_entry_approved 在真实入场时自动初始化，
    测试直接构造 pos 时需手动补齐。
    """
    base = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "entry_price": Decimal("2000"),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "opened_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        "stop_loss": Decimal("1980"),
        "tp1": Decimal("2020"),
        "tp1_close_ratio": Decimal("0.30"),
        "tp2": Decimal("2040"),
        "tp2_close_ratio": Decimal("0.40"),
        "trailing": None,
        "time_stop": None,
        "grade": "A",
        # 阻断 #3：trailing 运行态字段（引擎 state.on_entry_approved 自动初始化）
        "highest_price": Decimal("2000"),
        "lowest_price": Decimal("2000"),
        "trailing_activated": False,
        "trailing_stop_price": None,
        "pending_profit_pct": None,
        "tp1_hit": False,
        "atr": None,
        "_stop_loss_atr_multiplier": None,
    }
    base.update(overrides)
    return base


def _short_pos(**overrides):
    """SHORT 入场持仓基线。同 _long_pos，阻断 #3 补齐 trailing 运行态字段。"""
    base = {
        "symbol": "XAUUSDT",
        "direction": "SHORT",
        "entry_price": Decimal("2000"),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "opened_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        "stop_loss": Decimal("2020"),
        "tp1": Decimal("1980"),
        "tp1_close_ratio": Decimal("0.30"),
        "tp2": Decimal("1960"),
        "tp2_close_ratio": Decimal("0.40"),
        "trailing": None,
        "time_stop": None,
        "grade": "A",
        # 阻断 #3：trailing 运行态字段
        "highest_price": Decimal("2000"),
        "lowest_price": Decimal("2000"),
        "trailing_activated": False,
        "trailing_stop_price": None,
        "pending_profit_pct": None,
        "tp1_hit": False,
        "atr": None,
        "_stop_loss_atr_multiplier": None,
    }
    base.update(overrides)
    return base


def _kline(high, low, open_p, close_p):
    return {
        "open_time": datetime(2026, 10, 1, 10, 1, tzinfo=UTC).isoformat(),
        "high": str(high),
        "low": str(low),
        "open": str(open_p),
        "close": str(close_p),
        "volume": "1000",
    }


# ---- TP1 触发 ----
def test_tp1_triggers_when_high_crosses_for_long():
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos()
    bar = _kline(2025, 1999, 2000, 2022)  # high 穿越 tp1=2020
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "TP1"
    # tp1 触发后 tp1 置 None，避免重复
    assert engine.states["xau_original"].open_position["tp1"] is None


def test_tp1_triggers_when_low_crosses_for_short():
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _short_pos()
    bar = _kline(2001, 1975, 2000, 1980)  # low 穿越 tp1=1980
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "TP1"
    assert engine.states["xau_original"].open_position["tp1"] is None


def test_tp2_triggers_when_high_crosses_for_long():
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos()
    # 先让 tp1 置 None（模拟 TP1 已触发），数量仍 > 0
    engine.states["xau_original"].open_position["tp1"] = None
    bar = _kline(2050, 2035, 2030, 2045)  # high 穿越 tp2=2040
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "TP2"


# ---- 反向断言：tp=None 时不会误触发 ----
def test_no_trigger_when_tp_is_none():
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos(tp1=None, tp2=None)
    bar = _kline(2100, 1900, 2000, 2050)  # price 大幅穿越 tp1/tp2 位置
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    # 只有 stop_loss 也没触发（high 2100, low 1900 → low=1900 <= sl=1980）
    # → 返回 "STOP_LOSS"；但若 tp1=None 不该误返回 TP1
    assert result != "TP1" and result != "TP2"


# ---- Trailing stop 触发 ----
def test_trailing_stop_triggers_after_activation():
    """阻断 #3：嵌套 trailing 配置 + running peak 初始化。

    测试意图不变：浮盈 >= activation 后启用 trailing，回撤触发平仓。
    适配新契约：trailing_cfg 必须嵌套结构，pos 必须预设 highest_price
    （running peak 由引擎 _run_position_exit_up_to 每根 K 线更新，
    测试直接调 _check_and_close_on_bar 时需手动模拟更新后的状态）。

    场景：entry=2000, highest_price(peaked)=2030 → pct=1.5% >= min_profit=1% 激活
    单档 tier profit_ceiling=999, retrace_ratio=0.5 → stop=2030 - 30*0.5=2015
    k_low=2000 <= 2015 → 触发 TRAILING_STOP
    """
    engine = _make_engine_shell()
    # 嵌套结构（冻结 profile 的实际配置形态）
    trailing_cfg = {
        "enabled": True,
        "activation": {"min_profit_pct": Decimal("1"), "also_on_tp1": False},
        "regression_tiers": [{"profit_ceiling": Decimal("999"), "retrace_ratio": Decimal("0.5")}],
    }
    # tp1/tp2 拉远，确保 high=2030 不触发 TP1/TP2；stop_loss 也拉远确保不触发
    engine.states["xau_original"].open_position = _long_pos(
        tp1=Decimal("2050"),
        tp2=Decimal("2100"),
        trailing=trailing_cfg,
        stop_loss=Decimal("1950"),
        # 阻断 #3：模拟 _run_position_exit_up_to 已把 highest_price 更新到 2030
        highest_price=Decimal("2030"),
    )
    # high=2030（peak）→ pct=1.5% 激活；low=2000 → final_stop=2015 → k_low=2000<=2015 触发
    bar = _kline(2030, 2000, 2020, 2005)
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "TRAILING_STOP"


# ---- Time stop 触发 ----
def test_time_stop_triggers_after_max_holding_hours():
    engine = _make_engine_shell()
    ts_cfg = {"max_holding_hours": 2, "close_ratio": Decimal("1")}
    engine.states["xau_original"].open_position = _long_pos(time_stop=ts_cfg)
    # 入场 opened_at = 10:00，当前时间 12:01（超 2h）
    bar = _kline(2010, 1995, 2000, 2005)
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 12, 1, tzinfo=UTC),
    )
    assert result == "TIME_STOP"


# ---- Stop loss（原有路径，回归）----
def test_stop_loss_triggers_for_long_when_low_below():
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos(stop_loss=Decimal("1980"))
    bar = _kline(2005, 1975, 2000, 1990)  # low 1975 < 1980
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "STOP_LOSS"


# ---- 更关键：验证 open_position 带上 tp/trailing/time_stop 后四条规则才能触发 ----
# （缺陷 1 的直接回归：构造 state.on_entry_approved 签名本身收齐 6 字段）
def test_state_on_entry_approved_stores_all_exit_rules():
    from backtest.btc_eth.replay.state import VirtualState
    s = VirtualState(initial_equity=Decimal("300"), equity=Decimal("300"))
    ts_cfg = {"max_holding_hours": 2}
    # 真实冻结 profile 的嵌套结构（strategies/btc_eth/shadow/config/xau_original.yaml
    # 的 dynamic_trailing 段：activation.min_profit_pct + regression_tiers），
    # 不再固化扁平 {"activation_min_profit_pct": 1} 的错误形态
    tr_cfg = {
        "enabled": True,
        "activation": {"min_profit_pct": Decimal("1"), "also_on_tp1": True},
        "regression_tiers": [
            {"profit_ceiling": Decimal("1.5"), "retrace_ratio": Decimal("0.0")},
            {"profit_ceiling": Decimal("999"), "retrace_ratio": Decimal("0.5")},
        ],
    }
    s.on_entry_approved(
        at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        symbol="XAUUSDT",
        direction="LONG",
        quantity=Decimal("0.03"),
        entry_price=Decimal("2000"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss_price=Decimal("1980"),
        grade="A",
        tp1=Decimal("2020"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2040"),
        tp2_close_ratio=Decimal("0.40"),
        trailing=tr_cfg,
        time_stop=ts_cfg,
    )
    pos = s.open_position
    assert pos is not None
    assert pos["tp1"] == Decimal("2020")
    assert pos["tp1_close_ratio"] == Decimal("0.30")
    assert pos["tp2"] == Decimal("2040")
    assert pos["tp2_close_ratio"] == Decimal("0.40")
    assert pos["trailing"] == tr_cfg
    assert pos["time_stop"] == ts_cfg


# ---- trailing / time_stop 为 None 时不误触发 ----
def test_no_trigger_when_trailing_or_time_stop_is_none():
    engine = _make_engine_shell()
    # trailing=None, time_stop=None，即使 price 大幅回撤也不该触发 trailing
    engine.states["xau_original"].open_position = _long_pos(
        trailing=None, time_stop=None, tp1=None, tp2=None,
        stop_loss=Decimal("1950"),  # 把 stop_loss 拉远，只看 trailing/time_stop
    )
    bar = _kline(2025, 1990, 2025, 1995)
    # 时间不超 2h，且 trailing=None, time_stop=None → 没有任何退出规则应该触发
    # （stop_loss=1950 也没被触发，low=1990 > 1950）
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result is None


# ===========================================================================
# 同一根 K 线同时穿止损与止盈 → 止损优先（engine docstring 承诺的保守顺序）
# ===========================================================================
def test_same_bar_sl_and_tp_both_hit_sl_priority_long():
    """LONG：同一根 K 线 low 穿止损且 high 穿 TP1，无法判序 → 止损先发生。"""
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos(
        stop_loss=Decimal("1980"), tp1=Decimal("2020"),
    )
    # low=1975 <= 1980（穿止损），high=2025 >= 2020（穿 TP1），同根 K 线
    bar = _kline(2025, 1975, 2000, 1990)
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "STOP_LOSS"
    # 全平后无持仓，且没有 TP 部分平仓痕迹（最后一条 trade 的 exit_reason 是 STOP_LOSS）
    assert engine.states["xau_original"].open_position is None
    assert engine.replay_trades["xau_original"][-1]["exit_reason"] == "STOP_LOSS"


def test_same_bar_sl_and_tp_both_hit_sl_priority_short():
    """SHORT：同一根 K 线 high 穿止损且 low 穿 TP1 → 止损优先。"""
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _short_pos(
        stop_loss=Decimal("2020"), tp1=Decimal("1980"),
    )
    # high=2025 >= 2020（穿止损），low=1975 <= 1980（穿 TP1），同根 K 线
    bar = _kline(2025, 1975, 2000, 2010)
    result = engine._check_and_close_on_bar(
        "xau_original", bar, datetime(2026, 10, 1, 10, 1, tzinfo=UTC),
    )
    assert result == "STOP_LOSS"
    assert engine.states["xau_original"].open_position is None
    assert engine.replay_trades["xau_original"][-1]["exit_reason"] == "STOP_LOSS"


# ===========================================================================
# 限价单过期（expired_unfilled）与未到期仍挂起（pending）
# ===========================================================================
def _pending_limit_order(opened_at):
    """构造一条挂在 pending_limit_orders 里的限价入场单。"""
    return {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),  # LONG：K 线 low <= 1990 才成交
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None,
        "tp1_close_ratio": None,
        "tp2": None,
        "tp2_close_ratio": None,
        "trailing": None,
        "time_stop": None,
    }


def _flat_klines_above(price, count=2):
    """构造 count 根 low 均高于 price 的 K 线（限价不穿越）。"""
    out = []
    for i in range(count):
        t = datetime(2026, 10, 1, 10, 1 + i, tzinfo=UTC)
        out.append({
            "open_time": t.isoformat(),
            "high": str(price + Decimal("20")),
            "low": str(price + Decimal("10")),
            "open": str(price + Decimal("15")),
            "close": str(price + Decimal("18")),
            "volume": "1000",
        })
    return out


def test_limit_order_expired_unfilled():
    """限价单超时未成交：up_to 越过 expiry → 记 filled=False, unfilled_reason=expired_unfilled。"""
    engine = _make_engine_shell()
    engine.limit_expiry_seconds = 300
    opened_at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    engine.pending_limit_orders["xau_original"] = [_pending_limit_order(opened_at)]
    # low 均高于限价 1990 → 永不成交；末尾 10:06 的 K 线保证 expiry(10:05) 时刻 idx_end 非 None
    klines = _flat_klines_above(Decimal("1990"), count=6)

    # up_to = opened_at + 301s ≥ expiry(300s) → 过期
    up_to = opened_at + timedelta(seconds=301)
    engine._settle_pending_limit_orders("xau_original", up_to, klines)

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    assert trades[0]["filled"] is False
    assert trades[0]["unfilled_reason"] == "expired_unfilled"
    assert trades[0]["exit_reason"] is None
    assert Decimal(trades[0]["net_pnl"]) == 0
    # pending 清空，状态无持仓
    assert engine.pending_limit_orders["xau_original"] == []
    assert engine.states["xau_original"].open_position is None


def test_limit_order_still_pending_before_expiry():
    """限价单未到期且未穿越：保持 pending，不产生 unfilled trade。"""
    engine = _make_engine_shell()
    engine.limit_expiry_seconds = 300
    opened_at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    order = _pending_limit_order(opened_at)
    engine.pending_limit_orders["xau_original"] = [order]
    klines = _flat_klines_above(Decimal("1990"))

    # up_to = opened_at + 60s < expiry(300s)
    up_to = opened_at + timedelta(seconds=60)
    engine._settle_pending_limit_orders("xau_original", up_to, klines)

    assert engine.replay_trades["xau_original"] == []
    assert engine.pending_limit_orders["xau_original"] == [order]
    assert engine.states["xau_original"].open_position is None


def test_limit_order_settle_at_data_end_no_crash():
    """up_to 恰为最后一根 K 线 open_time 且未到期：不应崩溃，应保持 pending。"""
    engine = _make_engine_shell()
    engine.limit_expiry_seconds = 300
    opened_at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    order = _pending_limit_order(opened_at)
    engine.pending_limit_orders["xau_original"] = [order]
    klines = _flat_klines_above(Decimal("1990"), count=2)  # 最后一根 open_time=10:02

    # up_to = 10:02 == 最后一根 K 线 open_time → idx_end=None → 当前实现 TypeError
    up_to = datetime(2026, 10, 1, 10, 2, tzinfo=UTC)
    engine._settle_pending_limit_orders("xau_original", up_to, klines)

    # 期望行为：未到期且未穿越 → 仍 pending，不产生 trade，不崩溃
    assert engine.replay_trades["xau_original"] == []
    assert engine.pending_limit_orders["xau_original"] == [order]


# ===========================================================================
# 资金费净额口径（spec Scenario：资金费按净额计入权益；缺陷 2 修复端到端）
# ===========================================================================
def test_positive_funding_income_counted_in_engine_net_pnl_and_equity():
    """SHORT 持仓期间正资金费收入：funding_fee>0，且 equity 变动 == report 公式净额。

    场景：entry=close=2000（零价差），SHORT 0.03 张、rate=+0.01% → 收入 +0.006。
    手续费 -0.03、滑点 -0.012，net_pnl = 0 - 0.03 + 0.006 - 0.012 = -0.036。
    断言 equity 变动与 report 公式（realized - fee - slip + funding）完全一致。
    """
    engine = _make_engine_shell()
    state = engine.states["xau_original"]
    state.open_position = _short_pos()
    # funding 事件在 [opened_at, at] 区间内 → 被过滤命中
    engine.funding_events = [
        (datetime(2026, 10, 1, 10, 0, 1, tzinfo=UTC), Decimal("0.0001")),
        # 区间外事件：不应计入
        (datetime(2026, 10, 1, 9, 0, 0, tzinfo=UTC), Decimal("0.0100")),
    ]

    close_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    engine._close_position("xau_original", close_at, Decimal("2000"), "STOP_LOSS")

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    t = trades[0]
    # 资金费为正收入且只计入区间内一笔：0.03 * 2000 * 0.0001 = 0.006
    assert Decimal(t["funding_fee"]) == Decimal("0.006")
    # net_pnl = pnl(0) + fee(-0.03) + funding(+0.006) + slip(-0.012)
    assert Decimal(t["net_pnl"]) == Decimal("-0.036")
    # equity 口径一致：变动 == realized - fee - slip + funding（report 公式）
    s = engine.states["xau_original"]
    assert s.open_position is None
    assert s.funding_fee == Decimal("0.006")
    equity_delta = s.equity - s.initial_equity
    report_formula = s.realized_pnl - s.fee_paid - s.slippage_cost + s.funding_fee
    assert equity_delta == report_formula == Decimal("-0.036")


# ===========================================================================
# 期终强制平仓（FINAL_CLOSE）
# ===========================================================================
def _klines_series(count, base_price=Decimal("2000")):
    """构造 count 根 open_time 依次 +1min 的 K 线。"""
    out = []
    for i in range(count):
        t = datetime(2026, 10, 1, 10, 0 + i, tzinfo=UTC)
        out.append({
            "open_time": t.isoformat(),
            "high": str(base_price + Decimal("5")),
            "low": str(base_price - Decimal("5")),
            "open": str(base_price),
            "close": str(base_price),
            "volume": "1000",
        })
    return out


def test_final_close_force_closes_open_position():
    """数据末尾仍有持仓：按最后一根 K 线 close 强制平仓，exit_reason=FINAL_CLOSE。"""
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos()  # entry=2000
    klines = _klines_series(3, base_price=Decimal("2010"))  # 末根 close=2010

    cutoff = datetime(2026, 10, 1, 23, 0, tzinfo=UTC)  # 晚于全部 K 线
    engine._force_close_remaining_positions(klines, cutoff)

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "FINAL_CLOSE"
    assert trades[0]["filled"] is True
    assert Decimal(trades[0]["exit_price"]) == Decimal("2010")
    # 价格盈亏：(2010-2000) * 0.03 = +0.30，扣除费用后 equity 应增加
    s = engine.states["xau_original"]
    assert s.open_position is None
    assert s.equity > s.initial_equity


def test_final_close_empty_klines_no_crash():
    """K 线为空时期终强制平仓直接返回，不崩溃、不产生 trade。"""
    engine = _make_engine_shell()
    engine.states["xau_original"].open_position = _long_pos()
    engine._force_close_remaining_positions([], datetime(2026, 10, 1, 23, 0, tzinfo=UTC))
    assert engine.replay_trades["xau_original"] == []
    assert engine.states["xau_original"].open_position is not None  # 原持仓保留


# ===========================================================================
# 无保护平仓不变量
# ===========================================================================
def test_close_position_without_position_records_invariant():
    """无持仓却收到平仓：记 invariant_violations（unprotected_close），不崩溃。"""
    engine = _make_engine_shell()
    engine._close_position(
        "xau_original", datetime(2026, 10, 1, 11, 0, tzinfo=UTC),
        Decimal("2000"), "STOP_LOSS",
    )
    assert len(engine.invariant_violations) == 1
    v = engine.invariant_violations[0]
    assert v["profile_id"] == "xau_original"
    assert v["violation"] == "unprotected_close"
    assert engine.replay_trades["xau_original"] == []


# ===========================================================================
# F1 修复回归：入场手续费 + 出场手续费完整 roundtrip 口径
# ===========================================================================
def _do_fill_entry(engine, profile_id="xau_original", direction="LONG", qty=Decimal("0.03"),
                   entry_price=Decimal("2000"), stop_loss=Decimal("1980")):
    """走完整 _fill_entry 路径（撮合成功），写入 open_position 含 entry_fee_per_qty。"""
    engine._fill_entry(
        profile_id=profile_id,
        event={"symbol": "XAUUSDT", "direction": direction},
        opened_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        fill_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        fill_price=entry_price,
        quantity=qty,
        margin=Decimal("20"),
        leverage=3,
        stop_loss=stop_loss,
        grade="A",
        order_kind="MARKET",
    )


# ---- 测试 a: 一次完整 roundtrip fee ≈ 2 × 单边 ----
def test_roundtrip_fee_is_double_single_side():
    """LONG 0.03 qty @ 2000 入场、平仓 @ 2000，taker_bps=5。
    entry_fee = -0.03（notional 60），exit_fee = -0.03（notional 60），fee 合计 -0.06。
    """
    engine = _make_engine_shell()
    _do_fill_entry(engine, direction="LONG", qty=Decimal("0.03"), entry_price=Decimal("2000"))

    pos = engine.states["xau_original"].open_position
    assert pos is not None
    # 入场手续费按单位数量存入：-0.03 / 0.03 = -1.0
    assert pos["entry_fee_per_qty"] == Decimal("-1")

    # 平仓（entry=exit=2000，零价差）
    close_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    engine._close_position("xau_original", close_at, Decimal("2000"), "STOP_LOSS")

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    t = trades[0]
    # 双边合计手续费 = -0.06（精确 Decimal 断言）
    assert Decimal(t["fee"]) == Decimal("-0.06")
    # pnl = 0 + fee(-0.06) + slip(-0.012) = -0.072
    assert Decimal(t["net_pnl"]) == Decimal("-0.072")

    s = engine.states["xau_original"]
    # fee_paid 累计应 = abs(-0.06) = 0.06
    assert s.fee_paid == Decimal("0.06")


# ---- 测试 b: 分两次平仓 fee 之和 == 双边理论总额 ----
def test_partial_close_fee_sum_equals_full_roundtrip():
    """LONG 0.03 qty @ 2000 入场，第一次部分平 0.01（TP1，平仓价 2000），
    第二次全平剩余 0.02（STOP_LOSS，平仓价 2000）。两笔 fee 之和 == -0.06。"""
    engine = _make_engine_shell()
    _do_fill_entry(engine, direction="LONG", qty=Decimal("0.03"), entry_price=Decimal("2000"))

    # 第一次部分平仓 0.01（TP1）
    t1_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    engine._partial_close("xau_original", t1_at, Decimal("2000"), "TP1", Decimal("0.01"))

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    # entry_fee_per_qty = -1.0；close 0.01 → entry 分摊 -0.01；exit 手续费 -0.01（20 notional）→ fee=-0.02
    assert Decimal(trades[0]["fee"]) == Decimal("-0.02")
    # 剩余持仓量应更新为 0.02
    assert engine.states["xau_original"].open_position["quantity"] == Decimal("0.02")

    # 第二次全平剩余 0.02
    t2_at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    engine._close_position("xau_original", t2_at, Decimal("2000"), "STOP_LOSS")

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 2
    # 第二次：entry 分摊 -0.02；exit 手续费 -0.02（40 notional）→ fee=-0.04
    assert Decimal(trades[1]["fee"]) == Decimal("-0.04")

    # 两笔 fee 之和 == -0.06（完整 roundtrip 双边）
    fee_sum = Decimal(trades[0]["fee"]) + Decimal(trades[1]["fee"])
    assert fee_sum == Decimal("-0.06")

    # fee_paid 累计 == 0.06
    assert engine.states["xau_original"].fee_paid == Decimal("0.06")


# ---- 测试 c: 正资金费 + 双边手续费组合下 state.equity 与 report.compute_metrics 一致 ----
def test_equity_consistency_with_funding_and_roundtrip_fee():
    """SHORT 0.03 qty @ 2000 入场，平仓 @ 2000，funding_rate=+0.01% → funding 收入 +0.006。
    双边手续费 -0.06，出场滑点 -0.012。
    state.equity 变动应 == report.compute_metrics 的 net_pnl / final_equity。
    """
    from backtest.btc_eth.replay.report import compute_metrics

    engine = _make_engine_shell()
    _do_fill_entry(engine, direction="SHORT", qty=Decimal("0.03"), entry_price=Decimal("2000"))
    # 正资金费事件在区间内 → SHORT 收入
    engine.funding_events = [
        (datetime(2026, 10, 1, 10, 0, 1, tzinfo=UTC), Decimal("0.0001")),
    ]

    close_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    engine._close_position("xau_original", close_at, Decimal("2000"), "STOP_LOSS")

    s = engine.states["xau_original"]
    # 手算：pnl=0, fee=-0.06, funding=+0.006, slip=-0.012 → equity_delta = -0.066
    assert s.realized_pnl == Decimal("0")
    assert s.fee_paid == Decimal("0.06")           # abs(-0.06)
    assert s.funding_fee == Decimal("0.006")       # 正收入
    assert s.slippage_cost == Decimal("0.012")     # abs(-0.012)

    equity_delta = s.equity - s.initial_equity
    report_formula = s.realized_pnl - s.fee_paid - s.slippage_cost + s.funding_fee
    assert equity_delta == report_formula == Decimal("-0.066")

    # 构造 compute_metrics 输入，验证 final_equity 与 report 公式一致
    state_dict = {
        "final_equity": str(s.equity),
        "realized_pnl": str(s.realized_pnl),
        "fee_paid": str(s.fee_paid),
        "funding_fee": str(s.funding_fee),
        "slippage_cost": str(s.slippage_cost),
    }
    trades = engine.replay_trades["xau_original"]
    eq_hist = [(engine.replay_trades["xau_original"][0]["exit_time"], str(s.equity))]
    prof = {
        "trades": trades,
        "state": state_dict,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": eq_hist,
    }
    meta = {
        "replay_run_id": "test",
        "engine_version": "v1",
        "manifest_sha256": "x",
        "input_cutoff_at": close_at.isoformat(),
        "initial_equity": str(s.initial_equity),
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
    }
    m = compute_metrics(prof, meta)
    assert Decimal(m["final_equity"]) == s.equity
    # report.net_pnl = realized - fee_paid - slip_cost + funding = equity_delta
    assert Decimal(m["net_pnl"]) == equity_delta


# ---- 测试 d: 未成交订单 fee 仍为 "0"（入场不应产生手续费）----
def test_unfilled_order_fee_always_zero():
    """限价单过期未成交：trade 明细 fee 字段必须为 "0"（入场费不应在未成交时产生）。"""
    engine = _make_engine_shell()
    engine.limit_expiry_seconds = 300
    opened_at = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    engine.pending_limit_orders["xau_original"] = [_pending_limit_order(opened_at)]
    klines = _flat_klines_above(Decimal("1990"), count=6)
    up_to = opened_at + timedelta(seconds=301)
    engine._settle_pending_limit_orders("xau_original", up_to, klines)

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    t = trades[0]
    assert t["filled"] is False
    assert t["unfilled_reason"] == "expired_unfilled"
    assert t["fee"] == "0"
    assert t["funding_fee"] == "0"
    assert t["slippage"] == "0"
    assert Decimal(t["net_pnl"]) == Decimal("0")
    # 全局 state 不应有任何 fee_paid
    assert engine.states["xau_original"].fee_paid == Decimal("0")
    assert engine.states["xau_original"].open_position is None


# ===========================================================================
# F4 回归：_partial_close 在 min_notional clamp 抬升后 close_qty >= 持仓量 → 实际平光
# ===========================================================================
def test_partial_close_clamped_to_min_notional_flattens_position_no_crash():
    """小仓位经 min_notional clamp 抬升后，close_qty >= 持仓量，_partial_close 不应崩溃。

    场景：持仓 0.002 张 @ 2000（notional=4），entry_fee_per_qty=-1（入场费 0.002@2000=0.002
    → 每单位 -1）。TP1 close_ratio=0.5 → 原始 close_qty=0.001（notional=2 < min_notional=5）。
    clamp_notional 向上取整到 ceil(5/2000)=1（整单位），远大于持仓 0.002。
    → _execute_exit 内 remaining=0.002-1<0 → on_position_closed 置 open_position=None。

    修复前：_partial_close 在 _execute_exit 返回后无条件写 None["quantity"] → TypeError。
    修复后：检测到 None 直接 return。trade 记 exit_reason="TP1"、fee 含入场双边、
    state.fee_paid 累计、无 invariant 违反。
    """
    engine = _make_engine_shell()
    engine.min_notional = Decimal("5")

    # 直接构造带 entry_fee_per_qty 的 open_position（模拟入场完成状态）
    engine.states["xau_original"].open_position = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "entry_price": Decimal("2000"),
        "quantity": Decimal("0.002"),
        "margin": Decimal("4"),
        "leverage": 3,
        "opened_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        "stop_loss": Decimal("1980"),
        "tp1": Decimal("2020"),
        "tp1_close_ratio": Decimal("0.5"),
        "tp2": None,
        "tp2_close_ratio": None,
        "trailing": None,
        "time_stop": None,
        "grade": "A",
        "entry_fee_per_qty": Decimal("-1"),
    }

    close_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    # 触发：round(0.001)→0.001，clamp→1，round→1；close_qty=1 >> 0.002
    engine._partial_close("xau_original", close_at, Decimal("2020"), "TP1", Decimal("0.001"))

    # 核心断言：不崩溃，持仓已平光
    assert engine.states["xau_original"].open_position is None
    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TP1"
    assert trades[0]["filled"] is True
    assert engine.invariant_violations == []

    # fee 断言：加上限约束后 close_qty = min(1, 0.002) = 0.002（整仓平仓）
    #   exit_fee = -0.002 * 2020 * 5bps = -0.00202
    #   entry_fee_per_qty 分摊 = -1 * 0.002 = -0.002
    #   合计 fee = -0.00402
    assert Decimal(trades[0]["fee"]) == Decimal("-0.00402")
    assert engine.states["xau_original"].fee_paid == Decimal("0.00402")


# ===========================================================================
# F6 补充：上限约束不误伤正常部分平仓（close_qty 经 round/clamp/round 后仍 < 持仓量）
# ===========================================================================
def test_partial_close_not_truncated_when_close_qty_below_position():
    """正常部分平仓场景：close_qty 经 round/clamp/round 后仍 < 持仓量，
    上限约束 min(close_qty, pos_qty) 不应改变 close_qty；持仓剩余量 = pos_qty - close_qty。

    场景：持仓 0.01 张 @ 2000（notional=20 > min_notional=5）。
    TP1 close_ratio=0.5 → 原始 close_qty=0.005（notional=0.005*2020=10.1 >= 5，不受 clamp 影响）。
    round/clamp/round 后 close_qty 仍为 0.005 < 0.01 → 应保留 0.005 持仓。
    """
    engine = _make_engine_shell()
    engine.min_notional = Decimal("5")

    engine.states["xau_original"].open_position = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "entry_price": Decimal("2000"),
        "quantity": Decimal("0.01"),
        "margin": Decimal("20"),
        "leverage": 3,
        "opened_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        "stop_loss": Decimal("1980"),
        "tp1": Decimal("2020"),
        "tp1_close_ratio": Decimal("0.5"),
        "tp2": None,
        "tp2_close_ratio": None,
        "trailing": None,
        "time_stop": None,
        "grade": "A",
        "entry_fee_per_qty": Decimal("-1"),
    }

    close_at = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    engine._partial_close("xau_original", close_at, Decimal("2020"), "TP1", Decimal("0.005"))

    # 核心：不是整仓平仓，持仓仍存在且 quantity = 0.01 - 0.005 = 0.005
    pos = engine.states["xau_original"].open_position
    assert pos is not None
    assert Decimal(pos["quantity"]) == Decimal("0.005")

    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TP1"
    assert trades[0]["filled"] is True
    # 摊销量 = entry_fee_per_qty * close_qty = -1 * 0.005 = -0.005
    # exit_fee = -0.005 * 2020 * 5bps = -0.00505
    # 合计 = -0.01005
    assert Decimal(trades[0]["fee"]) == Decimal("-0.01005")
    assert engine.invariant_violations == []
