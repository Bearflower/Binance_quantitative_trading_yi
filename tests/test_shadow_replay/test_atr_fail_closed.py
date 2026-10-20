"""ATR 提取与 fail-closed 专项回归测试（缺陷 2 修复）。

缺陷 2 根因：_attempt_entry 用 event.get("atr", "0.01") 读顶层字段，
但冻结事件里没有顶层 atr，atr 只在 stop_loss_rule.atr_value 和
feature_snapshot.atr 两处。结果 atr 恒为 0.01 → 止损价几乎贴入场价。

修复后：_extract_atr(event) 优先读 stop_loss_rule.atr_value，
回退 feature_snapshot.atr；都缺失时返回 None，由调用方 fail-closed
跳过入场（不产生 pending 单、不记 trade、只打 warning）。
"""
from decimal import Decimal

from backtest.btc_eth.replay.engine import ReplayEngine


# ---- 直接测 _extract_atr 三分支 ----
def test_extract_atr_prefers_stop_loss_rule_atr_value():
    event = {
        "stop_loss_rule": {"atr_value": "5.23", "atr_multiplier": 2},
        "feature_snapshot": {"atr": "9.99"},  # 故意不同
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr == Decimal("5.23")


def test_extract_atr_falls_back_to_feature_snapshot():
    event = {
        "stop_loss_rule": {"atr_multiplier": 2},  # 无 atr_value
        "feature_snapshot": {"atr": "3.14"},
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr == Decimal("3.14")


def test_extract_atr_returns_none_when_both_missing():
    event = {
        "stop_loss_rule": {"atr_multiplier": 2},  # 无 atr_value
        "feature_snapshot": {"price_change_1h": 0.5},  # 无 atr
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr is None


def test_extract_atr_returns_none_when_event_has_no_rule_or_snapshot():
    event = {"symbol": "XAUUSDT", "direction": "LONG"}  # 啥都没有
    atr = ReplayEngine._extract_atr(event)
    assert atr is None


def test_extract_atr_returns_none_when_atr_is_zero():
    event = {
        "stop_loss_rule": {"atr_value": "0.0"},
        "feature_snapshot": {"atr": "0.00"},
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr is None  # 0 不算有效值


def test_extract_atr_returns_none_when_atr_is_negative():
    event = {
        "stop_loss_rule": {"atr_value": "-1.0"},
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr is None


def test_extract_atr_skips_top_level_atr_legacy_field():
    """修复前代码用 event.get('atr', '0.01') 读顶层字段；
    修复后顶层 atr 被忽略（冻结事件确实没有顶层 atr）。"""
    event = {
        "atr": "99.9",  # 顶层字段存在 → 修复后不应被当作有效 atr
        "stop_loss_rule": {"atr_value": "5.00"},
        "feature_snapshot": {"atr": "9.99"},
    }
    atr = ReplayEngine._extract_atr(event)
    assert atr == Decimal("5.00")  # 取 stop_loss_rule.atr_value，不是顶层 99.9


# ---- 验证正常 event 能正确算出止损价与 atr 成比例 ----
def test_attempt_entry_atr_drives_stop_proportionally():
    """构造最小 ReplayEngine 壳，验证 atr=10 会让 stop_loss_atr=2.0
    算出 stop_loss_price = 2000 - 10*2 = 1980（与 atr 成比例）。

    此前 atr 恒为 0.01 → stop_loss_price = 2000 - 0.01*2 = 1999.98（贴入场价）。
    """
    from datetime import datetime, timezone

    UTC = timezone.utc
    engine = ReplayEngine.__new__(ReplayEngine)
    # 为让 _attempt_entry 走完整个逻辑，构造最小 profile_cfg
    engine.profile_cfg_orig = {
        "pilot": {"max_single_position_margin_usdt": 30, "max_leverage": 3},
        "strategy": {
            "risk": {
                "signal_levels": {
                    "A": {
                        "stop_loss_atr_multiplier": 2.0,
                        "partial_take_profit": {
                            "tp1_atr_multiplier": 4.0,
                            "tp1_close_ratio": 0.30,
                            "tp2_atr_multiplier": 6.0,
                            "tp2_close_ratio": 0.40,
                        },
                        "dynamic_trailing": {
                            # 真实冻结 profile 的嵌套结构（activation/regression_tiers），
                            # 不再固化扁平 {"activation_min_profit_pct": 0.5} 形态
                            "enabled": True,
                            "activation": {"min_profit_pct": 0.5, "also_on_tp1": True},
                            "regression_tiers": [
                                {"profit_ceiling": 1.5, "retrace_ratio": 0.0},
                                {"profit_ceiling": 999.0, "retrace_ratio": 0.5},
                            ],
                        },
                        "time_stop": {
                            "max_holding_hours": 96,
                            "close_ratio": 1.0,
                        },
                    }
                }
            }
        },
    }
    engine.profile_cfg_aggr = engine.profile_cfg_orig  # 测试只用 orig
    # 任务 C：per-profile 仓位约束（替代旧的 self.max_margin/max_lev 单值）
    engine._profile_max_margin = {"xau_original": Decimal("30"), "xau_aggressive": Decimal("30")}
    engine._profile_max_lev = {"xau_original": 3, "xau_aggressive": 3}
    engine.states = {
        "xau_original": __import__(
            "backtest.btc_eth.replay.state", fromlist=["VirtualState"]
        ).VirtualState(initial_equity=Decimal("300"), equity=Decimal("300")),
        "xau_aggressive": __import__(
            "backtest.btc_eth.replay.state", fromlist=["VirtualState"]
        ).VirtualState(initial_equity=Decimal("300"), equity=Decimal("300")),
    }
    engine.tick_size = Decimal("0.01")
    engine.step_size = Decimal("0.001")
    engine.min_notional = Decimal("5")
    engine.pending_limit_orders = {"xau_original": [], "xau_aggressive": []}
    engine.replay_trades = {"xau_original": [], "xau_aggressive": []}
    # 任务 A：halt_lines_pct 单一真源（scan_triggers 从此读取）
    engine.halt_lines_pct = [15, 25, 30]

    event = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "grade": "A",
        "reference_price": Decimal("2000"),
        "intended_order_type": "MARKET",
        "intended_entry_price": Decimal("2000"),
        "stop_loss_rule": {"atr_value": "10.0", "atr_multiplier": 2},
        "feature_snapshot": {"atr": "10.0"},
        "scheduled_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC).isoformat(),
    }
    # _attempt_entry 会撮合失败（因为没有 klines，_find_next_kline_open 返回 None），
    # 但关键验证：不会被 atr 缺失 fail-closed 提前 return。
    # 用空 klines 调用 —— 预期不会提前 return（atr 是合法的 10.0），
    # 继续算到 _find_next_kline_open 返回 None → 返回 "no_klines"，
    # trade 里不应该有 "rejected:" 或 fail-closed warning。
    engine._attempt_entry(
        profile_id="xau_original",
        event=event,
        scheduled_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        klines=[],
    )
    # 至少 1 条 trade（"no_klines" 未成交）
    trades = engine.replay_trades["xau_original"]
    assert len(trades) >= 1
    assert trades[0]["unfilled_reason"] == "no_klines"


# ---- 验证 atr 缺失时 fail-closed ----
def test_attempt_entry_skips_when_atr_missing():
    from datetime import datetime, timezone

    UTC = timezone.utc
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.profile_cfg_orig = {"strategy": {"risk": {"signal_levels": {"A": {}}}}}
    engine.profile_cfg_aggr = engine.profile_cfg_orig
    engine.states = {
        "xau_original": __import__(
            "backtest.btc_eth.replay.state", fromlist=["VirtualState"]
        ).VirtualState(initial_equity=Decimal("300"), equity=Decimal("300")),
    }
    engine.max_margin = Decimal("30")
    engine.max_lev = 3
    engine.tick_size = Decimal("0.01")
    engine.step_size = Decimal("0.001")
    engine.min_notional = Decimal("5")
    engine.pending_limit_orders = {"xau_original": []}
    engine.replay_trades = {"xau_original": []}

    # event 没有任何 atr 字段
    event = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "grade": "A",
        "reference_price": Decimal("2000"),
        "intended_order_type": "MARKET",
        "intended_entry_price": Decimal("2000"),
        # 故意缺少 stop_loss_rule.atr_value 和 feature_snapshot.atr
        "stop_loss_rule": {"atr_multiplier": 2},
        "feature_snapshot": {"market_state": "TRENDING"},
        "scheduled_at": datetime(2026, 10, 1, 10, 0, tzinfo=UTC).isoformat(),
    }
    engine._attempt_entry(
        profile_id="xau_original",
        event=event,
        scheduled_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        klines=[],
    )
    # 断言：atr 缺失 → fail-closed 跳过入场，不产生任何 trade、
    # 也不产生任何 pending limit order
    assert engine.replay_trades["xau_original"] == []
    assert engine.pending_limit_orders["xau_original"] == []
