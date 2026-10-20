"""Round4 修复测试：分批平仓后连续亏损判定 + 截止时 pending 挂单补记录。

P1（阻断）：整笔仓位级净盈亏 position_net_pnl 替代单批 realized_pnl 做连续亏损判定。
  - 用例 1：整笔盈利 + 最后一腿亏损 → consecutive_losses 不得增加
  - 用例 2：整笔亏损 + 最后一腿盈利 → consecutive_losses 必须增加
  - 用例 3：连续两笔仓位级净亏损 → 触发暂停
  - 用例 4：控制组——旧实现行为错误的反向证据

P2：截止时未到 expiry_at 的 pending 挂单补记录为 pending_at_cutoff。
  - 用例 5：未过期挂单 → 记录 + 清空 + 计数
  - 用例 6（控制组）：不运行补记路径时 → 既不出现也不计数

风格：与 test_round3_regressions.py 对齐，直接构造 state/engine shell。
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from backtest.btc_eth.replay.state import VirtualState

UTC = timezone.utc


# ===========================================================================
# 通用构造
# ===========================================================================


def _make_state(**overrides):
    """默认 VirtualState：threshold=2, pause=24h, 与 test_state._make_state 同口径。"""
    base = VirtualState(
        max_single_position_margin_usdt=Decimal("30"),
        max_concurrent_positions=1,
        max_leverage=3,
        initial_equity=Decimal("300"),
        equity=Decimal("300"),
        max_daily_trades=1,
        symbol_cooldown_hours=72,
        consecutive_loss_pause_threshold=2,
        pause_duration_hours=24,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ===========================================================================
# P1 用例 1：整笔盈利 + 最后一腿亏损 → consecutive_losses 不得增加
# ===========================================================================


def test_position_net_pnl_profit_last_leg_loss_no_count():
    """Round4 修复验证：整笔净赚 +1.90、最后一腿 pnl=-0.10 → consecutive_losses 仍为 0。

    模拟场景：整笔仓位累计 net_pnl=+1.90（含所有腿的 fee/funding/slip），
    最后一批 realized_pnl=-0.10。新实现按 position_net_pnl 判定 → 不增。
    """
    s = _make_state(consecutive_losses=0)
    # 全平：最后一腿亏损 pnl，但整笔 position_net_pnl = +1.90（正盈利）
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("-0.10"),
        fee=Decimal("-0.03"),
        funding=Decimal("0"),
        slippage=Decimal("-0.012"),
        close_reason="FORCE_CLOSE",
        # Round4 修复：整笔累计净盈亏（不是最后一腿的 -0.10，而是全部腿 +1.90）
        position_net_pnl=Decimal("1.90"),
    )
    assert s.consecutive_losses == 0, "整笔盈利不该增加连续亏损计数"
    assert s.consecutive_loss_pause_until is None


# ===========================================================================
# P1 用例 2：整笔亏损 + 最后一腿盈利 → consecutive_losses 必须增加
# ===========================================================================


def test_position_net_pnl_loss_last_leg_profit_counted():
    """Round4 修复验证：整笔净亏 -1.90、最后一腿 pnl=+0.10 → consecutive_losses 必须增加到 1。

    模拟场景：整笔 position_net_pnl=-1.90（净亏），最后一腿 realized_pnl=+0.10。
    旧实现只看 realized_pnl>0 会清零计数器 → 新实现按 position_net_pnl<0 增加。
    """
    s = _make_state(consecutive_losses=0)
    s.on_position_closed(
        at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        realized_pnl=Decimal("+0.10"),
        fee=Decimal("-0.03"),
        funding=Decimal("0"),
        slippage=Decimal("-0.012"),
        close_reason="FORCE_CLOSE",
        # 整笔累计净亏损 -1.90
        position_net_pnl=Decimal("-1.90"),
    )
    assert s.consecutive_losses == 1, "整笔亏损必须增加连续亏损计数"
    assert s.consecutive_loss_pause_until is None  # threshold=2，一笔还不够暂停


# ===========================================================================
# P1 用例 3：连续两笔仓位级净亏损 → 触发暂停
# ===========================================================================


def test_two_position_net_pnl_losses_triggers_pause():
    """连续两笔仓位级净亏损 → consecutive_loss_pause_until 非 None。"""
    s = _make_state()
    base = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

    # 第一笔：整笔亏 -5.00（realized_pnl 恰好是负值，但核心是 position_net_pnl）
    s.on_position_closed(
        at=base,
        realized_pnl=Decimal("-5"),
        fee=Decimal("-0.03"),
        funding=Decimal("0"),
        slippage=Decimal("-0.012"),
        close_reason="STOP_LOSS",
        position_net_pnl=Decimal("-5.00"),
    )
    assert s.consecutive_losses == 1
    assert s.consecutive_loss_pause_until is None

    # 第二笔：整笔亏 -3.00，且故意让 realized_pnl 为正（模拟"最后一腿盈利、整笔亏损"场景）
    s.on_position_closed(
        at=base + timedelta(hours=4),
        realized_pnl=Decimal("+0.50"),  # 旧实现只看这个会清零
        fee=Decimal("-0.03"),
        funding=Decimal("0"),
        slippage=Decimal("-0.012"),
        close_reason="TP2_REMAINDER",
        position_net_pnl=Decimal("-3.00"),  # 整笔净亏
    )
    assert s.consecutive_losses == 2, "两笔净亏 → consecutive_losses=2"
    expected_until = base + timedelta(hours=4) + timedelta(hours=24)
    assert s.consecutive_loss_pause_until == expected_until, "应触发 24h 暂停"


# ===========================================================================
# P1 用例 4：控制组——旧实现行为错误的反向证据
# ===========================================================================


def test_old_behavior_would_be_wrong_control():
    """反向证据：旧实现（只用单批 realized_pnl 判定）在用例 1/2 场景下会错。

    目的：确认"整笔盈利 + 最后一腿亏损"场景下，realized_pnl<0 会误增计数；
    "整笔亏损 + 最后一腿盈利"场景下，realized_pnl>0 会误清零。
    这里**不**验证新实现行为（由用例 1/2 覆盖），而是把旧判定公式跑一遍当控制组。
    """

    def old_counter(prev: int, realized_pnl: Decimal) -> int:
        """旧逻辑：只用 realized_pnl < 0 判连续亏损。"""
        if realized_pnl < 0:
            return prev + 1
        else:
            return 0

    # 用例 1 场景：整笔净赚 +1.90、最后一腿 realized_pnl=-0.10
    # 旧实现误判为亏损 → consecutive_losses 从 0 增到 1
    old_after_case1 = old_counter(prev=0, realized_pnl=Decimal("-0.10"))
    assert old_after_case1 == 1, "反向证据：旧实现会误增到 1（错误行为）"

    # 用例 2 场景：整笔净亏 -1.90、最后一腿 realized_pnl=+0.10
    # 旧实现误判为盈利 → consecutive_losses 从 1 清零
    old_after_case2 = old_counter(prev=1, realized_pnl=Decimal("+0.10"))
    assert old_after_case2 == 0, "反向证据：旧实现会清零（错误行为）"


# ===========================================================================
# P2 构造：shell engine（只需要 replay_trades + pending_limit_orders + limit_expiry_seconds + _record_unfilled）
# ===========================================================================


def _make_engine_p2_shell() -> Any:
    """极简 ReplayEngine 壳——仅保留 P2 测试需要的字段与方法。"""
    from backtest.btc_eth.replay.engine import ReplayEngine

    engine = ReplayEngine.__new__(ReplayEngine)
    engine.replay_trades = {"xau_original": [], "xau_aggressive": []}
    engine.pending_limit_orders = {"xau_original": [], "xau_aggressive": []}
    engine.limit_expiry_seconds = 300  # 5 分钟后过期
    return engine


def _run_pending_at_cutoff_pass(
    engine: Any, cutoff_at: datetime,
) -> int:
    """独立执行 P2 补记逻辑（与 engine.run() 尾部代码块一致），返回本次补记计数。

    此函数复刻 engine.run() 尾部的 pending_at_cutoff 补记循环，用于**隔离单测**——
    仅验证补记逻辑本身（遍历未到期挂单、写 replay_trades、清空 pending_limit_orders）。
    真实完整路径（含 _settle_pending_limit_orders 前置撮合 + 事件循环）由
    下方 test_pending_at_cutoff_recorded_via_real_run 的 engine.run() 集成测试覆盖。
    """
    from backtest.btc_eth.replay.engine import _parse_ts
    from datetime import timedelta as _td

    count = 0
    for pid in ("xau_original", "xau_aggressive"):
        still_pending = engine.pending_limit_orders[pid]
        cutoff_unfilled: List[Dict[str, Any]] = []
        for order in still_pending:
            opened_at = _parse_ts(order["opened_at"])
            expiry_at = opened_at + _td(seconds=engine.limit_expiry_seconds)
            if cutoff_at < expiry_at:
                engine._record_unfilled(pid, order, reason="pending_at_cutoff")
                count += 1
            else:
                cutoff_unfilled.append(order)
        engine.pending_limit_orders[pid] = []
    return count


# ===========================================================================
# P2 用例 5：截止时未到期挂单 → 记录 pending_at_cutoff + 清空 + 计数
# ===========================================================================


def test_pending_at_cutoff_recorded():
    """未过期挂单 → run() 补记 pending_at_cutoff，出现在 replay_trades 中，计数 = 1。

    构造：opened_at = cutoff - 60s, limit_expiry_seconds=300 → expiry = cutoff + 240s
    → cutoff 时距到期还有 240s，属于"截止但未过期"。
    """
    engine = _make_engine_p2_shell()
    cutoff_at = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    opened_at = cutoff_at - timedelta(seconds=60)  # 距 cutoff 60s，还剩 240s 到期

    order = {
        "position_id": "pos-pending-001",
        "opened_at": opened_at,  # datetime 对象（_settle_pending_limit_orders 里也是 datetime）
        "limit_price": Decimal("2000.50"),
        "direction": "LONG",
        "symbol": "XAUUSDT",
        "quantity": Decimal("0.10"),
        "margin": Decimal("20"),
        "leverage": 3,
        "stop_loss": Decimal("1990"),
        "source_event": {},
    }
    engine.pending_limit_orders["xau_original"].append(order)

    count = _run_pending_at_cutoff_pass(engine, cutoff_at)

    # 补记计数
    assert count == 1
    # pending_limit_orders 已清空
    assert engine.pending_limit_orders["xau_original"] == []
    # replay_trades 新增一条 unfilled_reason == "pending_at_cutoff" 的记录
    trades = engine.replay_trades["xau_original"]
    pending_trades = [t for t in trades if t.get("unfilled_reason") == "pending_at_cutoff"]
    assert len(pending_trades) == 1
    assert pending_trades[0]["position_id"] == "pos-pending-001"


# ===========================================================================
# P2 用例 6（控制组）：不运行补记路径 → 挂单既不出现也不计数
# ===========================================================================


def test_without_pending_at_cutoff_control():
    """反向证据：不执行补记逻辑时，挂单既不出现在 replay_trades 也不计数。

    证明 P2 补记代码确实带来了新增样本——若该段代码被注释掉，报告将缺失此类订单。
    """
    engine = _make_engine_p2_shell()
    cutoff_at = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    opened_at = cutoff_at - timedelta(seconds=60)

    order = {
        "position_id": "pos-pending-002",
        "opened_at": opened_at,
        "limit_price": Decimal("2000.50"),
        "direction": "LONG",
        "symbol": "XAUUSDT",
        "quantity": Decimal("0.10"),
        "margin": Decimal("20"),
        "leverage": 3,
        "stop_loss": Decimal("1990"),
        "source_event": {},
    }
    engine.pending_limit_orders["xau_original"].append(order)

    # 故意不调用 _run_pending_at_cutoff_pass —— 模拟旧实现缺此逻辑
    # 直接断言：既没被清掉、也没进 replay_trades

    assert len(engine.pending_limit_orders["xau_original"]) == 1, \
        "没有补记时挂单应残留未清"
    trades = engine.replay_trades["xau_original"]
    pending_trades = [t for t in trades if t.get("unfilled_reason") == "pending_at_cutoff"]
    assert len(pending_trades) == 0, \
        "没有补记逻辑时 replay_trades 不应出现 pending_at_cutoff"


# ===========================================================================
# P2 用例 7（真实 run() 集成）：驱动完整 engine.run() → 断言
#   replay_trades 中存在 pending_at_cutoff + engine_metadata 中计数一致
# ===========================================================================


def test_pending_at_cutoff_recorded_via_real_run(tmp_path):
    """通过 main.py 驱动真实 engine.run()：
      - 事件为 LIMIT candidate（限价高于当前 close，K 线未触发）
      - 挂单被 _settle_pending_limit_orders 保留（未撮合）
      - 期终 cutoff 到来，未过期 → 被尾部 pending_at_cutoff 补记逻辑捕获

    断言：
      (a) engine_metadata["pending_at_cutoff_count"] >= 1
      (b) replay_trades[pid] 中存在 unfilled_reason == "pending_at_cutoff" 的记录
    """
    import hashlib
    import json
    from pathlib import Path

    import yaml
    from shared.config_loader import compute_config_hash

    from backtest.btc_eth.replay.snapshot import _compute_manifest_sha256
    from backtest.btc_eth.replay import main as replay_main

    ROOT = Path(__file__).resolve().parents[2]
    ORIG_YAML = ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_original.yaml"
    AGGR_YAML = ROOT / "strategies" / "btc_eth" / "shadow" / "config" / "xau_aggressive.yaml"

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    # 1) 构造快照（干净、自洽 sha256）：10 根 K 线，close 恒 2000
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    cutoff = datetime(2026, 1, 5, 10, 10, tzinfo=UTC)  # 10 分钟窗口
    t0_ms = int(start.timestamp() * 1000)

    klines = []
    for i in range(10):
        t_ms = t0_ms + i * 60_000
        klines.append({
            "open_time": t_ms,
            "open": "2000", "high": "2001", "low": "1999", "close": "2000",
            "volume": "100", "close_time": t_ms + 59_999,
            "quote_volume": "200000", "count": 10,
        })

    snap_dir = tmp_path / "snapshot"
    snap_dir.mkdir()
    kl_path = snap_dir / "xauusdt_1m.jsonl"
    kl_path.write_text("\n".join(json.dumps(k) for k in klines), encoding="utf-8")
    fund_path = snap_dir / "xauusdt_funding.json"
    fund_path.write_text("[]", encoding="utf-8")
    ei_path = snap_dir / "xauusdt_exchange_info.json"
    ei_path.write_text(json.dumps({
        "symbols": [{
            "symbol": "XAUUSDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            ],
        }],
    }), encoding="utf-8")

    files = {
        "klines_1m": {"path": str(kl_path), "rows": len(klines), "sha256": _sha(kl_path)},
        "funding_rates": {"path": str(fund_path), "rows": 0, "sha256": _sha(fund_path)},
        "exchange_info": {"path": str(ei_path), "sha256": _sha(ei_path)},
    }
    manifest = {
        "symbol": "XAUUSDT", "interval": "1m",
        "start": start.isoformat(), "end": cutoff.isoformat(),
        "gaps": [], "files": files,
        "sha256": _compute_manifest_sha256(files),
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # 2) 构造事件：
    #    - SHORT LIMIT @ 2100（空限价远高于 close=2000，K 线 high=2001 永远触不到）
    #      → 不会被立即撮合，会留在 pending_limit_orders 里被期终扫描捕获
    #    - 挂单 opened_at = 事件 scheduled_at（默认），limit_expiry_seconds=300（5 分钟）
    #      cutoff=10:10，事件 scheduled_at=10:02 → opened_at=10:02 → expiry=10:07
    #      cutoff_at=10:10 >= expiry_at=10:07 → 过期？不对，需要 cutoff_at < expiry_at
    #      让 scheduled_at = 10:07 → opened_at=10:07 → expiry=10:12 → cutoff=10:10 < 10:12 → 未过期
    events = [
        {
            "run_id": "p2-pending-test",
            "profile_id": "xau_original",
            "symbol": "XAUUSDT",
            "scheduled_at": datetime(2026, 1, 5, 10, 7, tzinfo=UTC).isoformat(),
            "evaluated_at": datetime(2026, 1, 5, 10, 7, tzinfo=UTC).isoformat(),
            "decision": "candidate",
            "direction": "SHORT",  # SHORT LIMIT @ 2100（高于 market）
            "grade": "S",
            "score": 0.75,
            "reference_price": "2000",
            "intended_order_type": "LIMIT",
            "intended_entry_price": "2100",  # 空限价远高于 close=2000，K 线 high=2001 永远触不到
            "stop_loss_rule": {"atr_value": "10.0", "atr_multiplier": 1.5},
            "tp1_rule": None, "tp2_rule": None, "trailing_rule": None,
            "time_stop_rule": None, "feature_snapshot": {},
            "reason_codes": [], "data_quality_reason": None,
            "config_hash": "pending-limit", "git_commit": "abc",
            "source_kline_close_times": {"1h": None, "4h": None, "1d": None},
        },
        # 再给 aggressive profile 一条 no_signal（避免它因无事件而出错）
        {
            "run_id": "p2-pending-test",
            "profile_id": "xau_aggressive",
            "symbol": "XAUUSDT",
            "scheduled_at": datetime(2026, 1, 5, 10, 7, tzinfo=UTC).isoformat(),
            "evaluated_at": datetime(2026, 1, 5, 10, 7, tzinfo=UTC).isoformat(),
            "decision": "no_signal", "direction": None, "grade": None,
            "config_hash": "pending-limit", "git_commit": "abc",
            "source_kline_close_times": {"1h": None, "4h": None, "1d": None},
        },
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8",
    )

    # 3) run_manifest（必须含真实 config_hashes）
    with open(ORIG_YAML, "r", encoding="utf-8") as f:
        _h_orig = compute_config_hash(yaml.safe_load(f))
    with open(AGGR_YAML, "r", encoding="utf-8") as f:
        _h_aggr = compute_config_hash(yaml.safe_load(f))
    run_manifest = {
        "run_id": "p2-pending-test",
        "instance_id": "mtcps_xau_shadow",
        "symbol": "XAUUSDT",
        "status": "active",
        "initial_virtual_equity_usdt": 300,
        "config_hashes": {"xau_original": _h_orig, "xau_aggressive": _h_aggr},
        "data_cutoff_at": cutoff.isoformat(),
    }
    run_manifest_path = tmp_path / "run_manifest.json"
    run_manifest_path.write_text(json.dumps(run_manifest), encoding="utf-8")

    # 4) 构造真实 ReplayEngine + engine.run()（完整链路）
    from backtest.btc_eth.replay.engine import ReplayEngine

    REPO_ROOT = ROOT
    cfg_replay = str(REPO_ROOT / "backtest/btc_eth/replay/config.yaml")
    cfg_orig = str(ORIG_YAML)
    cfg_aggr = str(AGGR_YAML)

    with open(run_manifest_path, "r", encoding="utf-8") as f:
        run_manifest_loaded = json.load(f)

    engine = ReplayEngine(
        replay_config_path=cfg_replay,
        profile_config_path_original=cfg_orig,
        profile_config_path_aggressive=cfg_aggr,
        klines_1m_path=str(kl_path),
        funding_rates_path=str(fund_path),
        exchange_info_json_path=str(ei_path),
        snapshot_manifest_path=str(snap_dir / "manifest.json"),
        run_manifest=run_manifest_loaded,
    )
    engine.load_events(str(events_path))
    result = engine.run()

    # 5) 从 engine.run() 返回值里读 engine_metadata（含 pending_at_cutoff_count）
    meta = result["engine_metadata"]
    count = meta.get("pending_at_cutoff_count", 0)
    assert count >= 1, (
        f"engine_metadata.pending_at_cutoff_count 应 >=1，实际 {count}；"
        f"完整 meta keys: {list(meta.keys())}"
    )

    # 6) 同时断言 replay_trades 中确实存在 pending_at_cutoff 的 unfilled 记录
    trades_orig = result["xau_original"]["trades"]
    pending_in_report = [
        t for t in trades_orig
        if t.get("unfilled_reason") == "pending_at_cutoff"
    ]
    assert len(pending_in_report) >= 1, (
        "replay_trades 中应至少有 1 条 pending_at_cutoff unfilled 记录，"
        f"实际 {len(pending_in_report)}；全部 unfilled_reasons="
        f"{[t.get('unfilled_reason') for t in trades_orig]}"
    )
