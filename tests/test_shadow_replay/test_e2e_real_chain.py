"""XAUUSDT MTCPS 影子回放器端到端测试（真实链路，不测孤立函数）。

覆盖七条关键链路：
  1. 真实毫秒 K 线 → collector 三周期闭合校验（毫秒通过 / 秒级被拒）
  2. 真实冻结 profile 嵌套配置（xau_original.yaml 的 dynamic_trailing）驱动
     engine._check_dynamic_trailing，经 _run_position_exit_up_to 逐 K 线
     running peak 更新 → regression_tiers 选档 → 全平
  3. 分批退出（TP1 部分平仓 + 最终平仓）按 position_id 聚合为一笔，
     quantity == entry_quantity，net_pnl == Σ 批次净盈亏
  4. 逐分钟盯市：equity_history 三元组、max_drawdown 反映持仓期回撤、
     counterfactual 15% 触发点 open_position=True
  5. 数据缺口 fail-closed：main.py 返回 3 + admission=insufficient_data；
     --allow-gaps 继续且报告 data_completeness.complete=False
  6. 同点决策差异：same_timestamp_decision_diff 三项数值
  7. 三档滑点压力：1/2/4 bps 档存在、2bps == final_equity、4bps <= 2bps

时间戳口径：整条链路统一毫秒（open_time/close_time 为 13 位 Unix 毫秒），
严禁秒级（10 位）——这是本轮修复的核心契约。
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from shared.config_loader import compute_config_hash

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.state import VirtualState
from backtest.btc_eth.replay.report import (
    _aggregate_positions,
    _compute_max_drawdown,
    compute_metrics,
)
from backtest.btc_eth.replay.counterfactual import scan_triggers
from backtest.btc_eth.replay.snapshot import _compute_manifest_sha256
from backtest.btc_eth.replay import main as replay_main
from strategies.btc_eth.shadow.collector import _validate_all_intervals

UTC = timezone.utc

ROOT = Path(__file__).resolve().parents[2]
ORIG_YAML = ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_original.yaml"
AGGR_YAML = ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_aggressive.yaml"
REPLAY_CFG = ROOT / "backtest" / "btc_eth" / "replay" / "config.yaml"


# ===========================================================================
# 通用构造辅助
# ===========================================================================

def _make_engine_shell(initial_equity=Decimal("300")):
    """不加载 config 文件的 ReplayEngine 壳（与 test_engine_exit_rules 同风格）。"""
    engine = ReplayEngine.__new__(ReplayEngine)
    engine.states = {
        "xau_original": VirtualState(initial_equity=initial_equity, equity=initial_equity),
        "xau_aggressive": VirtualState(initial_equity=initial_equity, equity=initial_equity),
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
    # 任务 C：per-profile 仓位约束（引擎在 _attempt_entry 里按 profile_id 取值）
    engine._profile_max_margin = {
        "xau_original": Decimal("30"), "xau_aggressive": Decimal("30"),
    }
    engine._profile_max_lev = {"xau_original": 3, "xau_aggressive": 3}
    # 任务 A：反事实停牌的亏损红线（单一真源，engine.run() 调 scan_triggers 时传入）
    engine.halt_lines_pct = [15, 25, 30]
    return engine


def _mk_1m(open_time_ms, o, h, l, c):
    """真实 Binance 1m K 线 dict（毫秒 open_time/close_time）。"""
    return {
        "open_time": open_time_ms,
        "open": str(o),
        "high": str(h),
        "low": str(l),
        "close": str(c),
        "volume": "100",
        "close_time": open_time_ms + 59_999,
        "quote_volume": "200000",
        "count": 10,
    }


def _flat_klines_ms(start_dt, n, high="2001", low="1999"):
    """n 根毫秒 1m K 线，close 恒 2000，波动 [low, high]（不触发任何退出规则）。"""
    t0 = int(start_dt.timestamp() * 1000)
    return [_mk_1m(t0 + i * 60_000, "2000", high, low, "2000") for i in range(n)]


# ===========================================================================
# 用例 1：真实毫秒 K 线 → collector 三周期闭合校验
# ===========================================================================

def _gen_closed_klines(step_ms, count, now_ms):
    """生成 count 根已闭合、毫秒步长恒定的 K 线（末根 close_time = now - step）。"""
    last_close = now_ms - step_ms  # 恰好闭合
    return [
        {"close_time": last_close - (count - 1 - i) * step_ms, "open_time": 0}
        for i in range(count)
    ]


def test_real_ms_klines_pass_three_interval_validation():
    """真实毫秒 close_time 的 1h/4h/1d 三周期必须全部通过闭合校验。"""
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    multi = {
        "1h": _gen_closed_klines(3_600_000, 60, now_ms),
        "4h": _gen_closed_klines(14_400_000, 60, now_ms),
        "1d": _gen_closed_klines(86_400_000, 60, now_ms),
    }
    all_valid, reasons, close_times = _validate_all_intervals(
        multi, ["1h", "4h", "1d"], now_ms,
    )
    assert all_valid is True, f"合法毫秒 K 线不应被拒：{reasons}"
    assert reasons == {}
    assert set(close_times.keys()) == {"1h", "4h", "1d"}


def test_second_based_klines_rejected_on_all_intervals():
    """秒级（10 位）close_time 必须被三周期校验拒绝（步长断链）。

    单周期级「秒级被拒 / 毫秒通过」回归已存在于
    tests/test_strategies/test_shadow_collector.py；本用例在 _validate_all_intervals
    层证明三周期同时 fail-closed。
    """
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    # 末根 close_time 用秒级数值（约 1.7e9），相邻差 3600/14400/86400（秒）
    multi = {
        "1h": _gen_closed_klines(3_600, 60, 1_800_000_000),
        "4h": _gen_closed_klines(14_400, 60, 1_800_000_000),
        "1d": _gen_closed_klines(86_400, 60, 1_800_000_000),
    }
    all_valid, reasons, _ = _validate_all_intervals(multi, ["1h", "4h", "1d"], now_ms)
    assert all_valid is False
    # 三个周期都被拒，reason 均含断链/未闭合语义
    assert set(reasons.keys()) == {"1h", "4h", "1d"}


# ===========================================================================
# 用例 2：真实冻结 profile 嵌套配置驱动动态 trailing
# ===========================================================================

def test_real_nested_trailing_config_from_frozen_profile_triggers_full_close():
    """从 xau_original.yaml 读 S 档 dynamic_trailing（真实嵌套结构），
    构造「先涨 4% 激活 trailing、再回撤触发」的 1m 序列：
      - S 档 activation.min_profit_pct=3.0；peak=2080 → pct=4.0%
      - regression_tiers 按 profit_ceiling 升序 [1.5, 4.0, 8.0, 999]：
        pct=4.0 落在 ceiling=8.0 档 → retrace_ratio=0.35
      - stop = 2080 - (2080-2000)*0.35 = 2052
      - K2 low=2045 <= 2052 → TRAILING_STOP 全平，平仓价 = final_stop = 2052
    若实现错选 0.5 档 → stop=2040，K2 low 2045 > 2040 不会触发；
    错选 0.25 档 → stop=2060，K1 low 2060 <= 2060 会提前触发。
    因此断言「K2 触发且价 2052」精确区分了档位选择。
    """
    with open(ORIG_YAML, "r", encoding="utf-8") as f:
        profile_cfg = yaml.safe_load(f)
    trailing_real = profile_cfg["strategy"]["risk"]["signal_levels"]["S"]["dynamic_trailing"]
    # 真实配置形态自证：必须是嵌套结构，而非扁平键
    assert "activation" in trailing_real and "regression_tiers" in trailing_real
    assert "min_profit_pct" in trailing_real["activation"]

    engine = _make_engine_shell()
    state = engine.states["xau_original"]
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    # 走真实入场路径写入 open_position（含 running peak 初始化 + entry_quantity）
    state.on_entry_approved(
        at=opened_at,
        symbol="XAUUSDT",
        direction="LONG",
        quantity=Decimal("0.03"),
        entry_price=Decimal("2000"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss_price=None,   # 拉开止损，隔离 trailing 链路
        grade="S",
        tp1=None,               # 拉开 TP，隔离 trailing 链路
        tp2=None,
        trailing=trailing_real,
        atr=Decimal("10"),
    )

    klines = [
        _mk_1m(int(datetime(2026, 1, 5, 10, 1, tzinfo=UTC).timestamp() * 1000),
               "2030", "2080", "2060", "2075"),  # peak→2080（pct=4% 激活），low 不触发
        _mk_1m(int(datetime(2026, 1, 5, 10, 2, tzinfo=UTC).timestamp() * 1000),
               "2070", "2078", "2045", "2050"),  # low=2045 <= 2052 → 触发
    ]
    engine._run_position_exit_up_to(
        "xau_original", datetime(2026, 1, 5, 10, 5, tzinfo=UTC), klines,
        datetime(2026, 1, 5, 10, 20, tzinfo=UTC),
    )

    assert state.open_position is None, "trailing 触发后必须全平"
    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "TRAILING_STOP"
    # 平仓价 = 触发时 final_stop=2052 → 证明选中 0.35 档（非 0.5/0.25/保本）
    assert Decimal(t["exit_price"]) == Decimal("2052")
    # K1 未触发 → 应留下持仓期逐分钟盯市点（has_open_position=True）
    marked_points = [row for row in state.equity_history if row[2] is True]
    assert len(marked_points) >= 1


def test_real_nested_trailing_keeps_activated_when_profit_pulls_back():
    """trailing 激活后浮盈回落不退出激活态（单向保持契约）。

    S 档 min_profit_pct=3.0：K1 冲高 4% 激活（未触发回撤），
    K2 浮盈回落到 0.05%（pct < min_profit）——已激活则必须继续维持
    trailing 逻辑而非 return None；K3 回撤触发全平。
    """
    with open(ORIG_YAML, "r", encoding="utf-8") as f:
        profile_cfg = yaml.safe_load(f)
    trailing_real = profile_cfg["strategy"]["risk"]["signal_levels"]["S"]["dynamic_trailing"]

    engine = _make_engine_shell()
    state = engine.states["xau_original"]
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at, symbol="XAUUSDT", direction="LONG",
        quantity=Decimal("0.03"), entry_price=Decimal("2000"),
        margin=Decimal("20"), leverage=3, stop_loss_price=None, grade="S",
        tp1=None, tp2=None, trailing=trailing_real, atr=Decimal("10"),
    )
    klines = [
        # K1: peak→2080（pct=4% ≥ 3% 激活），low 2060 > stop 2052 不触发
        _mk_1m(int(datetime(2026, 1, 5, 10, 1, tzinfo=UTC).timestamp() * 1000),
               "2030", "2080", "2060", "2065"),
        # K2: 浮盈回落（close 2001 → 峰值浮盈仍 4%，但当前浮盈 0.05%），
        #     peak 不变 → stop 仍 2052；low 2055 > 2052 不触发
        _mk_1m(int(datetime(2026, 1, 5, 10, 2, tzinfo=UTC).timestamp() * 1000),
               "2010", "2015", "2055", "2001"),
        # K3: low 2040 <= 2052 → 触发（此时 pct 回落到 0.05% < min_profit，
        #     若实现错误地因浮盈回落退出激活态 → 不触发 → 本断言失败）
        _mk_1m(int(datetime(2026, 1, 5, 10, 3, tzinfo=UTC).timestamp() * 1000),
               "2005", "2010", "2040", "2045"),
    ]
    engine._run_position_exit_up_to(
        "xau_original", datetime(2026, 1, 5, 10, 10, tzinfo=UTC), klines,
        datetime(2026, 1, 5, 10, 20, tzinfo=UTC),
    )
    trades = engine.replay_trades["xau_original"]
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TRAILING_STOP"
    assert Decimal(trades[0]["exit_price"]) == Decimal("2052")


# ===========================================================================
# 用例 3：分批退出按 position_id 聚合为一笔
# ===========================================================================

def test_partial_then_final_close_aggregates_to_single_trade():
    """真实链路：_fill_entry 入场 0.03 → TP1 部分平 0.009 → 最终 STOP_LOSS 平 0.021。
    _aggregate_positions 必须输出恰好 1 笔：
      - quantity == entry_quantity（入场总数量 0.03，而非任何批次平仓量）
      - net_pnl == Σ 两批次 net_pnl
      - exit_reason 取终结原因 STOP_LOSS
    再经 compute_metrics 断言 n_closed_trades == 1（分批不被虚增为多笔）。
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
        stop_loss=Decimal("1980"),
        grade="S",
        order_kind="MARKET",
        tp1=Decimal("2020"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2040"),
        tp2_close_ratio=Decimal("0.40"),
        atr=Decimal("10"),
        stop_loss_atr_multiplier="2.5",
        position_id="e2e_partial_pid_1",
    )

    # TP1 部分平仓（0.03 × 0.30 = 0.009）
    engine._partial_close(pid, datetime(2026, 1, 5, 10, 30, tzinfo=UTC),
                          Decimal("2020"), "TP1", Decimal("0.009"))
    # 最终止损全平剩余 0.021
    engine._close_position(pid, datetime(2026, 1, 5, 11, 0, tzinfo=UTC),
                           Decimal("1980"), "STOP_LOSS")

    trades = engine.replay_trades[pid]
    assert len(trades) == 2, "engine 明细层是动作级两条流水"
    for t in trades:
        assert Decimal(str(t["entry_quantity"])) == Decimal("0.03"), (
            "每条明细必须携带入场总数量"
        )

    aggregated = _aggregate_positions(trades)
    assert len(aggregated) == 1, f"分批必须聚合为 1 笔，实际 {len(aggregated)}"
    agg = aggregated[0]
    assert Decimal(str(agg["quantity"])) == Decimal("0.03"), (
        f"聚合 quantity 应为入场总数量 0.03，实际 {agg['quantity']}"
    )
    expected_net = sum(Decimal(t["net_pnl"]) for t in trades)
    assert Decimal(agg["net_pnl"]) == expected_net
    assert agg["exit_reason"] == "STOP_LOSS"
    assert agg["exit_time"] == trades[-1]["exit_time"]
    assert len(agg["exits"]) == 2
    assert [e["exit_reason"] for e in agg["exits"]] == ["TP1", "STOP_LOSS"]

    # 报告层：n_closed_trades 不被分批虚增
    state = {
        "final_equity": str(engine.states[pid].equity),
        "realized_pnl": str(engine.states[pid].realized_pnl),
        "fee_paid": str(engine.states[pid].fee_paid),
        "funding_fee": str(engine.states[pid].funding_fee),
        "slippage_cost": str(engine.states[pid].slippage_cost),
    }
    meta = {
        "replay_run_id": "e2e", "engine_version": "v1", "manifest_sha256": "x",
        "input_cutoff_at": "2026-01-05T12:00:00+00:00",
        "initial_equity": "300",
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
    }
    prof = {
        "trades": trades, "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": [],
    }
    m = compute_metrics(prof, meta)
    assert m["n_closed_trades"] == 1, "分批平仓不得被当成多笔已结交易"


# ===========================================================================
# 用例 4：逐分钟盯市 → equity_history 三元组 / max_drawdown / counterfactual
# ===========================================================================

def test_marked_equity_per_minute_captures_intra_position_drawdown():
    """持仓期间一度浮亏 15%、最终回本：
      - initial_equity=60（放大回撤敏感度；0.03 qty → 300 点价差 = 9 USDT = 15%）
      - K2 close 1700 → marked=51 = 60×0.85 → 恰好触 15% 线
      - 断言 equity_history 持仓期逐分钟盯市点全部 has_open_position=True
      - max_drawdown_pct == 15.0（peak=60 → 谷 51）
      - counterfactual 15% 触发非 None、open_position=True、25%/30% 未触发
    """
    engine = _make_engine_shell(initial_equity=Decimal("60"))
    pid = "xau_original"
    state = engine.states[pid]
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at, symbol="XAUUSDT", direction="LONG",
        quantity=Decimal("0.03"), entry_price=Decimal("2000"),
        margin=Decimal("20"), leverage=3, stop_loss_price=None, grade="S",
        tp1=None, tp2=None, trailing=None, time_stop=None, atr=None,
    )

    klines = [
        _mk_1m(int(datetime(2026, 1, 5, 10, 1, tzinfo=UTC).timestamp() * 1000),
               "2000", "2001", "1999", "2000"),   # marked = 60.00
        _mk_1m(int(datetime(2026, 1, 5, 10, 2, tzinfo=UTC).timestamp() * 1000),
               "2000", "2001", "1690", "1700"),   # marked = 51.00 → 15% 线
        _mk_1m(int(datetime(2026, 1, 5, 10, 3, tzinfo=UTC).timestamp() * 1000),
               "1700", "1999", "1690", "1999"),   # marked = 59.97
        _mk_1m(int(datetime(2026, 1, 5, 10, 4, tzinfo=UTC).timestamp() * 1000),
               "1999", "2001", "1995", "2000"),   # marked = 60.00 回本
    ]
    engine._run_position_exit_up_to(
        pid, datetime(2026, 1, 5, 10, 10, tzinfo=UTC), klines,
        datetime(2026, 1, 5, 10, 20, tzinfo=UTC),
    )

    # 全程无退出规则触发（无止损/TP/trailing/时间止损）→ 仍持仓
    assert state.open_position is not None
    hist = state.equity_history
    # 持仓期逐分钟盯市点：4 根 K 线各一条，全部 has_open_position=True
    assert len(hist) >= 4
    assert all(row[2] is True for row in hist), (
        f"持仓期盯市点必须 has_open_position=True：{hist}"
    )
    marked_values = [row[1] for row in hist]
    assert Decimal("51") in marked_values, f"盯市曲线必须包含谷底 51：{marked_values}"

    # max_drawdown 基于盯市曲线：peak=60 → 谷 51 → 15.0%
    dd = _compute_max_drawdown([(t.isoformat(), str(e), bool(h)) for t, e, h in hist], Decimal("60"))
    assert abs(dd - 15.0) < 0.01, f"max_drawdown 应反映盯市回撤 15%，实际 {dd}"

    # counterfactual：15% 触发时点非 None 且当时持仓；25/30% 未触发
    ct = scan_triggers(hist, state.initial_equity, halt_lines_pct=[15, 25, 30])
    assert ct.trigger_15pct is not None
    trig_at, trig_eq, trig_has_pos = ct.trigger_15pct
    assert trig_has_pos is True
    assert trig_eq == Decimal("51")
    assert trig_at == datetime(2026, 1, 5, 10, 2, tzinfo=UTC)
    assert ct.trigger_25pct is None
    assert ct.trigger_30pct is None


# ===========================================================================
# 用例 5/6/7：main.py 真实 CLI 端到端（缺口 fail-closed / 决策差异 / 滑点压力）
# ===========================================================================

EXCHANGE_INFO = {
    "symbols": [{
        "symbol": "XAUUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ],
    }],
}


def _cli_events():
    """两 profile 在相同 scheduled_at 上部分决策不同的真实事件集。

    10:00  original=(candidate, LONG, A) vs aggressive=(candidate, SHORT, S) → 差异
    10:05  两 profile 同为 (no_signal, None, None) → 相同
    → common=2, differing=1, ratio=0.5
    """
    t0 = "2026-01-05T10:00:00+00:00"
    t1 = "2026-01-05T10:05:00+00:00"
    return [
        {"profile_id": "xau_original", "symbol": "XAUUSDT", "scheduled_at": t0,
         "decision": "candidate", "direction": "LONG", "grade": "A",
         "reference_price": "2000", "intended_order_type": "MARKET",
         "intended_entry_price": "2000",
         "stop_loss_rule": {"atr_value": "10.0", "atr_multiplier": 1.5}},
        {"profile_id": "xau_aggressive", "symbol": "XAUUSDT", "scheduled_at": t0,
         "decision": "candidate", "direction": "SHORT", "grade": "S",
         "reference_price": "2000", "intended_order_type": "MARKET",
         "intended_entry_price": "2000",
         "stop_loss_rule": {"atr_value": "10.0", "atr_multiplier": 3.0}},
        {"profile_id": "xau_original", "symbol": "XAUUSDT", "scheduled_at": t1,
         "decision": "no_signal", "direction": None, "grade": None},
        {"profile_id": "xau_aggressive", "symbol": "XAUUSDT", "scheduled_at": t1,
         "decision": "no_signal", "direction": None, "grade": None},
    ]


def _write_cli_inputs(tmp_path, klines, gaps):
    """落盘完整 CLI 输入：snapshot（含 SHA-256 manifest）+ events + run_manifest。"""
    snap = tmp_path / "snapshot"
    snap.mkdir()
    kl_path = snap / "xauusdt_1m.jsonl"
    kl_path.write_text("\n".join(json.dumps(k) for k in klines), encoding="utf-8")
    fund_path = snap / "xauusdt_funding.json"
    fund_path.write_text("[]", encoding="utf-8")
    ei_path = snap / "xauusdt_exchange_info.json"
    ei_path.write_text(json.dumps(EXCHANGE_INFO), encoding="utf-8")

    def _sha(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    # 先独立构造 files 字典（含真实 sha256），再用共享 helper 计算顶层 manifest 哈希
    # ——避免手动复刻 canonical 公式导致口径漂移（verify_snapshot 依赖同一 helper）
    files = {
        "klines_1m": {"path": str(kl_path), "rows": len(klines), "sha256": _sha(kl_path)},
        "funding_rates": {"path": str(fund_path), "rows": 0, "sha256": _sha(fund_path)},
        "exchange_info": {"path": str(ei_path), "sha256": _sha(ei_path)},
    }
    manifest = {
        "symbol": "XAUUSDT", "interval": "1m",
        "start": "2026-01-05T10:00:00+00:00", "end": "2026-01-05T10:20:00+00:00",
        "gaps": gaps, "sha256": _compute_manifest_sha256(files),
        "files": files,
    }
    (snap / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in _cli_events()), encoding="utf-8",
    )
    run_manifest_path = tmp_path / "run_manifest.json"
    run_manifest_path.write_text(json.dumps({
        "run_id": "e2e-xau-shadow",
        "data_cutoff_at": "2026-01-05T10:20:00+00:00",
        "initial_virtual_equity_usdt": 300,
        # engine 构造函数会校验 profile 配置哈希（fail-closed），fixture 必须提供真实值
        "config_hashes": {
            "xau_original": compute_config_hash(yaml.safe_load(ORIG_YAML.read_text())),
            "xau_aggressive": compute_config_hash(yaml.safe_load(AGGR_YAML.read_text())),
        },
    }), encoding="utf-8")
    return snap, events_path, run_manifest_path


def _run_cli(tmp_path, snap, events_path, run_manifest_path, allow_gaps=False):
    out_dir = tmp_path / f"reports_{int(allow_gaps)}"
    argv = [
        "--events", str(events_path),
        "--run-manifest", str(run_manifest_path),
        "--snapshot-dir", str(snap),
        "--profile-orig", str(ORIG_YAML),
        "--profile-aggr", str(AGGR_YAML),
        "--output-dir", str(out_dir),
    ]
    if allow_gaps:
        argv.append("--allow-gaps")
    return replay_main.main(argv), out_dir


def test_e2e_gaps_fail_closed_rejects_admission(tmp_path):
    """缺口 fail-closed：manifest gaps>0 且未 --allow-gaps → 返回 3 +
    stdout 结构化 admission=insufficient_data；传 --allow-gaps 后继续回放，
    报告 data_completeness.complete=False（gaps 如实透传）。"""
    klines = _flat_klines_ms(datetime(2026, 1, 5, 10, 0, tzinfo=UTC), 20)
    gaps = [{
        "from_open_time": klines[3]["open_time"],
        "to_open_time": klines[4]["open_time"] + 3 * 60_000,
        "missing_minutes": 3,
    }]
    snap, events_path, run_manifest_path = _write_cli_inputs(tmp_path, klines, gaps)

    # 1) 默认 fail-closed
    rc, _ = _run_cli(tmp_path, snap, events_path, run_manifest_path, allow_gaps=False)
    assert rc == 3, f"缺口且未 --allow-gaps 必须返回 3，实际 {rc}"

    # 2) --allow-gaps 显式放行 → 走完回放并落盘报告
    rc2, out_dir = _run_cli(tmp_path, snap, events_path, run_manifest_path, allow_gaps=True)
    assert rc2 == 0, f"--allow-gaps 后应正常完成，实际 {rc2}"
    report = json.loads(
        (out_dir / "report_e2e-xau-shadow.json").read_text(encoding="utf-8"),
    )
    assert report["data_completeness"]["complete"] is False
    assert report["data_completeness"]["gaps"] == 1  # manifest 里 1 处缺口如实透传


def test_e2e_decision_diff_and_slippage_pressure_on_clean_snapshot(tmp_path):
    """干净快照（gaps=0）走完 main.py 全链路：
      6) same_timestamp_decision_diff：common=2 / differing=1 / ratio=0.5
      7) slippage_pressure：1/2/4 bps 三档存在、2bps == final_equity、4bps <= 2bps
    """
    klines = _flat_klines_ms(datetime(2026, 1, 5, 10, 0, tzinfo=UTC), 20)
    snap, events_path, run_manifest_path = _write_cli_inputs(tmp_path, klines, gaps=[])

    rc, out_dir = _run_cli(tmp_path, snap, events_path, run_manifest_path, allow_gaps=False)
    assert rc == 0, f"干净快照应正常完成，实际 {rc}"
    report = json.loads(
        (out_dir / "report_e2e-xau-shadow.json").read_text(encoding="utf-8"),
    )
    assert report["data_completeness"] == {"gaps": 0, "complete": True}

    # —— 用例 6：同点决策差异 ——
    diff = report["decision_summary"]["same_timestamp_decision_diff"]
    assert diff["common_scheduled_count"] == 2
    assert diff["differing_count"] == 1
    assert diff["differing_ratio"] == pytest.approx(0.5, rel=1e-9)
    assert len(diff["samples"]) == 1
    sample = diff["samples"][0]
    assert sample["original"]["decision"] == "candidate"
    assert sample["original"]["direction"] == "LONG"
    assert sample["aggressive"]["decision"] == "candidate"
    assert sample["aggressive"]["direction"] == "SHORT"

    # —— 用例 7：三档滑点压力（对两 profile 都断言）——
    for pid in ("xau_original", "xau_aggressive"):
        m = report[pid]
        slip = m["slippage_pressure"]
        assert slip["baseline_bps"] == 2
        assert slip["tiers_bps"] == [1, 2, 4]
        for tier_key in ("1bps", "2bps", "4bps"):
            assert tier_key in slip, f"{pid} 缺 {tier_key} 档"
        final_eq = Decimal(m["final_equity"])
        # baseline=2bps → 2bps 档增量 0 → 恰等于 final_equity
        assert Decimal(slip["2bps"]) == final_eq, (
            f"{pid}: 2bps 档应等于 final_equity={final_eq}，实际 {slip['2bps']}"
        )
        # 滑点越高权益越低
        assert Decimal(slip["4bps"]) <= Decimal(slip["2bps"])
        assert Decimal(slip["2bps"]) <= Decimal(slip["1bps"])
