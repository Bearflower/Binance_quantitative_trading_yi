"""确定性回放测试：同输入两次跑逐字节一致；不同 manifest/engine_version → 不同 replay_run_id。"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
import yaml
from shared.config_loader import compute_config_hash

from backtest.btc_eth.replay.engine import (
    ReplayEngine, stable_sort_key, compute_replay_run_id,
)


UTC = timezone.utc


def _make_fixture_dir():
    """在 tmp 下生成一套最小 fixture 目录（1m K 线 / funding / exchangeInfo / manifest / events）。

    manifest 顶层 sha256 必须基于 files.*.sha256 通过共享 helper _compute_manifest_sha256 计算，
    形成自洽的 canonical 口径——新 fail-closed 顶层校验下，手动伪造的 sha 会被 verify_snapshot 拒掉。
    """
    from backtest.btc_eth.replay.snapshot import _compute_manifest_sha256

    d = tempfile.mkdtemp(prefix="xau_replay_fixture_")

    # 1m K 线：100 根，简单上涨趋势
    klines = []
    base_price = 2000.0
    start = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    for i in range(100):
        t = start + timedelta(minutes=i)
        open_t = base_price + i * 0.5
        close_t = open_t + 0.5
        klines.append({
            "open_time": int(t.timestamp() * 1000),
            "open": f"{open_t:.2f}",
            "high": f"{open_t + 2:.2f}",
            "low": f"{open_t - 2:.2f}",
            "close": f"{close_t:.2f}",
            "volume": "10",
            "close_time": int((t + timedelta(minutes=1)).timestamp() * 1000),
            "quote_volume": "20000",
            "count": "1",
        })

    klines_path = os.path.join(d, "xauusdt_1m.jsonl")
    with open(klines_path, "w", encoding="utf-8") as f:
        for k in klines:
            f.write(json.dumps(k) + "\n")

    # funding：空数组（简化）
    funding_path = os.path.join(d, "xauusdt_funding.json")
    with open(funding_path, "w", encoding="utf-8") as f:
        json.dump([], f)

    # exchangeInfo
    ei = {
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
    ei_path = os.path.join(d, "xauusdt_exchange_info.json")
    with open(ei_path, "w", encoding="utf-8") as f:
        json.dump(ei, f)

    # 先用真实文件 SHA 组装 files 字典，再用共享 helper 生成顶层哈希——与 build_snapshot 口径一致
    def _sha_file(p: str) -> str:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    files = {
        "klines_1m": {"path": klines_path, "rows": len(klines), "sha256": _sha_file(klines_path)},
        "funding_rates": {"path": funding_path, "rows": 0, "sha256": _sha_file(funding_path)},
        "exchange_info": {"path": ei_path, "sha256": _sha_file(ei_path)},
    }
    manifest = {
        "symbol": "XAUUSDT",
        "interval": "1m",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=100)).isoformat(),
        "sha256": _compute_manifest_sha256(files),
        "files": files,
        "gaps": [],
    }
    manifest_path = os.path.join(d, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    # 决策事件：仅 no_signal（避免撮合复杂度，专注 engine 确定性）
    events_path = os.path.join(d, "events.jsonl")
    events = []
    for i in range(10):
        sched = start + timedelta(minutes=10 * i)
        for pid in ["xau_original", "xau_aggressive"]:
            events.append({
                "run_id": "xau-shadow-test",
                "profile_id": pid,
                "symbol": "XAUUSDT",
                "scheduled_at": sched.isoformat(),
                "evaluated_at": sched.isoformat(),
                "decision": "no_signal",
                "direction": None,
                "grade": None,
                "score": None,
                "reference_price": None,
                "intended_order_type": None,
                "intended_entry_price": None,
                "stop_loss_rule": None,
                "tp1_rule": None,
                "tp2_rule": None,
                "trailing_rule": None,
                "time_stop_rule": None,
                "feature_snapshot": {},
                "reason_codes": ["grade_below_min"],
                "data_quality_reason": None,
                "config_hash": "abc",
                "git_commit": "def",
                "source_kline_close_times": {"1h": None, "4h": None, "1d": None},
            })
    # 乱序写，验证 stable sort
    import random
    random.shuffle(events)
    with open(events_path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    # run manifest
    # 动态计算真实 profile 配置的哈希（engine 构造函数会校验，fixture 不能用假值）
    _REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    _cfg_orig_path = os.path.join(_REPO_ROOT, "strategies/btc_eth/shadow/config/xau_original.yaml")
    _cfg_aggr_path = os.path.join(_REPO_ROOT, "strategies/btc_eth/shadow/config/xau_aggressive.yaml")
    with open(_cfg_orig_path) as _f:
        _hash_orig = compute_config_hash(yaml.safe_load(_f))
    with open(_cfg_aggr_path) as _f:
        _hash_aggr = compute_config_hash(yaml.safe_load(_f))

    run_manifest = {
        "run_id": "xau-shadow-test",
        "instance_id": "mtcps_xau_shadow",
        "symbol": "XAUUSDT",
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(minutes=100)).isoformat(),
        "status": "active",
        "initial_virtual_equity_usdt": 300,
        "git_commit": "abc123",
        "git_sha": "def456",
        "config_hashes": {"xau_original": _hash_orig, "xau_aggressive": _hash_aggr},
        "data_cutoff_at": (start + timedelta(minutes=100)).isoformat(),
    }
    run_manifest_path = os.path.join(d, "run_manifest.json")
    with open(run_manifest_path, "w", encoding="utf-8") as f:
        json.dump(run_manifest, f)

    return {
        "dir": d,
        "klines_path": klines_path,
        "funding_path": funding_path,
        "ei_path": ei_path,
        "manifest_path": manifest_path,
        "events_path": events_path,
        "run_manifest_path": run_manifest_path,
        "manifest_sha256": manifest["sha256"],
    }


def _run_engine_once(fixture):
    # 从 tests/test_shadow_replay/ → 上溯 2 层到仓库根 Binance_quantitative_trading/
    REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cfg_replay = os.path.join(REPO_ROOT, "backtest", "btc_eth", "replay", "config.yaml")
    cfg_orig = os.path.join(REPO_ROOT, "strategies", "btc_eth", "shadow", "config", "xau_original.yaml")
    cfg_aggr = os.path.join(REPO_ROOT, "strategies", "btc_eth", "shadow", "config", "xau_aggressive.yaml")

    with open(fixture["run_manifest_path"], "r") as f:
        run_manifest = json.load(f)

    engine = ReplayEngine(
        replay_config_path=cfg_replay,
        profile_config_path_original=cfg_orig,
        profile_config_path_aggressive=cfg_aggr,
        klines_1m_path=fixture["klines_path"],
        funding_rates_path=fixture["funding_path"],
        exchange_info_json_path=fixture["ei_path"],
        snapshot_manifest_path=fixture["manifest_path"],
        run_manifest=run_manifest,
    )
    engine.load_events(fixture["events_path"])
    return engine.run()


def test_determinism_same_input():
    """同输入跑两次，replay_run_id 和 equity_history 完全相同。"""
    fixture = _make_fixture_dir()
    r1 = _run_engine_once(fixture)
    r2 = _run_engine_once(fixture)

    assert r1["engine_metadata"]["replay_run_id"] == r2["engine_metadata"]["replay_run_id"]
    assert r1["engine_metadata"]["engine_version"] == r2["engine_metadata"]["engine_version"]
    for pid in ("xau_original", "xau_aggressive"):
        assert r1[pid]["equity_history"] == r2[pid]["equity_history"]
        assert r1[pid]["trades"] == r2[pid]["trades"]


def test_compute_replay_run_id_different_inputs():
    """不同 manifest_sha256 / engine_version / input_cutoff_at → 不同 replay_run_id。"""
    cutoff = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    r1 = compute_replay_run_id("aaa", "v1", cutoff)
    r2 = compute_replay_run_id("bbb", "v1", cutoff)
    r3 = compute_replay_run_id("aaa", "v2", cutoff)
    r4 = compute_replay_run_id("aaa", "v1", cutoff + timedelta(seconds=1))

    assert r1 != r2
    assert r1 != r3
    assert r1 != r4
    # 同一组参数重复调用应相同
    assert r1 == compute_replay_run_id("aaa", "v1", cutoff)


def test_stable_sort_key_orders_deterministically():
    events = []
    for sched_min in [10, 10, 5, 10]:  # 三个相同时间点不同 profile
        events.append({
            "scheduled_at": datetime(2026, 10, 1, 10, sched_min, tzinfo=UTC).isoformat(),
            "profile_id": "xau_original",
            "symbol": "XAUUSDT",
        })
        events.append({
            "scheduled_at": datetime(2026, 10, 1, 10, sched_min, tzinfo=UTC).isoformat(),
            "profile_id": "xau_aggressive",
            "symbol": "XAUUSDT",
        })
    events.sort(key=stable_sort_key)
    # profile_id 字典序："xau_aggressive" < "xau_original"
    assert events[0]["profile_id"] == "xau_aggressive"
    assert events[1]["profile_id"] == "xau_original"


def test_no_random_usage_in_engine():
    """engine.py 和 replay 包内不得有 import random 或 uuid。"""
    import backtest.btc_eth.replay as replay_pkg
    import inspect
    import sys
    modules_to_check = []
    for name in list(sys.modules):
        if name.startswith("backtest.btc_eth.replay") and name != "backtest.btc_eth.replay.__init__":
            mod = sys.modules[name]
            try:
                src = inspect.getsource(mod)
            except TypeError:
                continue
            assert "import random" not in src, f"{name} 有随机来源"
            assert "import uuid" not in src and "from uuid" not in src, f"{name} 用了 uuid"
