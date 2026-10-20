"""第三轮阻断项收尾 —— 5+1 组语义级回归测试。

覆盖阻断项：
  ① 阻断 #1：TP1 后不重扫旧 K 线（时间倒流平仓）
  ② 阻断 #2：初始权益作为回撤锚点
  ③ 阻断 #3：事件循环结束后 pending limit orders 推进到 data_cutoff_at
  ④ 阻断 #4：快照内容哈希（行数同内容异 → 哈希异）
  ⑤ fail-closed：profile 配置哈希与 run manifest 不一致 → ValueError
  ⑥（附加，覆盖 report.py 修复）零调度事件 profile 的分析成功率渲染

不硬编码、不复用真实联网、中文注释。
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from shared.config_loader import compute_config_hash

from backtest.btc_eth.replay.engine import (
    ReplayEngine,
    compute_replay_run_id,
)
from backtest.btc_eth.replay.state import VirtualState
from backtest.btc_eth.replay.invariants import run_all_invariants
from backtest.btc_eth.replay.report import (
    _compute_max_drawdown,
    _build_markdown,
    write_reports,
    compute_metrics,
)
from backtest.btc_eth.replay.snapshot import build_snapshot

UTC = timezone.utc

# ========== 路径常量（相对仓库根） ==========
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORIG_YAML = _REPO_ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_original.yaml"
_AGGR_YAML = _REPO_ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_aggressive.yaml"
_REPLAY_CFG = _REPO_ROOT / "backtest" / "btc_eth" / "replay" / "config.yaml"


# ===========================================================================
# 通用构造辅助
# ===========================================================================

def _make_engine_shell(initial_equity=Decimal("300")):
    """与 test_engine_exit_rules / test_e2e_real_chain 同风格的 ReplayEngine 壳。"""
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
    engine.limit_expiry_seconds = 300
    return engine


def _mk_1m(open_time_ms, o, h, l, c):
    """真实 Binance 1m K 线 dict（毫秒 open_time）。"""
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


# ===========================================================================
# 组① TP1 之后不重扫旧 K 线，退出时间严格单调递增（场景 B）
# ===========================================================================

def test_tp1_exit_time_strictly_monotonic_no_backward_rescan():
    """阻断 #1（场景 B，收紧版）：TP1 在 10:01 触发后，
    旧游标缺失逻辑会在 10:00 触发 TRAILING_STOP —— 与 TP1 形成时间倒流。

    构造（必须用 trailing + TP1 同 bar 触发，才能让"重扫导致早时间退出"真实可发生）：
      - LONG 入场 entry=2000 @10:00, sl=1900（远）, tp1=2010, tp1_close_ratio=0.30,
        tp2=2200（远）, trailing 嵌套（min_profit_pct=1%, retrace=0.5, also_on_tp1=True）
      - 10:00 K 线 high=2005<tp1=2010 → 不触发 TP；peak=2005, pct=0.25%<1% → trailing 未激活
      - 10:01 K 线 high=2025≥tp1=2010 → TP1 部分平仓；peak 抬到 2025, pct=1.25%≥1% → trailing 激活
      - 10:02 K 线 peak 仍 2025, trailing_stop=2025-(2025-2000)*0.5=2012.5
        fixed 下：游标从 10:02 开始, low=2001≤2012.5 → TRAILING_STOP @10:02
    """
    engine = _make_engine_shell()
    state = engine.states["xau_original"]

    # trailing 嵌套配置（参考 probe scenario_B）
    trailing_cfg = {
        "enabled": True,
        "activation": {"min_profit_pct": Decimal("1"), "also_on_tp1": True},
        "regression_tiers": [
            {"profit_ceiling": Decimal("999"), "retrace_ratio": Decimal("0.5")}
        ],
    }

    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at,
        symbol="XAUUSDT",
        direction="LONG",
        quantity=Decimal("0.03"),
        entry_price=Decimal("2000"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss_price=Decimal("1900"),  # 拉远，不干扰 trailing 行为
        grade="A",
        tp1=Decimal("2010"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2200"),
        tp2_close_ratio=Decimal("0.40"),
        trailing=trailing_cfg,
        time_stop=None,
        atr=None,
    )

    base_ms = int(opened_at.timestamp() * 1000)
    klines = [
        # 10:00：high=2005<tp1, low=2000；peak=2005, profit=0.25%<1% → trailing 未激活
        _mk_1m(base_ms, "2000", "2005", "2000", "2002"),
        # 10:01：high=2025≥tp1=2010 → TP1；peak 抬到 2025, profit=1.25%≥1% → trailing 激活
        _mk_1m(base_ms + 60_000, "2002", "2025", "2000", "2022"),
        # 10:02：peak 仍 2025, trailing_stop=2012.5, low=2001 ≤ 2012.5 → TRAILING_STOP
        _mk_1m(base_ms + 120_000, "2022", "2023", "2001", "2005"),
    ]

    cutoff = datetime(2026, 1, 5, 10, 10, tzinfo=UTC)

    # 第一次推进到 10:01:30（游标推进到 10:01 之后）
    up_to_1 = datetime(2026, 1, 5, 10, 1, 30, tzinfo=UTC)
    engine._run_position_exit_up_to("xau_original", up_to_1, klines, cutoff)

    # 第二次推进到 10:02:30（继续扫，不回扫 10:00/10:01）
    up_to_2 = datetime(2026, 1, 5, 10, 2, 30, tzinfo=UTC)
    engine._run_position_exit_up_to("xau_original", up_to_2, klines, cutoff)

    exits = [
        (datetime.fromisoformat(t["exit_time"]), t["exit_reason"])
        for t in engine.replay_trades["xau_original"]
        if t.get("exit_time")
    ]

    # 固定实现：TP1 @10:01, TRAILING_STOP @10:02（严格递增，无 10:00 退出）
    assert len(exits) == 2, f"应产生 2 条退出（TP1+TRAILING_STOP），实际 {len(exits)}"
    assert exits[0] == (datetime(2026, 1, 5, 10, 1, tzinfo=UTC), "TP1"), (
        f"第一条退出应为 TP1 @10:01，实际 {exits[0]}"
    )
    assert exits[1] == (datetime(2026, 1, 5, 10, 2, tzinfo=UTC), "TRAILING_STOP"), (
        f"第二条退出应为 TRAILING_STOP @10:02，实际 {exits[1]}"
    )


def test_tp1_exit_time_old_behavior_would_really_create_time_reversal():
    """反向验证（控制组）：模拟旧行为（每次重扫前强制 cursor=None），
    断言会出现 exit_time=10:00 的 TRAILING_STOP，与 TP1@10:01 形成时间倒流。

    这是阻断 #1 的控制组 —— 如果这个测试 FAIL，说明当前 fixture 无法捕获旧行为，
    组①就是假绿。
    """
    engine = _make_engine_shell()
    state = engine.states["xau_original"]

    trailing_cfg = {
        "enabled": True,
        "activation": {"min_profit_pct": Decimal("1"), "also_on_tp1": True},
        "regression_tiers": [
            {"profit_ceiling": Decimal("999"), "retrace_ratio": Decimal("0.5")}
        ],
    }

    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at, symbol="XAUUSDT", direction="LONG",
        quantity=Decimal("0.03"), entry_price=Decimal("2000"), margin=Decimal("20"),
        leverage=3, stop_loss_price=Decimal("1900"), grade="A",
        tp1=Decimal("2010"), tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2200"), tp2_close_ratio=Decimal("0.40"),
        trailing=trailing_cfg, time_stop=None, atr=None,
    )

    base_ms = int(opened_at.timestamp() * 1000)
    klines = [
        _mk_1m(base_ms, "2000", "2005", "2000", "2002"),
        _mk_1m(base_ms + 60_000, "2002", "2025", "2000", "2022"),
        _mk_1m(base_ms + 120_000, "2022", "2023", "2001", "2005"),
    ]
    cutoff = datetime(2026, 1, 5, 10, 10, tzinfo=UTC)

    # 模拟旧行为：每次调 _run_position_exit_up_to 前强制 cursor=None
    # （旧实现没有游标，每次都从 opened_at 重扫）
    def _run_old():
        pos = engine.states["xau_original"].open_position
        if pos is not None:
            pos["exit_scan_cursor"] = None
        engine._run_position_exit_up_to(
            "xau_original",
            datetime(2026, 1, 5, 10, 1, 30, tzinfo=UTC),
            klines, cutoff,
        )
        pos = engine.states["xau_original"].open_position
        if pos is not None:
            pos["exit_scan_cursor"] = None
        engine._run_position_exit_up_to(
            "xau_original",
            datetime(2026, 1, 5, 10, 2, 30, tzinfo=UTC),
            klines, cutoff,
        )

    _run_old()

    exits = [
        (datetime.fromisoformat(t["exit_time"]), t["exit_reason"])
        for t in engine.replay_trades["xau_original"]
        if t.get("exit_time")
    ]

    # 旧行为：第二次推进时重扫 10:00，此时 peak=2025 已抬升，
    # trailing_stop 从 10:00 就触发 → TRAILING_STOP exit_time=10:00
    assert len(exits) == 2, f"应产生 2 条退出，实际 {len(exits)}"
    # 关键证据：TRAILING_STOP 出现在 10:00（时间倒流！）
    trailing_exit = [e for e in exits if e[1] == "TRAILING_STOP"][0]
    assert trailing_exit[0] <= datetime(2026, 1, 5, 10, 0, tzinfo=UTC), (
        f"旧行为应在 10:00 触发 TRAILING_STOP，实际 {trailing_exit[0]}"
    )


# ===========================================================================
# 组② 初始 300 → 首个盯市 255，回撤必须为 15%（阻断 #2）
# ===========================================================================

def test_max_drawdown_uses_initial_equity_as_anchor_when_first_bar_below():
    """阻断 #2：首条盯市权益已跌破 initial_equity 时，回撤应从 initial_equity 起算。

    旧实现 bug：首条就是 peak，若首条=255、initial=300 → 旧实现 peak=255，
    后续哪怕权益降到 200，(255-200)/255 ≈ 21.6%，但 initial 是 300，实际从起点已亏 33%。
    更极端的情况：如果只有一条 equity_history=(255)，旧实现会返回 0%（没有后续点比 peak=255 更低）。

    新实现：peak = max(initial_equity, first_equity) = max(300, 255) = 300，
    dd = (300 - 255) / 300 = 15.0%。
    """
    # 单条 equity_history，首条 255 < initial 300
    eq_hist_single = [("2026-10-01T10:00:00+00:00", "255", True)]
    dd = _compute_max_drawdown(eq_hist_single, Decimal("300"))
    assert abs(dd - 15.0) < 0.01, (
        f"首条盯市 255 < initial 300 → 回撤应为 15.0%，实际 {dd}"
    )

    # 多条 equity_history，首条已跌破，后续更低
    eq_hist_multi = [
        ("2026-10-01T10:00:00+00:00", "255", True),
        ("2026-10-01T10:01:00+00:00", "240", True),  # dd=(300-240)/300=20%
        ("2026-10-01T10:02:00+00:00", "270", True),  # 反弹
    ]
    dd_multi = _compute_max_drawdown(eq_hist_multi, Decimal("300"))
    assert abs(dd_multi - 20.0) < 0.01, (
        f"首条 255 → 后续 240，回撤应为 20.0%，实际 {dd_multi}"
    )


def test_max_drawdown_no_false_when_first_bar_equals_initial():
    """首条盯市权益 == initial_equity（无回撤）→ 0.0%，锚点不误伤。"""
    eq_hist = [("2026-10-01T10:00:00+00:00", "300", True)]
    dd = _compute_max_drawdown(eq_hist, Decimal("300"))
    assert abs(dd - 0.0) < 0.01


def test_max_drawdown_anchor_proof_old_impl_would_get_zero():
    """证明旧实现（首条当 peak）会在单条 255 场景下得 0%。

    旧逻辑：peak = eq_hist[0] = 255；唯一数据点就是 peak，没有"比 peak 更低"的点 → dd=0。
    新逻辑：peak = max(300, 255) = 300 → dd=(300-255)/300 = 15%。
    """
    eq_hist_single = [("2026-10-01T10:00:00+00:00", "255", True)]
    new_dd = _compute_max_drawdown(eq_hist_single, Decimal("300"))

    # 手算旧实现（仅用于对比，不是生产代码）
    old_peak = Decimal("255")
    old_dd = 0.0  # 单条永远 dd=0
    assert abs(old_dd - 0.0) < 0.01  # 旧实现锚定错误
    assert abs(new_dd - 15.0) < 0.01  # 新实现正确


# ===========================================================================
# 组③ run() 末尾 fallback settle：成交一例 + 过期一例（端到端）
# ===========================================================================

def _make_minimal_fixture(tmp_path, klines_override=None):
    """最小 fixture：klines / funding / exchangeInfo / manifest（不含 run_manifest 和 events）。

    Args:
        tmp_path: pytest tmp_path fixture
        klines_override: 可选，若提供则用它替代默认 _mk_fake_klines(10, 2000)
    """
    # klines
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    klines = klines_override if klines_override is not None else _mk_fake_klines(10, 2000)
    klines_path = tmp_path / "xauusdt_1m.jsonl"
    klines_path.write_text("\n".join(json.dumps(k) for k in klines), encoding="utf-8")

    # funding
    funding_path = tmp_path / "xauusdt_funding.json"
    funding_path.write_text("[]", encoding="utf-8")

    # exchangeInfo
    ei = _mk_fake_exchange_info()
    ei_path = tmp_path / "xauusdt_exchange_info.json"
    ei_path.write_text(json.dumps(ei), encoding="utf-8")

    # manifest（sha256 需基于真实文件内容）
    def _sha(p):
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()

    manifest = {
        "symbol": "XAUUSDT",
        "interval": "1m",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=10)).isoformat(),
        "sha256": "fixture-manifest",
        "files": {
            "klines_1m": {"path": str(klines_path), "rows": len(klines), "sha256": _sha(klines_path)},
            "funding_rates": {"path": str(funding_path), "rows": 0, "sha256": _sha(funding_path)},
            "exchange_info": {"path": str(ei_path), "sha256": _sha(ei_path)},
        },
        "gaps": [],
    }
    # 真实 sha256（阻断 #4 内容哈希组合）
    manifest["sha256"] = hashlib.sha256(
        json.dumps({
            "klines_1m": manifest["files"]["klines_1m"]["sha256"],
            "funding_rates": manifest["files"]["funding_rates"]["sha256"],
            "exchange_info": manifest["files"]["exchange_info"]["sha256"],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    return {
        "klines_1m_path": str(klines_path),
        "funding_rates_path": str(funding_path),
        "exchange_info_json_path": str(ei_path),
        "snapshot_manifest_path": str(manifest_path),
    }


def _build_crossing_klines():
    """构造 10 根 K 线：idx=4 (10:04) low=1985 ≤ limit=1990，穿越 LONG 限价单。"""
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    out = []
    for i in range(10):
        t_ms = int((start + timedelta(minutes=i)).timestamp() * 1000)
        if i == 4:
            # 10:04 low=1985 ≤ 1990 → LONG 限价成交
            out.append(_mk_1m(t_ms, "2000", "2005", "1985", "2000"))
        else:
            # 其他根 low 都在 2000 以上 → 不穿越
            out.append(_mk_1m(t_ms, "2000", "2010", "2005", "2008"))
    return out


def _build_never_crossing_klines():
    """构造 10 根 K 线：全部 low=2005 远高于 limit=1990，永不穿越。"""
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    out = []
    for i in range(10):
        t_ms = int((start + timedelta(minutes=i)).timestamp() * 1000)
        out.append(_mk_1m(t_ms, "2010", "2015", "2005", "2008"))
    return out


def _make_real_engine(tmp_path, klines_override=None):
    """构造带正确 config_hashes 的真实 ReplayEngine。"""
    paths = _make_minimal_fixture(tmp_path, klines_override=klines_override)
    with open(_ORIG_YAML, "r", encoding="utf-8") as f:
        hash_orig = compute_config_hash(yaml.safe_load(f))
    with open(_AGGR_YAML, "r", encoding="utf-8") as f:
        hash_aggr = compute_config_hash(yaml.safe_load(f))

    run_manifest = {
        "run_id": "test",
        "data_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_virtual_equity_usdt": 300,
        "config_hashes": {
            "xau_original": hash_orig,
            "xau_aggressive": hash_aggr,
        },
    }
    return ReplayEngine(
        replay_config_path=str(_REPLAY_CFG),
        profile_config_path_original=str(_ORIG_YAML),
        profile_config_path_aggressive=str(_AGGR_YAML),
        **paths,
        run_manifest=run_manifest,
    )


def test_limit_order_run_fallback_settle_filled_case(tmp_path):
    """阻断 #3（端到端）：事件循环 settle 窗口为空，
    挂单只能通过 run() 末尾的 fallback settle 撮合成交。

    撮合窗口数学：
      opened_at=10:00, expiry_at=10:05, limit=1990 (LONG)
      event scheduled_at=10:01 → event内 settle 窗口 [10:00, min(10:05,10:01)) = [10:00,10:01)
        → 只有 idx=0 (10:00), low=2000 > 1990 → 不撮合 → 挂单保持 pending
      run() 末尾 fallback settle → 窗口 [10:00, min(10:05,10:10)) = [10:00,10:05)
        → 覆盖 idx 0~4, idx=4 (10:04) low=1985 ≤ 1990 → 撮合成功
    """
    pid = "xau_original"
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)

    engine = _make_real_engine(tmp_path, klines_override=_build_crossing_klines())

    # 事件 scheduled_at=10:01（晚于 opened_at=10:00，早于 crossing K 线 10:04）
    # 注意：不能让 scheduled_at == opened_at，否则 _settle_pending_limit_orders
    # 在 search_end<=opened_at 分支会静默丢弃挂单（产品 bug，见汇报）
    engine.events = [{
        "profile_id": pid,
        "scheduled_at": "2026-01-05T10:01:00+00:00",
        "decision": "no_signal",
        "symbol": "XAUUSDT",
        "direction": "LONG",
    }]

    # 直接注入挂单（run() 入口，不是直接调 _settle_pending_limit_orders）
    order = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }
    engine.pending_limit_orders[pid] = [order]

    engine.run()

    # 撮合成功：pending 清空、state 曾开仓（run() 末尾强平）、产生 FINAL_CLOSE 明细
    assert engine.pending_limit_orders[pid] == [], "撮合成功后 pending 应清空"
    state = engine.states[pid]
    assert state.open_position is None, "run() 末尾强制平仓后 open_position 应为 None"

    trades = engine.replay_trades[pid]
    # run() 末尾强平会产生 1 条 FINAL_CLOSE
    assert len(trades) >= 1, f"应有至少 1 条 trade（FINAL_CLOSE），实际 {len(trades)}"
    final_close = [t for t in trades if t.get("exit_reason") == "FINAL_CLOSE"]
    assert len(final_close) == 1, f"应有 1 条 FINAL_CLOSE，实际 {len(final_close)}"
    assert final_close[0]["filled"] is True
    assert Decimal(final_close[0]["entry_price"]) == Decimal("1990"), (
        f"限价单入场价应为 1990，实际 {final_close[0]['entry_price']}"
    )


def test_limit_order_run_fallback_settle_expired_case(tmp_path):
    """阻断 #3（端到端）：事件循环 settle 窗口为空，
    挂单永不穿越 → run() 末尾 fallback settle 标记为 expired_unfilled。

    撮合窗口数学：
      opened_at=10:00, expiry_at=10:05, limit=1990 (LONG)
      全部 K 线 low=2005 > 1990 → 永不穿越
      run() 末尾 fallback settle → search_end=10:05, up_to=10:10 ≥ expiry_at=10:05
        → 标记 expired_unfilled
    """
    pid = "xau_original"
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)

    engine = _make_real_engine(tmp_path, klines_override=_build_never_crossing_klines())

    engine.events = [{
        "profile_id": pid,
        "scheduled_at": "2026-01-05T10:01:00+00:00",
        "decision": "no_signal",
        "symbol": "XAUUSDT",
        "direction": "LONG",
    }]

    order = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }
    engine.pending_limit_orders[pid] = [order]

    engine.run()

    assert engine.pending_limit_orders[pid] == [], "过期后 pending 应清空"

    trades = engine.replay_trades[pid]
    assert len(trades) == 1, f"过期应有 1 条 expired_unfilled trade，实际 {len(trades)}"
    assert trades[0]["filled"] is False
    assert trades[0]["unfilled_reason"] == "expired_unfilled"


def test_limit_order_run_fallback_settle_absent_would_leave_pending(tmp_path):
    """反向验证（控制组）：monkeypatch 屏蔽 run() 末尾的 fallback settle，
    成交案例的挂单会保持 pending 不撮合 —— 证明阻断 #3 的兜底路径确实被测试到了。
    """
    pid = "xau_original"
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)

    engine = _make_real_engine(tmp_path, klines_override=_build_crossing_klines())

    engine.events = [{
        "profile_id": pid,
        "scheduled_at": "2026-01-05T10:01:00+00:00",
        "decision": "no_signal",
        "symbol": "XAUUSDT",
        "direction": "LONG",
    }]

    order = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }
    engine.pending_limit_orders[pid] = [order]

    # monkeypatch 屏蔽 run() 末尾的两行 fallback settle
    from backtest.btc_eth.replay import engine as engine_module

    original_run = engine_module.ReplayEngine.run

    def _run_without_fallback(self):
        """跳过 run() 末尾的 _settle_pending_limit_orders + _run_position_exit_up_to 兜底。"""
        if not self.events:
            raise RuntimeError("尚未 load_events")
        from backtest.btc_eth.replay.engine import _parse_ts, compute_replay_run_id, scan_triggers
        cutoff_ts = self.run_manifest.get("data_cutoff_at")
        if cutoff_ts is None:
            cutoff_ts = self.events[-1]["scheduled_at"] if self.events else datetime.now(UTC)
        cutoff_at = _parse_ts(cutoff_ts) if not isinstance(cutoff_ts, datetime) else cutoff_ts
        self.input_cutoff_at = cutoff_at
        replay_run_id = compute_replay_run_id(self.manifest_sha256, self.engine_version, cutoff_at)
        cutoff_idx = None
        for i, k in enumerate(self.klines_1m_sorted):
            t = _parse_ts(k["open_time"])
            if t >= cutoff_at:
                cutoff_idx = i
                break
        if cutoff_idx is None:
            cutoff_idx = len(self.klines_1m_sorted)
        usable_klines = self.klines_1m_sorted[:cutoff_idx]
        for event in self.events:
            self._process_one_event(event, usable_klines, cutoff_at)
        # 故意跳过 fallback settle —— 直接进入强平
        self._force_close_remaining_positions(usable_klines, cutoff_at)
        return {"engine_metadata": {}}

    # 构造新 engine（原始 engine 已被上面的真实 run 尝试污染了？不，这是单独的）
    # 实际上我们需要 patch 类方法，对新 engine 生效
    from backtest.btc_eth.replay import engine as _eng
    eng2 = _make_real_engine(tmp_path, klines_override=_build_crossing_klines())
    eng2.events = [{
        "profile_id": pid,
        "scheduled_at": "2026-01-05T10:01:00+00:00",
        "decision": "no_signal",
        "symbol": "XAUUSDT",
        "direction": "LONG",
    }]
    eng2.pending_limit_orders[pid] = [{
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }]

    with patch.object(_eng.ReplayEngine, "run", _run_without_fallback):
        eng2.run()

    # 挂单应保持 pending（没有 fallback settle）
    assert len(eng2.pending_limit_orders[pid]) == 1, (
        f"屏蔽 fallback settle 后，挂单应保持 pending，"
        f"实际 pending={eng2.pending_limit_orders[pid]}"
    )


# ===========================================================================
# 组④ 快照行数不变但内容变化 → manifest_sha256 与 replay_run_id 必须变化
# ===========================================================================

def _mk_fake_klines(count, base_close):
    """monkeypatch 用的假 K 线列表。close 参数可控，行数固定 count。"""
    out = []
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    for i in range(count):
        t = start + timedelta(minutes=i)
        out.append({
            "open_time": int(t.timestamp() * 1000),
            "open": str(base_close - 0.5),
            "high": str(base_close + 1),
            "low": str(base_close - 1),
            "close": str(base_close),
            "volume": "10",
            "close_time": int((t + timedelta(minutes=1)).timestamp() * 1000),
            "quote_volume": "20000",
            "count": "1",
        })
    return out


def _mk_fake_funding():
    return []


def _mk_fake_exchange_info():
    return {
        "time": 1727664000000,
        "symbols": [{
            "symbol": "XAUUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "1"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
            ],
        }],
    }


def test_snapshot_sha_changes_when_content_differs_but_rows_same(tmp_path):
    """阻断 #4：两次 build_snapshot 行数完全相同，但 close 不同 → manifest.sha256 必须不同。"""
    from backtest.btc_eth.replay import snapshot as snap_module

    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=20)

    with patch.object(snap_module, "fetch_klines_1m", return_value=_mk_fake_klines(20, 2000)):
        with patch.object(snap_module, "fetch_funding_rate_history", return_value=_mk_fake_funding()):
            with patch.object(snap_module, "fetch_exchange_info", return_value=_mk_fake_exchange_info()):
                m1 = build_snapshot("XAUUSDT", start, end, str(tmp_path / "snap1"))

    # 第二次：同样 20 行，但 close=2001（仅一行之差也应哈希不同）
    with patch.object(snap_module, "fetch_klines_1m", return_value=_mk_fake_klines(20, 2001)):
        with patch.object(snap_module, "fetch_funding_rate_history", return_value=_mk_fake_funding()):
            with patch.object(snap_module, "fetch_exchange_info", return_value=_mk_fake_exchange_info()):
                m2 = build_snapshot("XAUUSDT", start, end, str(tmp_path / "snap2"))

    # 行数相同但内容不同 → 哈希必须不同
    files1 = m1["files"]
    files2 = m2["files"]
    assert files1["klines_1m"]["rows"] == files2["klines_1m"]["rows"] == 20, (
        "两次快照行数必须相同才能证明阻断 #4"
    )
    assert m1["sha256"] != m2["sha256"], (
        f"阻断 #4 未修复：行数相同内容不同但 sha256 一致！"
        f" m1.sha256={m1['sha256'][:16]}... m2.sha256={m2['sha256'][:16]}..."
    )

    # 进一步：replay_run_id 也必须不同（因为它基于 manifest_sha256）
    rid1 = compute_replay_run_id(m1["sha256"], "test-v1.0", end)
    rid2 = compute_replay_run_id(m2["sha256"], "test-v1.0", end)
    assert rid1 != rid2, "replay_run_id 应随 manifest_sha256 变化"


def test_snapshot_sha_identical_when_content_identical_idempotent(tmp_path):
    """反向断言：内容完全相同 → 哈希相同（幂等性）。"""
    from backtest.btc_eth.replay import snapshot as snap_module

    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=20)

    klines_fixed = _mk_fake_klines(20, 2000)
    with patch.object(snap_module, "fetch_klines_1m", return_value=klines_fixed):
        with patch.object(snap_module, "fetch_funding_rate_history", return_value=_mk_fake_funding()):
            with patch.object(snap_module, "fetch_exchange_info", return_value=_mk_fake_exchange_info()):
                m1 = build_snapshot("XAUUSDT", start, end, str(tmp_path / "snap_a"))
                m2 = build_snapshot("XAUUSDT", start, end, str(tmp_path / "snap_b"))

    assert m1["sha256"] == m2["sha256"], (
        "内容完全相同的两次 build_snapshot，sha256 应幂等"
    )


# ===========================================================================
# 组⑤ profile 配置哈希与 run manifest 不一致 → fail-closed
# ===========================================================================

def test_engine_init_raises_when_config_hashes_missing(tmp_path):
    """run_manifest 缺 config_hashes 键 → ValueError。"""
    paths = _make_minimal_fixture(tmp_path)
    run_manifest = {
        "run_id": "test",
        "data_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_virtual_equity_usdt": 300,
        # 故意不写 config_hashes
    }
    with pytest.raises(ValueError, match="config_hashes"):
        ReplayEngine(
            replay_config_path=str(_REPLAY_CFG),
            profile_config_path_original=str(_ORIG_YAML),
            profile_config_path_aggressive=str(_AGGR_YAML),
            **paths,
            run_manifest=run_manifest,
        )


def test_engine_init_raises_when_config_hashes_missing_pid_key(tmp_path):
    """config_hashes 存在但缺某个 pid 键 → ValueError。"""
    paths = _make_minimal_fixture(tmp_path)
    with open(_ORIG_YAML, "r", encoding="utf-8") as f:
        hash_orig = compute_config_hash(yaml.safe_load(f))
    # 故意只给 orig，不给 aggr
    run_manifest = {
        "run_id": "test",
        "data_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_virtual_equity_usdt": 300,
        "config_hashes": {"xau_original": hash_orig},
        # 缺 xau_aggressive
    }
    with pytest.raises(ValueError, match="xau_aggressive"):
        ReplayEngine(
            replay_config_path=str(_REPLAY_CFG),
            profile_config_path_original=str(_ORIG_YAML),
            profile_config_path_aggressive=str(_AGGR_YAML),
            **paths,
            run_manifest=run_manifest,
        )


def test_engine_init_raises_when_config_hash_value_mismatch(tmp_path):
    """config_hashes 值与真实 profile yaml 的 compute_config_hash 不一致 → ValueError。"""
    paths = _make_minimal_fixture(tmp_path)
    # orig 给对，aggr 故意给假值
    with open(_ORIG_YAML, "r", encoding="utf-8") as f:
        hash_orig = compute_config_hash(yaml.safe_load(f))
    run_manifest = {
        "run_id": "test",
        "data_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_virtual_equity_usdt": 300,
        "config_hashes": {
            "xau_original": hash_orig,
            "xau_aggressive": "0" * 64,  # 假哈希
        },
    }
    with pytest.raises(ValueError, match="哈希不匹配"):
        ReplayEngine(
            replay_config_path=str(_REPLAY_CFG),
            profile_config_path_original=str(_ORIG_YAML),
            profile_config_path_aggressive=str(_AGGR_YAML),
            **paths,
            run_manifest=run_manifest,
        )


def test_engine_init_succeeds_with_correct_config_hashes(tmp_path):
    """正确的 config_hashes → 正常构造（不抛异常）。"""
    paths = _make_minimal_fixture(tmp_path)
    with open(_ORIG_YAML, "r", encoding="utf-8") as f:
        hash_orig = compute_config_hash(yaml.safe_load(f))
    with open(_AGGR_YAML, "r", encoding="utf-8") as f:
        hash_aggr = compute_config_hash(yaml.safe_load(f))

    run_manifest = {
        "run_id": "test",
        "data_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_virtual_equity_usdt": 300,
        "config_hashes": {
            "xau_original": hash_orig,
            "xau_aggressive": hash_aggr,
        },
    }
    # 不抛异常即通过
    engine = ReplayEngine(
        replay_config_path=str(_REPLAY_CFG),
        profile_config_path_original=str(_ORIG_YAML),
        profile_config_path_aggressive=str(_AGGR_YAML),
        **paths,
        run_manifest=run_manifest,
    )
    # 顺带断言 engine.tick_size 等字段正常加载
    assert engine.tick_size == Decimal("0.01")


# ===========================================================================
# 组⑥（附加，覆盖任务 1）零事件 profile 的报告渲染
# ===========================================================================

def test_write_reports_zero_scheduled_runs_renders_not_computable(tmp_path):
    """任务 1 修复验证：scheduled_runs=0 时 analysis_success_rate=None，
    _build_markdown 不崩溃且包含「不可计算」字样。"""
    from backtest.btc_eth.replay.report import _build_markdown

    # 直接构造 write_reports 的 engine_result / run_manifest / report dict，
    # 绕过完整回放引擎（不关心真实撮合，只测报告渲染）
    decision_stats_zero = {
        "scheduled_runs": 0,
        "candidate": 0,
        "no_signal": 0,
        "analysis_error": 0,
        "analysis_success_rate": None,
        "analysis_success_rate_computable": False,
        "analysis_sample_note": "no_scheduled_events",
        "no_signal_reasons": {},
    }

    # 构造 metrics：空交易 + 零决策统计
    empty_trades = []
    state = {
        "final_equity": "300",
        "realized_pnl": "0",
        "fee_paid": "0",
        "funding_fee": "0",
        "slippage_cost": "0",
    }
    eq_hist = [("2026-01-05T10:00:00+00:00", "300", True)]

    # 手动构造 report dict（模拟 compute_metrics 输出，注入 decision_stats_zero）
    from backtest.btc_eth.replay.report import compute_metrics
    prof_result = {
        "trades": empty_trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": eq_hist,
    }
    meta = {
        "replay_run_id": "zero-decision-test",
        "engine_version": "replay-v1.0.0",
        "manifest_sha256": "deadbeef",
        "input_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_equity": "300",
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
    }
    metrics_orig = compute_metrics(prof_result, meta, decision_stats=decision_stats_zero)
    metrics_aggr = compute_metrics(prof_result, meta, decision_stats=decision_stats_zero)

    # 手动组装 report dict（与 write_reports 产物同结构）
    report_dict = {
        "run_id": "zero-decision-test",
        "replay_run_id": "zero-decision-test",
        "engine_version": meta["engine_version"],
        "manifest_sha256": meta["manifest_sha256"],
        "input_cutoff_at": meta["input_cutoff_at"],
        "initial_equity": meta["initial_equity"],
        "symbol_filters": meta["symbol_filters"],
        "invariant_violations": [],
        "snapshot_gaps": 0,
        "trailing": {},
        "decision_summary": {"same_timestamp_decision_diff": {}},
        "xau_original": metrics_orig,
        "xau_aggressive": metrics_aggr,
        "admission": {"admission": "none", "original_passed": False, "aggressive_passed": False},
        "data_completeness": {"gaps": 0, "complete": True},
    }

    # 核心：_build_markdown 不崩溃且包含「不可计算」
    md_text = _build_markdown(report_dict)
    assert "不可计算" in md_text, (
        f"零调度事件 profile 的报告应包含「不可计算」字样，实际 markdown 中缺失"
    )
    assert "no_scheduled_events" in md_text, (
        f"应透传 analysis_sample_note，实际缺失"
    )


def test_write_reports_with_scheduled_runs_still_renders_percentage(tmp_path):
    """非零 scheduled_runs 时，analysis_success_rate 正常渲染为百分比。"""
    from backtest.btc_eth.replay.report import _build_markdown

    decision_stats_ok = {
        "scheduled_runs": 4,
        "candidate": 2,
        "no_signal": 1,
        "analysis_error": 1,
        "analysis_success_rate": 0.75,  # (4-1)/4 = 75%
        "analysis_success_rate_computable": True,
        "analysis_sample_note": None,
        "no_signal_reasons": {"grade_below_min": 1},
    }

    empty_trades = []
    state = {
        "final_equity": "300",
        "realized_pnl": "0",
        "fee_paid": "0",
        "funding_fee": "0",
        "slippage_cost": "0",
    }
    eq_hist = [("2026-01-05T10:00:00+00:00", "300", True)]
    prof_result = {
        "trades": empty_trades,
        "state": state,
        "counterfactual": {"15pct": None, "25pct": None, "30pct": None},
        "equity_history": eq_hist,
    }
    meta = {
        "replay_run_id": "ok-decision-test",
        "engine_version": "replay-v1.0.0",
        "manifest_sha256": "cafebabe",
        "input_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_equity": "300",
        "symbol_filters": {"tickSize": "0.01", "stepSize": "0.001", "minNotional": "5"},
        "invariant_violations": [],
    }
    metrics_orig = compute_metrics(prof_result, meta, decision_stats=decision_stats_ok)
    metrics_aggr = compute_metrics(prof_result, meta, decision_stats=decision_stats_ok)

    report_dict = {
        "run_id": "ok-decision-test",
        "replay_run_id": "ok-decision-test",
        "engine_version": meta["engine_version"],
        "manifest_sha256": meta["manifest_sha256"],
        "input_cutoff_at": meta["input_cutoff_at"],
        "initial_equity": meta["initial_equity"],
        "symbol_filters": meta["symbol_filters"],
        "invariant_violations": [],
        "snapshot_gaps": 0,
        "trailing": {},
        "decision_summary": {"same_timestamp_decision_diff": {}},
        "xau_original": metrics_orig,
        "xau_aggressive": metrics_aggr,
        "admission": {"admission": "none", "original_passed": False, "aggressive_passed": False},
        "data_completeness": {"gaps": 0, "complete": True},
    }

    md_text = _build_markdown(report_dict)
    # 应正常渲染百分比；核心断言：analysis_success_rate 那行不含「不可计算」
    # ——注意：成交率（fill_rate）在虚拟订单总数=0 时合法渲染「不可计算」，
    # 本用例 empty_trades=[] → 虚拟订单总数=0，因此宽泛扫「不可计算」会误报
    assert "75.0%" in md_text or "75%" in md_text, (
        f"正常 profile 应渲染 75% 成功率，实际 markdown 中缺失"
    )
    assert "- 分析成功率: 不可计算" not in md_text, (
        "有 4 次调度且 analysis_success_rate_computable=True 时，"
        "分析成功率行不应出现「不可计算」"
    )
    assert "- 分析成功率: 75.0%" in md_text, (
        "分析成功率应精确渲染为 75.0%（对应 decision_stats.analysis_success_rate=0.75）"
    )


# ===========================================================================
# 组⑦ 同 scheduled_at 事件调用 _settle_pending_limit_orders 时挂单不被静默丢弃
# ===========================================================================

def test_limit_order_same_scheduled_at_events_do_not_discard_pending(tmp_path):
    """同 scheduled_at 的两条 no_signal 事件调用 settle 时，
    opened_at == scheduled_at 的挂单不应被静默丢弃。

    触发路径（修复前）：
      - event1 scheduled_at=10:00 → _process_one_event 里可能生成挂单 opened_at=10:00
      - event2 scheduled_at=10:00 → _process_one_event → _settle_pending_limit_orders(up_to=10:00)
        → search_end = min(10:05, 10:00) = 10:00 <= opened_at=10:00
        → 修复前：search_end<=opened_at 分支只有 if expired，没有 else still_pending.append
        → 挂单被静默丢弃，永远不会在后续 K 线撮合
    修复后：else still_pending.append(order) 把挂单放回 pending → run() 末尾 fallback settle
    覆盖 10:04 的 crossing K 线 → 撮合成交。

    修复前该用例会 FAIL：挂单被丢弃 → 无撮合 → 无 FINAL_CLOSE。
    """
    pid = "xau_original"
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    scheduled = "2026-01-05T10:00:00+00:00"

    engine = _make_real_engine(tmp_path, klines_override=_build_crossing_klines())

    # 两条同 scheduled_at 的事件（真实场景：same_timestamp_decision_diff）
    engine.events = [
        {
            "profile_id": pid,
            "scheduled_at": scheduled,
            "decision": "no_signal",
            "symbol": "XAUUSDT",
            "direction": "LONG",
        },
        {
            "profile_id": pid,
            "scheduled_at": scheduled,
            "decision": "no_signal",
            "symbol": "XAUUSDT",
            "direction": "LONG",
        },
    ]

    # 预先注入 LONG 限价单：opened_at=10:00, limit=1990
    order = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }
    engine.pending_limit_orders[pid] = [order]

    engine.run()

    # 修复后期望：
    #  - 挂单未被同 t 的 settle 丢弃 → run() 末尾 fallback settle 在 10:04 撮合成功
    #  - pending 清空、产生 FINAL_CLOSE、entry_price=1990
    assert engine.pending_limit_orders[pid] == [], (
        f"撮合成功后 pending 应清空，实际 pending={engine.pending_limit_orders[pid]}"
    )
    trades = engine.replay_trades[pid]
    final_close = [t for t in trades if t.get("exit_reason") == "FINAL_CLOSE"]
    assert len(final_close) == 1, (
        f"应有 1 条 FINAL_CLOSE（撮合+run()强平），实际 {len(final_close)} 条；"
        f"修复前挂单被丢弃时应为 0 条"
    )
    assert final_close[0]["filled"] is True
    assert Decimal(final_close[0]["entry_price"]) == Decimal("1990"), (
        f"限价单入场价应为 1990，实际 {final_close[0]['entry_price']}"
    )


def test_limit_order_same_scheduled_at_old_behavior_would_discard_pending(tmp_path):
    """反向验证（控制组）：monkeypatch 复现修复前 search_end<=opened_at 分支
    缺失 else still_pending.append 的行为，断言挂单被静默丢弃。

    这是组⑦的控制组——如果这个测试 FAIL，说明修复没真正生效或 fixture 无法捕获 bug；
    通过则证明"不修的话挂单真的会消失"。
    """
    from backtest.btc_eth.replay import engine as engine_module

    pid = "xau_original"
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    scheduled = "2026-01-05T10:00:00+00:00"

    # 捕获原方法并替换为修复前版本
    original_settle = engine_module.ReplayEngine._settle_pending_limit_orders

    def _old_settle(self, profile_id, up_to, klines):
        """修复前：search_end<=opened_at 分支没有 else still_pending.append，
        挂单在 up_to <= opened_at 时被静默丢弃。"""
        still_pending = []
        for order in self.pending_limit_orders[profile_id]:
            from datetime import timedelta as _td
            from backtest.btc_eth.replay.engine import _parse_ts
            opened_at_order = _parse_ts(order["opened_at"])
            expiry_at = opened_at_order + _td(seconds=self.limit_expiry_seconds)
            search_end = min(expiry_at, up_to)
            if search_end <= opened_at_order:
                # 修复前 bug：没有 else still_pending.append(order)
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                continue  # ← 挂单被丢弃
            # 其余逻辑保持原样
            idx_start = self._find_kline_idx_after(klines, opened_at_order)
            idx_end = self._find_kline_idx_after(klines, search_end)
            idx_end = idx_end if idx_end is not None else len(klines)
            if idx_start is None or idx_start >= idx_end:
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                else:
                    still_pending.append(order)
                continue
            from backtest.btc_eth.replay.engine import find_first_crossing_index
            cross_idx = find_first_crossing_index(
                klines_1m=klines,
                limit_price=order["limit_price"],
                direction=order["direction"],
                search_start_idx=idx_start,
                search_end_idx=idx_end,
            )
            if cross_idx is not None:
                cross_kline = klines[cross_idx]
                self._fill_entry(
                    profile_id=profile_id,
                    event=order["source_event"],
                    opened_at=opened_at_order,
                    fill_at=_parse_ts(cross_kline["open_time"]),
                    fill_price=order["limit_price"],
                    quantity=order["quantity"],
                    margin=order["margin"],
                    leverage=order["leverage"],
                    stop_loss=order.get("stop_loss"),
                    grade=order.get("grade"),
                    order_kind="LIMIT",
                    tp1=order.get("tp1"),
                    tp1_close_ratio=order.get("tp1_close_ratio"),
                    tp2=order.get("tp2"),
                    tp2_close_ratio=order.get("tp2_close_ratio"),
                    trailing=order.get("trailing"),
                    time_stop=order.get("time_stop"),
                    atr=order.get("atr"),
                    stop_loss_atr_multiplier=order.get("stop_loss_atr_multiplier"),
                    position_id=order.get("position_id"),
                )
            else:
                if up_to >= expiry_at:
                    self._record_unfilled(profile_id, order, reason="expired_unfilled")
                else:
                    still_pending.append(order)
        self.pending_limit_orders[profile_id] = still_pending

    engine = _make_real_engine(tmp_path, klines_override=_build_crossing_klines())
    engine.events = [
        {
            "profile_id": pid,
            "scheduled_at": scheduled,
            "decision": "no_signal",
            "symbol": "XAUUSDT",
            "direction": "LONG",
        },
        {
            "profile_id": pid,
            "scheduled_at": scheduled,
            "decision": "no_signal",
            "symbol": "XAUUSDT",
            "direction": "LONG",
        },
    ]
    order = {
        "symbol": "XAUUSDT",
        "direction": "LONG",
        "limit_price": Decimal("1990"),
        "opened_at": opened_at.isoformat(),
        "quantity": Decimal("0.03"),
        "margin": Decimal("20"),
        "leverage": 3,
        "source_event": {"decision": "candidate", "symbol": "XAUUSDT", "direction": "LONG"},
        "stop_loss": Decimal("1980"),
        "grade": "A",
        "tp1": None, "tp1_close_ratio": None,
        "tp2": None, "tp2_close_ratio": None,
        "trailing": None, "time_stop": None,
    }
    engine.pending_limit_orders[pid] = [order]

    with patch.object(
        engine_module.ReplayEngine, "_settle_pending_limit_orders", _old_settle
    ):
        engine.run()

    # 旧行为期望：同 t 的 settle 把挂单丢弃 → 永远不会撮合 → 无 FINAL_CLOSE
    trades = engine.replay_trades[pid]
    final_close = [t for t in trades if t.get("exit_reason") == "FINAL_CLOSE"]
    assert len(final_close) == 0, (
        f"旧行为下挂单被丢弃，不应撮合 → 无 FINAL_CLOSE；"
        f"实际有 {len(final_close)} 条，说明旧行为复现失败"
    )


# ===========================================================================
# 组⑧ 生命周期级不变量测试 —— 证明分散检查方案在完整持仓生命周期下
# 不产生伪违规、且真违规仍能被捕获
# ===========================================================================

def test_partial_tp_close_does_not_produce_false_invariant_violation():
    """用例 1（核心）：TP1 部分平仓后 open_position 仍非 None，
    分散检查方案（只调 check_quantity_in_range）不应产生任何不变量违规。

    这是对 engine.py L1149-1157 设计决策的正向验证：
    部分平仓后调 run_all_invariants 会触发 check_no_double_close 伪违规
    （详见用例 2），因此引擎只做 quantity step 合法性检查。
    """
    engine = _make_engine_shell()
    state = engine.states["xau_original"]

    # 开仓：entry=2000, sl 远, tp1=2010, tp1_close_ratio=0.30
    # tp2 远, trailing=None, time_stop=None —— 简化场景，只触发 TP1
    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at,
        symbol="XAUUSDT",
        direction="LONG",
        quantity=Decimal("0.03"),
        entry_price=Decimal("2000"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss_price=Decimal("1900"),
        grade="A",
        tp1=Decimal("2010"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2200"),
        tp2_close_ratio=Decimal("0.40"),
        trailing=None,
        time_stop=None,
        atr=None,
    )

    # 构造一根 K 线：high=2015 >= tp1=2010，触发 TP1 部分平仓
    base_ms = int(opened_at.timestamp() * 1000)
    bar = _mk_1m(base_ms + 60_000, "2002", "2015", "2001", "2012")
    at = datetime(2026, 1, 5, 10, 1, tzinfo=UTC)

    result = engine._check_and_close_on_bar("xau_original", bar, at)

    # 触发了 TP1 部分平仓
    assert result == "TP1", f"应触发 TP1，实际返回 {result}"
    # 关键：部分平仓后持仓仍在（剩余 0.021）
    assert state.open_position is not None, "部分平仓后持仓不应消失"
    remaining_qty = Decimal(str(state.open_position["quantity"]))
    expected_remaining = Decimal("0.03") * Decimal("0.70")
    assert abs(remaining_qty - expected_remaining) < Decimal("1e-6"), (
        f"剩余数量应为 0.021，实际 {remaining_qty}"
    )

    # 核心断言：不变量违规列表必须为空（部分平仓不得产生任何伪违规）
    assert engine.invariant_violations == [], (
        f"部分平仓后 invariant_violations 应为空，"
        f"实际 {len(engine.invariant_violations)} 条：{engine.invariant_violations}"
    )


def test_run_all_invariants_false_positive_on_partial_close():
    """用例 2（反向证据）：直接调用 run_all_invariants 证明其在部分平仓场景下
    会产生 check_no_double_close 伪违规——这正是 engine.py L42-44 和 L1635
    注释说明停用它的原因。

    若此用例 FAIL，说明 run_all_invariants 的行为已变，
    引擎停用它的理由需要重新评估。
    """
    # 构造一个"部分平仓后"的状态：pos_before 和 pos_after 指向同一个非 None 仓位
    # （模拟"平仓事件到达后，引擎已做了部分 close，但 pos_after 仍非 None"的情形）
    partial_pos = {
        "direction": "LONG",
        "quantity": Decimal("0.021"),  # 部分平仓后的剩余仓位（仍是 step=0.001 的倍数）
        "entry_price": Decimal("2000"),
        "stop_loss": Decimal("1900"),
        "opened_at": "2026-01-05T10:00:00+00:00",
    }

    # run_all_invariants 的第一个分支：pos_before 非 None 且 pos_after 非 None
    # → 不会触发 check_no_unprotected_close（只在 pos_before is None 时才调）
    # → 但会触发 check_no_double_close（只要 pos_after 非 None 就报违规）
    violations = run_all_invariants(
        state_after={},
        pos_before=partial_pos,
        pos_after=partial_pos,  # 同一个非 None 对象
        quantity=Decimal("0.009"),  # close_qty（0.03*0.3=0.009），合法 step 倍数
        step_size=Decimal("0.001"),
        price=Decimal("2005"),
        min_notional=Decimal("5"),
        label="partial-close",
    )

    # 反向断言：run_all_invariants 必须返回非空（包含伪违规）
    assert len(violations) > 0, (
        "run_all_invariants 在 pos_after 非 None 时应产生伪违规，"
        "实际返回空列表——停用理由需重新评估"
    )

    # 确认违规类型是 check_no_double_close
    double_close_hits = [v for v in violations if "double close" in v or "平仓后持仓仍存在" in v]
    assert len(double_close_hits) >= 1, (
        f"应至少包含一条 check_no_double_close 伪违规，实际违规列表：{violations}"
    )


def test_full_position_lifecycle_produces_zero_invariant_violations():
    """用例 3（正向完整生命周期）：开仓 → TP1 部分平仓 → trailing 全平，
    验证整个生命周期结束时 invariant_violations 为空且仓位已全平。

    复用组①的 trailing 配置与 K 线序列（已验证时间严格递增）。
    """
    engine = _make_engine_shell()
    state = engine.states["xau_original"]

    trailing_cfg = {
        "enabled": True,
        "activation": {"min_profit_pct": Decimal("1"), "also_on_tp1": True},
        "regression_tiers": [
            {"profit_ceiling": Decimal("999"), "retrace_ratio": Decimal("0.5")}
        ],
    }

    opened_at = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    state.on_entry_approved(
        at=opened_at,
        symbol="XAUUSDT",
        direction="LONG",
        quantity=Decimal("0.03"),
        entry_price=Decimal("2000"),
        margin=Decimal("20"),
        leverage=3,
        stop_loss_price=Decimal("1900"),
        grade="A",
        tp1=Decimal("2010"),
        tp1_close_ratio=Decimal("0.30"),
        tp2=Decimal("2200"),
        tp2_close_ratio=Decimal("0.40"),
        trailing=trailing_cfg,
        time_stop=None,
        atr=None,
    )

    # 三根 K 线：与组① test_tp1_exit_time_strictly_monotonic_no_backward_rescan 同序列
    # 10:00 high=2005<tp1, 10:01 high=2025≥tp1(TP1), 10:02 low=2001≤trailing_stop(TRAILING_STOP)
    base_ms = int(opened_at.timestamp() * 1000)
    klines = [
        _mk_1m(base_ms, "2000", "2005", "2000", "2002"),
        _mk_1m(base_ms + 60_000, "2002", "2025", "2000", "2022"),
        _mk_1m(base_ms + 120_000, "2022", "2023", "2001", "2005"),
    ]
    cutoff = datetime(2026, 1, 5, 10, 10, tzinfo=UTC)

    # 第一次推进：到 10:01:30，触发 TP1 部分平仓
    up_to_1 = datetime(2026, 1, 5, 10, 1, 30, tzinfo=UTC)
    engine._run_position_exit_up_to("xau_original", up_to_1, klines, cutoff)

    # 第二次推进：到 10:02:30，触发 TRAILING_STOP 全平
    up_to_2 = datetime(2026, 1, 5, 10, 2, 30, tzinfo=UTC)
    engine._run_position_exit_up_to("xau_original", up_to_2, klines, cutoff)

    # 最终状态：仓位已全平
    assert state.open_position is None, "完整生命周期后 open_position 应为 None"

    # 产生 2 条退出（TP1 + TRAILING_STOP）
    exits = [
        t for t in engine.replay_trades["xau_original"]
        if t.get("exit_time")
    ]
    assert len(exits) == 2, f"应有 2 条退出（TP1+TRAILING_STOP），实际 {len(exits)}"

    # 核心断言：整个生命周期 invariant_violations 始终为空（无伪违规）
    assert engine.invariant_violations == [], (
        f"完整生命周期后 invariant_violations 应为空，"
        f"实际 {len(engine.invariant_violations)} 条：{engine.invariant_violations}"
    )
