"""报告模块测试：sum(net_pnl) == 汇总净盈亏；n_trades<12 或全无交易 → admission=none；active+inactive == all。"""
from decimal import Decimal
import pytest
from datetime import datetime, timezone, timedelta

from backtest.btc_eth.replay.report import (
    compute_metrics, decide_admission, _compute_max_drawdown,
)


UTC = timezone.utc

# 阻断 #5：_aggregate_positions 按 position_id 分组聚合，
# 测试 fixture 必须给每条独立仓位分配唯一 position_id。
# 用闭包计数器确保每次 _mk_trade 调用得到不同 pid（独立 complete 仓位）。
_pid_seq = {"n": 0}


def _next_pid():
    _pid_seq["n"] += 1
    return f"test_pid_{_pid_seq['n']}"


def _make_engine_meta(initial_equity="300"):
    return {
        "replay_run_id": "abc123def456",
        "engine_version": "replay-v1.0.0",
        "manifest_sha256": "deadbeef",
        "input_cutoff_at": "2026-10-10T00:00:00+00:00",
        "initial_equity": initial_equity,
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
    }


def _make_profile_result(trades, state, eq_hist, counterfactual=None):
    if counterfactual is None:
        counterfactual = {"15pct": None, "25pct": None, "30pct": None}
    return {
        "trades": trades,
        "state": state,
        "counterfactual": counterfactual,
        "equity_history": eq_hist,
    }


def _mk_trade(net_pnl, exit_reason="STOP_LOSS", exit_time_hour=12, direction="LONG"):
    base = datetime(2026, 10, 1, exit_time_hour, 0, tzinfo=UTC)
    # 阻断 #5：每条 trade 必须有唯一 position_id，否则被 _aggregate_positions 跳过或错误合并
    # 批次三 A：entry_quantity（入场总数量）是 engine 明细的新契约字段——
    # 每条 _mk_trade 代表一个独立 complete 仓位，批次平仓量 == 入场总量
    # 引擎契约：net_pnl = realized_pnl + fee + funding_fee + slippage（fee/slip 为负）
    realized = float(net_pnl) + 0.03 + 0.012  # 反推 realized 使 net_pnl 精确闭合
    return {
        "position_id": _next_pid(),
        "entry_time": (base - timedelta(hours=2)).isoformat(),
        "entry_price": "2000",
        "exit_time": base.isoformat(),
        "exit_price": "2100" if net_pnl > 0 else "1950",
        "direction": direction,
        "symbol": "XAUUSDT",
        "filled": True,
        "unfilled_reason": None,
        "exit_reason": exit_reason,
        "quantity": "0.03",
        "entry_quantity": "0.03",
        "leverage": "3",
        "margin": "20",
        "realized_pnl": str(realized),
        "fee": "-0.03",
        "funding_fee": "0",
        "slippage": "-0.012",
        "net_pnl": str(net_pnl),
        "mae": "5",
        "mfe": "20",
        "holding_seconds": "7200",
    }


def test_sum_net_pnl_equals_total():
    """Σ 明细 net_pnl == report 汇总 net_pnl（精确断言，不再允许 5 的容差）。

    fixture 口径：每笔 realized_pnl = net_pnl + 0.042（fee -0.03 + slip -0.012），
    state 聚合与明细完全一致 → report 公式 net_pnl = realized - fee_paid - slip + funding
    必须精确等于 Σ 明细 net_pnl。
    """
    trades = [
        _mk_trade(Decimal("+10")),
        _mk_trade(Decimal("-5")),
        _mk_trade(Decimal("+8")),
    ]
    sum_net = sum(Decimal(t["net_pnl"]) for t in trades)                # = 13
    sum_realized = sum(Decimal(t["realized_pnl"]) for t in trades)      # = 13.126
    state = {
        "final_equity": str(Decimal("300") + sum_net),
        "realized_pnl": str(sum_realized),
        "fee_paid": "0.09",
        "funding_fee": "0",
        "slippage_cost": "0.036",
    }
    eq_hist = [
        ("2026-10-01T10:00:00+00:00", "300"),
        ("2026-10-01T14:00:00+00:00", str(Decimal("300") + sum_net)),
    ]
    r = _make_profile_result(trades, state, eq_hist)
    m = compute_metrics(r, _make_engine_meta())

    # 精确断言：报告净盈亏 == Σ 明细净盈亏（全部走 net_pnl 口径，含手续费+滑点）
    assert Decimal(m["net_pnl"]) == sum_net == Decimal("13")
    # 交叉校验：state 口径自洽（final_equity = initial + net_pnl）
    assert Decimal(m["final_equity"]) == Decimal("313")


def test_less_than_12_trades_admission_none():
    trades = [_mk_trade(Decimal("+10")) for _ in range(11)]
    state = {
        "final_equity": "400",
        "realized_pnl": "120",
        "fee_paid": "0.33",
        "funding_fee": "0",
        "slippage_cost": "0.132",
    }
    eq_hist = [("2026-10-01T10:00:00+00:00", "300"), ("2026-10-05T10:00:00+00:00", "400")]
    r = _make_profile_result(trades, state, eq_hist)
    m = compute_metrics(r, _make_engine_meta())
    assert m["admission_checks"]["min_closed_trades"]["pass"] is False


def test_zero_trades_admission_none():
    trades = []
    state = {
        "final_equity": "300",
        "realized_pnl": "0",
        "fee_paid": "0",
        "funding_fee": "0",
        "slippage_cost": "0",
    }
    eq_hist = [("2026-10-01T10:00:00+00:00", "300")]
    r = _make_profile_result(trades, state, eq_hist)
    m = compute_metrics(r, _make_engine_meta())
    assert m["n_closed_trades"] == 0
    assert m["admission_checks"]["min_closed_trades"]["pass"] is False


def test_max_drawdown_15pct_ok():
    eq_hist = [
        ("2026-10-01T10:00:00+00:00", "300"),
        ("2026-10-02T10:00:00+00:00", "280"),  # peak=300, dd=(300-280)/300=6.67%
        ("2026-10-03T10:00:00+00:00", "255"),  # peak=300, dd=15%（刚好）
    ]
    dd = _compute_max_drawdown(eq_hist, Decimal("300"))
    assert abs(dd - 15.0) < 0.01


def test_max_drawdown_below_peak_after_rally():
    eq_hist = [
        ("2026-10-01T10:00:00+00:00", "300"),
        ("2026-10-02T10:00:00+00:00", "260"),  # dd = 40/300 = 13.3%
        ("2026-10-03T10:00:00+00:00", "310"),  # new peak
        ("2026-10-04T10:00:00+00:00", "290"),  # dd = 20/310 ≈ 6.45%
    ]
    dd = _compute_max_drawdown(eq_hist, Decimal("300"))
    assert abs(dd - 13.3333) < 0.01


def test_active_plus_inactive_equals_all():
    # 活跃时段 07-20；非活跃 <7 或 >=20
    all_trades = [
        _mk_trade(Decimal("+10"), exit_time_hour=8),   # active
        _mk_trade(Decimal("+10"), exit_time_hour=14),  # active
        _mk_trade(Decimal("-5"), exit_time_hour=23),   # inactive
        _mk_trade(Decimal("+5"), exit_time_hour=3),    # inactive
    ]
    state = {
        "final_equity": "320",
        "realized_pnl": "20",
        "fee_paid": "0.12",
        "funding_fee": "0",
        "slippage_cost": "0.048",
    }
    eq_hist = [("2026-10-01T10:00:00+00:00", "300"), ("2026-10-05T10:00:00+00:00", "320")]
    r = _make_profile_result(all_trades, state, eq_hist)
    m = compute_metrics(r, _make_engine_meta())

    assert m["active_trades"] + m["inactive_trades"] == m["n_closed_trades"]
    assert m["active_trades"] == 2
    assert m["inactive_trades"] == 2


def test_decide_admission_picks_lower_dd():
    # 构造两 profile 都通过准入：
    # 原版：dd=10%, exp=0.5
    # 激进版：dd=8%, exp=0.6
    # 期望选激进版（回撤更小）
    orig = {"max_drawdown_pct": 10.0, "expectancy": 0.5, "admission_checks": {
        "min_closed_trades": {"pass": True},
        "net_profit_positive": {"pass": True},
        "profit_factor": {"pass": True},
        "max_drawdown": {"pass": True},
        "no_invariant_violations": {"pass": True},
        "no_structural_loss_after_best_removed": {"pass": True},
        "no_30pct_breach_at_2x_slippage": {"pass": True},
    }}
    aggr = {"max_drawdown_pct": 8.0, "expectancy": 0.6, "admission_checks": {
        "min_closed_trades": {"pass": True},
        "net_profit_positive": {"pass": True},
        "profit_factor": {"pass": True},
        "max_drawdown": {"pass": True},
        "no_invariant_violations": {"pass": True},
        "no_structural_loss_after_best_removed": {"pass": True},
        "no_30pct_breach_at_2x_slippage": {"pass": True},
    }}
    result = decide_admission(orig, aggr)
    assert result["admission"] == "aggressive"


def test_decide_admission_none_when_both_fail():
    orig = {"max_drawdown_pct": 30.0, "expectancy": -0.1, "admission_checks": {
        "min_closed_trades": {"pass": False},  # 两个都不通过
        "net_profit_positive": {"pass": True},
        "profit_factor": {"pass": True},
        "max_drawdown": {"pass": True},
        "no_invariant_violations": {"pass": True},
        "no_structural_loss_after_best_removed": {"pass": True},
        "no_30pct_breach_at_2x_slippage": {"pass": True},
    }}
    aggr = {"max_drawdown_pct": 5.0, "expectancy": 0.1, "admission_checks": {
        "min_closed_trades": {"pass": False},
        "net_profit_positive": {"pass": True},
        "profit_factor": {"pass": True},
        "max_drawdown": {"pass": True},
        "no_invariant_violations": {"pass": True},
        "no_structural_loss_after_best_removed": {"pass": True},
        "no_30pct_breach_at_2x_slippage": {"pass": True},
    }}
    result = decide_admission(orig, aggr)
    assert result["admission"] == "none"


# ===========================================================================
# 准入门槛 ②③⑥⑦ 逐项直测（_admission_checks 的分支覆盖）
# ===========================================================================
def _zero_fee_trade(net_pnl, direction="LONG"):
    """零手续费/零滑点/零资金费的已结交易，便于精确推算汇总口径。"""
    t = _mk_trade(Decimal(net_pnl), direction=direction)
    t["fee"] = "0"
    t["slippage"] = "0"
    t["funding_fee"] = "0"
    return t


def _admission_input(trades, net_pnl, initial_equity="300", final_equity="300"):
    """构造 compute_metrics 输入，专测 admission_checks。

    net_pnl 会写入 state.realized_pnl（零费口径下 report 的 net_pnl == realized_pnl）。
    """
    state = {
        "final_equity": final_equity,
        "realized_pnl": str(net_pnl),
        "fee_paid": "0",
        "funding_fee": "0",
        "slippage_cost": "0",
    }
    eq_hist = [("2026-10-01T10:00:00+00:00", initial_equity)]
    r = _make_profile_result(trades, state, eq_hist)
    meta = _make_engine_meta(initial_equity=initial_equity)
    m = compute_metrics(r, meta, {
        "admission": {
            "min_closed_trades": 12,
            "min_profit_factor": 1.20,
            "max_drawdown_pct": 15,
            "capital_preservation_pct": 30,
        },
        "slippage": {"baseline_bps": 2},
    })
    return m["admission_checks"]


def _closed_trade(net_pnl, direction="LONG"):
    """标准费用口径的已结交易（_mk_trade 原样）。"""
    return _mk_trade(Decimal(net_pnl), direction=direction)


def test_admission_net_profit_positive():
    """② 净盈利 > 0：盈利过 / 亏损不过 / 零不过。"""
    trades = [_zero_fee_trade("+5") for _ in range(12)]
    checks = _admission_input(trades, net_pnl=Decimal("60"))
    assert checks["net_profit_positive"]["pass"] is True

    trades_loss = [_zero_fee_trade("-5") for _ in range(12)]
    checks_loss = _admission_input(trades_loss, net_pnl=Decimal("-60"))
    assert checks_loss["net_profit_positive"]["pass"] is False

    checks_zero = _admission_input(trades, net_pnl=Decimal("0"))
    assert checks_zero["net_profit_positive"]["pass"] is False


def test_admission_profit_factor_threshold():
    """③ PF >= 1.2：达标过 / 不达标不过 / 全胜(inf) 时 actual=None 且 pass。

    注意 PF = 总盈/总亏（realized_pnl 口径，_mk_trade 的 realized = net_pnl*1.1）。
    零盈亏 trade（realized=0）不计入 wins/losses，用于凑 12 笔闭环。
    """
    # PF = 22/11 = 2.0 >= 1.2 → pass
    trades = [_closed_trade("+20"), _closed_trade("-10")] + [_zero_fee_trade("0") for _ in range(10)]
    checks = _admission_input(trades, net_pnl=Decimal("8"))
    assert checks["profit_factor"]["pass"] is True
    assert checks["profit_factor"]["actual"] == 2.0

    # PF = 5.5/11 = 0.5 < 1.2 → fail
    trades_bad = [_closed_trade("+5"), _closed_trade("-10")] + [_zero_fee_trade("0") for _ in range(10)]
    checks_bad = _admission_input(trades_bad, net_pnl=Decimal("-5"))
    assert checks_bad["profit_factor"]["pass"] is False

    # 全胜（PF=inf）场景拆到下方 D4 留痕测试，本函数只测有限 PF 的 pass/fail。


def test_admission_profit_factor_inf_should_pass():
    """③ 全胜（PF=inf）应通过 PF 门槛。"""
    trades_all_win = [_zero_fee_trade("+5") for _ in range(12)]
    checks_inf = _admission_input(trades_all_win, net_pnl=Decimal("60"))
    assert checks_inf["profit_factor"]["actual"] == "inf"
    assert checks_inf["profit_factor"]["pass"] is True


# ===========================================================================
# write_reports 落盘冒烟（.md + .json 双文件 + 准入 none）
# ===========================================================================
def test_write_reports_smoke(tmp_path):
    """write_reports：落盘 report_<run_id>.md/.json，内容含准入结论与指标。"""
    import json as _json
    from backtest.btc_eth.replay.report import write_reports

    trades = [_zero_fee_trade("+1") for _ in range(3)]
    prof = _make_profile_result(
        trades,
        {"final_equity": "300", "realized_pnl": "0", "fee_paid": "0",
         "funding_fee": "0", "slippage_cost": "0"},
        [("2026-10-01T10:00:00+00:00", "300")],
    )
    engine_result = {
        "xau_original": prof,
        "xau_aggressive": prof,
        "engine_metadata": _make_engine_meta(),
    }
    run_manifest = {"run_id": "xau-shadow-TEST"}

    out = write_reports(engine_result, run_manifest, str(tmp_path), replay_config_path=None)

    json_path = tmp_path / "report_xau-shadow-TEST.json"
    md_path = tmp_path / "report_xau-shadow-TEST.md"
    assert json_path.exists() and md_path.exists()
    report = _json.loads(json_path.read_text(encoding="utf-8"))
    assert report["run_id"] == "xau-shadow-TEST"
    assert report["admission"]["admission"] == "none"  # 3 笔 < 12 → 不准入
    md_text = md_path.read_text(encoding="utf-8")
    assert "暂不准入 (none)" in md_text
    assert "净盈亏" in md_text
    assert out["admission"] == "none"


# ===========================================================================
# fees.py 异常分支：非法 direction
# ===========================================================================
def test_sum_funding_fee_invalid_direction_raises():
    """direction 非 LONG/SHORT → ValueError（异常路径覆盖）"""
    import pytest as _pytest
    from backtest.btc_eth.replay.fees import sum_funding_fee
    with _pytest.raises(ValueError):
        sum_funding_fee(
            Decimal("1"), Decimal("2000"), Decimal("2000"), "FLAT",
            [(datetime(2026, 10, 1, tzinfo=UTC), Decimal("0.0001"))],
        )


def test_admission_no_structural_loss_after_best_removed():
    """⑥ 扣除最佳单笔后剩余净盈亏 >= 0：结构性依赖单笔 → fail；均衡盈利 → pass。"""
    # 1 笔 +55 + 11 笔 +0.5：去掉最佳后剩 5.5 → pass
    trades_ok = [_closed_trade("+55")] + [_closed_trade("+0.5") for _ in range(11)]
    checks_ok = _admission_input(trades_ok, net_pnl=Decimal("60.5"))
    assert checks_ok["no_structural_loss_after_best_removed"]["pass"] is True
    assert Decimal(checks_ok["no_structural_loss_after_best_removed"]["actual"]) == Decimal("5.5")

    # 1 笔 +55 扛全局，其余 11 笔合计 -60.5：去掉最佳后 -60.5 → fail
    trades_structural = [_closed_trade("+55")] + [_closed_trade("-5.5") for _ in range(11)]
    checks_bad = _admission_input(trades_structural, net_pnl=Decimal("-5.5"))
    assert checks_bad["no_structural_loss_after_best_removed"]["pass"] is False


def test_admission_capital_preservation_30pct_at_2x_slippage():
    """⑦ 2× 滑点压力下不触 30% 红线（equity < initial*0.70=210 → fail）。"""
    # 无交易：notional=0，压力后 equity=final=300 >= 210 → pass
    checks = _admission_input([], net_pnl=Decimal("0"))
    assert checks["no_capital_preservation_breach_at_2x_slippage"]["pass"] is True

    # final=215（未破 210 红线），1 笔大 notional（2000*100=200000）+ 11 笔小（合计 660）
    # 压力扣 (200660)*2/10000 = 40.132 → 215-40.13 = 174.87 < 210 → fail
    # 批次三 A 契约：聚合 quantity 取 entry_quantity，大仓位明细必须同步补齐该字段
    big = _closed_trade("+1")
    big["entry_price"] = "2000"
    big["quantity"] = "100"
    big["entry_quantity"] = "100"
    trades_big = [big] + [_closed_trade("+1") for _ in range(11)]
    checks_press = _admission_input(
        trades_big, net_pnl=Decimal("12"),
        initial_equity="300", final_equity="215",
    )
    assert checks_press["no_capital_preservation_breach_at_2x_slippage"]["pass"] is False
    assert Decimal(checks_press["no_capital_preservation_breach_at_2x_slippage"]["breach_line"]) == Decimal("210")


def test_positive_funding_income_counted_in_report_net_pnl():
    """不变式：report 公式 net_pnl = realized - fee - slip + funding 应等于 equity 实际变动。

    场景：SHORT 持仓，funding_rate>0 → 空头收资金费（funding=+0.5）。
    """
    from backtest.btc_eth.replay.state import VirtualState
    s = VirtualState(initial_equity=Decimal("300"), equity=Decimal("300"))
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("10"),
        fee=Decimal("-0.3"),
        funding=Decimal("+0.5"),   # 正收入
        slippage=Decimal("-0.1"),
        close_reason="STOP_LOSS",
        # Round4 修复：整笔净盈亏 = 10 - 0.3 + 0.5 - 0.1 = 10.1
        position_net_pnl=Decimal("10.1"),
    )
    # equity 口径完整：300 + 10 - 0.3 + 0.5 - 0.1 = 310.1
    assert s.equity - s.initial_equity == Decimal("10.1")
    # 报告公式口径应与 equity 变动一致（当前实现：funding_fee 只累计负值 → 0 → 9.6 ≠ 10.1）
    net_pnl_report = s.realized_pnl - s.fee_paid - s.slippage_cost + s.funding_fee
    assert net_pnl_report == s.equity - s.initial_equity
