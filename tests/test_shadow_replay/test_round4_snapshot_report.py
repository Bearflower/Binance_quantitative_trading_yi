"""
Round 4 回归测试：snapshot 顶层哈希校验 + report 虚拟订单指标。

P1（阻断）：verify_snapshot 必须校验顶层 manifest["sha256"]，
           防止「改文件 + 同步更新嵌套 files.*.sha256 + 保留旧顶层哈希」的篡改路径。
P2：report.compute_metrics 新增 n_filled_orders / n_pending_at_cutoff /
    n_total_virtual_orders / fill_rate / fill_rate_computable，
    _build_markdown 新增虚拟订单数/成交率/截止未到期挂单渲染。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from backtest.btc_eth.replay.snapshot import (
    _compute_manifest_sha256,
    build_snapshot,
    verify_snapshot,
)
from backtest.btc_eth.replay.report import compute_metrics, _build_markdown

UTC = timezone.utc


# ============================================================================
# P1 辅助：复制 round3 的 fake klines/funding/exchange_info 手法，勿 import 那份
# ============================================================================

def _mk_fake_klines(count, base_close):
    """假 K 线列表，close 可控。"""
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
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
            "quantityPrecision": 2,
            "pricePrecision": 2,
        }],
    }


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ============================================================================
# P1 测试
# ============================================================================

def test_compute_manifest_sha256_canonical_consistency():
    """公共 helper 的 canonical 口径必须与 build_snapshot 共用 helper 的实现一致。

    历史：build_snapshot 曾内联哈希计算代码，现已改为统一调用 _compute_manifest_sha256；
    两端共用同一 helper 消除重复实现导致的口径漂移风险。
    """
    files = {
        "klines_1m": {"sha256": "aaa"},
        "funding_rates": {"sha256": "bbb"},
        "exchange_info": {"sha256": "ccc"},
    }
    h = _compute_manifest_sha256(files)
    # 手动算一遍，确认口径：json.dumps + sort_keys + separators=(",",":")
    expected = hashlib.sha256(
        json.dumps(
            {
                "klines_1m": "aaa",
                "funding_rates": "bbb",
                "exchange_info": "ccc",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert h == expected


def test_verify_snapshot_detects_top_hash_tampering(tmp_path):
    """P1 核心：改行情文件 + 同步更新嵌套 sha256 + 保留旧顶层哈希 → files_ok=False。"""
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    klines = _mk_fake_klines(10, 2000)

    with patch(
        "backtest.btc_eth.replay.snapshot.fetch_klines_1m", return_value=klines
    ), patch(
        "backtest.btc_eth.replay.snapshot.fetch_funding_rate_history", return_value=_mk_fake_funding()
    ), patch(
        "backtest.btc_eth.replay.snapshot.fetch_exchange_info", return_value=_mk_fake_exchange_info()
    ):
        manifest = build_snapshot("XAUUSDT", start, start + timedelta(minutes=10), str(tmp_path))

    manifest_path = tmp_path / "manifest.json"

    # 先验证正常情况下是过的
    result_ok = verify_snapshot(str(manifest_path))
    assert result_ok["files_ok"] is True, "正常快照应 files_ok=True"
    assert not any(m.get("file") == "__manifest_sha256__" for m in result_ok["mismatches"])

    # 篡改：改 klines 文件内容（改一根 K 线的 close），
    # 同步更新 manifest["files"]["klines_1m"]["sha256"]（嵌套哈希正确），
    # 但保留旧的顶层 manifest["sha256"]
    # → 逐文件校验会过，但顶层哈希校验必错
    new_close = 9999
    new_klines = _mk_fake_klines(10, new_close)
    klines_path = tmp_path / "xauusdt_1m.jsonl"
    klines_path.write_text(
        "\n".join(json.dumps(k) for k in new_klines), encoding="utf-8"
    )

    # 读 manifest → 更新嵌套 sha256 → 保留旧顶层 sha256 → 写回
    with open(manifest_path, "r", encoding="utf-8") as f:
        tampered_manifest = json.load(f)
    tampered_manifest["files"]["klines_1m"]["sha256"] = _sha(klines_path)
    # 故意不改 tampered_manifest["sha256"] —— 保留旧值

    # 确认嵌套哈希确实是新值（说明逐文件校验会过）
    assert tampered_manifest["files"]["klines_1m"]["sha256"] != manifest["files"]["klines_1m"]["sha256"]

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(tampered_manifest, f, indent=2, ensure_ascii=False)

    # verify_snapshot 必须 fail-closed
    result_fail = verify_snapshot(str(manifest_path))
    assert result_fail["files_ok"] is False, "篡改后 files_ok 必须为 False"

    # mismatches 必须包含 __manifest_sha256__
    top_mismatch = [
        m for m in result_fail["mismatches"] if m.get("file") == "__manifest_sha256__"
    ]
    assert top_mismatch, "mismatches 必须含 __manifest_sha256__"
    assert top_mismatch[0]["expected"] == manifest["sha256"]
    assert top_mismatch[0]["actual"] != manifest["sha256"]


def test_verify_snapshot_idempotent_no_tamper(tmp_path):
    """幂等：内容不变 → files_ok=True，无 __manifest_sha256__ mismatch。"""
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    klines = _mk_fake_klines(5, 2000)

    with patch(
        "backtest.btc_eth.replay.snapshot.fetch_klines_1m", return_value=klines
    ), patch(
        "backtest.btc_eth.replay.snapshot.fetch_funding_rate_history", return_value=_mk_fake_funding()
    ), patch(
        "backtest.btc_eth.replay.snapshot.fetch_exchange_info", return_value=_mk_fake_exchange_info()
    ):
        build_snapshot("XAUUSDT", start, start + timedelta(minutes=5), str(tmp_path))

    result = verify_snapshot(str(tmp_path / "manifest.json"))
    assert result["files_ok"] is True
    assert not any(m.get("file") == "__manifest_sha256__" for m in result["mismatches"])


# ============================================================================
# P2 测试：compute_metrics 虚拟订单指标 + markdown 渲染
# ============================================================================

def _mk_engine_meta(initial_equity="10000"):
    return {
        "initial_equity": initial_equity,
        "invariant_violations": [],
        "engine_version": "test",
        "replay_run_id": "r1",
        "manifest_sha256": "fixture",
        "input_cutoff_at": "2026-01-05T10:10:00+00:00",
        "symbol_filters": {},
    }


def _mk_trades_for_fill_rate():
    """构造 3 条 trade：1 个已成交仓位 + 1 个 expired_unfilled + 1 个 pending_at_cutoff。"""
    entry = "2026-01-05T10:00:00+00:00"
    exit_ = "2026-01-05T10:05:00+00:00"
    return [
        # 已成交（position_id 有值 + filled=True + exit_reason 存在 → 聚合后 n_closed=1）
        {
            "position_id": "pid-1",
            "filled": True,
            "exit_reason": "TP1",
            "entry_time": entry,
            "exit_time": exit_,
            "entry_price": "2000",
            "exit_price": "2010",
            "direction": "LONG",
            "symbol": "XAUUSDT",
            "grade": "A",
            "entry_quantity": "1",
            "quantity": "0.3",
            "net_pnl": "10",
            "realized_pnl": "10",
            "fee": "0.5",
            "funding_fee": "0",
            "slippage": "0.1",
            "mae": "5",
            "mfe": "15",
        },
        # 未成交（expired_unfilled）
        {
            "position_id": None,
            "filled": False,
            "unfilled_reason": "expired_unfilled",
        },
        # 未成交（pending_at_cutoff）—— 截止时仍挂着
        {
            "position_id": None,
            "filled": False,
            "unfilled_reason": "pending_at_cutoff",
        },
    ]


def test_compute_metrics_virtual_order_counts_and_fill_rate():
    """P2 核心：1 已成交 + 1 expired + 1 pending → 总数 3、pending_at_cutoff=1、成交率 1/3。"""
    trades = _mk_trades_for_fill_rate()
    state = {
        "realized_pnl": "10",
        "fee_paid": "0.5",
        "funding_fee": "0",
        "slippage_cost": "0.1",
        "final_equity": "10009.4",
    }
    profile = {
        "trades": trades,
        "state": state,
        "counterfactual": {"15pct": True, "25pct": True, "30pct": True},
        "equity_history": [
            ("2026-01-05T10:00:00+00:00", "10000", False),
            ("2026-01-05T10:05:00+00:00", "10009.4", False),
        ],
    }
    replay_cfg = {
        "tradfi_active_hours_utc": [7, 20],
        "slippage": {"baseline_bps": 2, "tiers_bps": [1, 2, 4]},
        "admission": {"min_closed_trades": 0},
    }
    m = compute_metrics(profile, _mk_engine_meta(), replay_cfg=replay_cfg)

    # 已成交仓位级 = 1
    assert m["n_closed_trades"] == 1
    # 已成交订单数 = n_closed_trades
    assert m["n_filled_orders"] == 1
    # 未成交总数（含 pending_at_cutoff）= 2
    assert m["n_unfilled"] == 2
    # pending_at_cutoff 单独计数 = 1
    assert m["n_pending_at_cutoff"] == 1
    # 虚拟订单总数 = 1 + 2 = 3
    assert m["n_total_virtual_orders"] == 3
    # 成交率 ≈ 1/3（float 精度有限，用 approx 或同数量级 float 比较）
    assert abs(m["fill_rate"] - (1 / 3)) < 1e-15
    assert m["fill_rate_computable"] is True

    # 同时确认 rejection_reasons 把 pending_at_cutoff 计入了
    rej = m["rejection_reason_distribution"]
    assert rej.get("pending_at_cutoff") == 1
    assert rej.get("expired_unfilled") == 1


def test_compute_metrics_fill_rate_zero_total():
    """分母为 0 → fill_rate=None, fill_rate_computable=False。"""
    profile = {
        "trades": [],
        "state": {
            "realized_pnl": "0",
            "fee_paid": "0",
            "funding_fee": "0",
            "slippage_cost": "0",
            "final_equity": "10000",
        },
        "counterfactual": {"15pct": True, "25pct": True, "30pct": True},
        "equity_history": [("2026-01-05T10:00:00+00:00", "10000", False)],
    }
    replay_cfg = {
        "tradfi_active_hours_utc": [7, 20],
        "slippage": {"baseline_bps": 2, "tiers_bps": [2]},
        "admission": {"min_closed_trades": 0},
    }
    m = compute_metrics(profile, _mk_engine_meta(), replay_cfg=replay_cfg)
    assert m["n_total_virtual_orders"] == 0
    assert m["fill_rate"] is None
    assert m["fill_rate_computable"] is False


def test_build_markdown_renders_virtual_orders_and_fill_rate():
    """_build_markdown 必须渲染虚拟订单数/成交率/截止未到期挂单数；None → 不可计算。"""
    trades = _mk_trades_for_fill_rate()
    state = {
        "realized_pnl": "10",
        "fee_paid": "0.5",
        "funding_fee": "0",
        "slippage_cost": "0.1",
        "final_equity": "10009.4",
    }
    profile = {
        "trades": trades,
        "state": state,
        "counterfactual": {"15pct": True, "25pct": True, "30pct": True},
        "equity_history": [
            ("2026-01-05T10:00:00+00:00", "10000", False),
            ("2026-01-05T10:05:00+00:00", "10009.4", False),
        ],
    }
    replay_cfg = {
        "tradfi_active_hours_utc": [7, 20],
        "slippage": {"baseline_bps": 2, "tiers_bps": [2]},
        "admission": {"min_closed_trades": 0},
    }
    m_orig = compute_metrics(profile, _mk_engine_meta(), replay_cfg=replay_cfg)

    report = {
        "run_id": "test-run",
        "replay_run_id": "r1",
        "engine_version": "test",
        "manifest_sha256": "fixture",
        "input_cutoff_at": "2026-01-05T10:10:00+00:00",
        "initial_equity": "10000",
        "snapshot_gaps": 0,
        "invariant_violations": [],
        "xau_original": m_orig,
        "xau_aggressive": m_orig,
        "admission": {"admission": "original", "original_passed": True, "aggressive_passed": True},
    }

    md = _build_markdown(report)

    # 必须出现这三个指标行
    assert "虚拟订单总数" in md
    assert "成交率" in md
    assert "截止未到期挂单数" in md
    # 数值：1/3 ≈ 33.3%
    assert "33.3%" in md
    assert "3 |" in md  # 总数 3
    assert "1 |" in md  # pending_at_cutoff 1

    # 不可计算分支：构造 fill_rate=None 的场景
    profile_empty = {
        "trades": [],
        "state": {
            "realized_pnl": "0",
            "fee_paid": "0",
            "funding_fee": "0",
            "slippage_cost": "0",
            "final_equity": "10000",
        },
        "counterfactual": {"15pct": True, "25pct": True, "30pct": True},
        "equity_history": [("2026-01-05T10:00:00+00:00", "10000", False)],
    }
    m_empty = compute_metrics(profile_empty, _mk_engine_meta(), replay_cfg=replay_cfg)
    report_empty = dict(report, xau_original=m_empty, xau_aggressive=m_empty)
    md_empty = _build_markdown(report_empty)
    assert "不可计算" in md_empty
