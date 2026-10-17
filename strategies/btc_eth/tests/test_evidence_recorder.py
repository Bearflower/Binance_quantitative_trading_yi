"""
DecisionEvidenceRecorder 离线测试（T5/T6）。

核心覆盖：
  T5  enabled=False 时 record 空跑、不调用 sink；
      accepted 与 rejected 两种 result 分别得到正确的
      accepted/direction/entry_mode/rejection_reason。
  T6  sink.upsert_evidence 抛异常时 recorder 吞掉、
      result 内容不变（无副作用）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from strategies.btc_eth.evidence_recorder import (
    DecisionEvidenceRecorder,
    _hourly_anchor_ms,
    _resolve_account_id,
    _safe_float,
    _build_entry_mode,
    _jsonable_feature_value,
    _normalize_decision_time,
)


# ── Fake sink：记录调用次数 + payload ──────────────────────────

class FakeSink:
    def __init__(self, boom: bool = False) -> None:
        self.calls: int = 0
        self.payloads: List[Dict[str, Any]] = []
        self.boom = boom

    async def upsert_evidence(self, payload: Dict[str, Any]) -> None:
        self.calls += 1
        self.payloads.append(payload)
        if self.boom:
            raise RuntimeError("sink boom")


# ── 构造 result ────────────────────────────────────────────────

def _accepted_result() -> Dict[str, Any]:
    """analyze() 返回的接受分支（含 direction/market_state）。"""
    return {
        "symbol": "BTCUSDT",
        "score": 82,
        "grade": "A",
        "direction": "LONG",
        "market_state": "STRONG_TREND",
        "entry_price": 50000.50,
        "initial_stop_loss": 49250.0,
        "atr": 750.50,
        "leverage": 5,
        "position_size_usdt": 300.0,
        "quantity": 0.006,
        "position_ratio": 0.6,
        "price_change_1h": 2.5,
        "tp1_price": 51500.0,
        "tp2_price": 53000.0,
        "timestamp": 1729000000,  # 秒级时间戳
    }


def _rejected_result() -> Dict[str, Any]:
    """analyze() 返回的拒绝分支（无 direction）。"""
    return {
        "symbol": "BTCUSDT",
        "score": 62,
        "grade": "C",
        "reason": "ADX 低于阈值 25",
    }


# ── 基础 fixtures ──────────────────────────────────────────────

def _config(enabled: bool = True, account_id: str = "") -> Dict[str, Any]:
    return {
        "decision_evidence": {
            "enabled": enabled,
            "account_id": account_id,
            "db_timeout_seconds": 3.0,
        },
    }


# ══════════════════════════════════════════════════════════════════
# T5：enabled=False → recorder 空跑、不调用 sink
# ══════════════════════════════════════════════════════════════════

def test_disabled_does_not_call_sink():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=False))
    asyncio.run(rec.record(_accepted_result(), "BTCUSDT"))
    assert sink.calls == 0


def test_none_sink_does_not_call():
    """sink=None 时（即使 enabled=True）record 直接返回，异常都不抛。"""
    rec = DecisionEvidenceRecorder(sink=None, config=_config(enabled=True))
    asyncio.run(rec.record(_accepted_result(), "BTCUSDT"))  # 不应抛，这就是实质断言


# ══════════════════════════════════════════════════════════════════
# T5：accepted result 的 accepted/direction/entry_mode/rejection_reason
# ══════════════════════════════════════════════════════════════════

def test_accepted_result_captures_correct_fields():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = _accepted_result()
    original_result = dict(result)
    asyncio.run(rec.record(result, "BTCUSDT"))

    assert sink.calls == 1
    payload = sink.payloads[0]

    # 决策级字段
    assert payload["direction"] == "LONG"
    assert payload["accepted"] is True
    assert payload["rejection_reason"] is None
    assert payload["entry_mode"] == "trend"  # STRONG_TREND → trend 映射
    assert payload["symbol"] == "BTCUSDT"
    assert payload["account_id"] == "default"  # account_id 空 → env 空 → default
    assert payload["strategy_id"] == "btc_eth"

    # 原 result 不变
    assert result == original_result


def test_accepted_ranging_market_state():
    """RANGING → entry_mode='ranging'。"""
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = _accepted_result()
    result["market_state"] = "RANGING"
    asyncio.run(rec.record(result, "BTCUSDT"))
    assert sink.payloads[0]["entry_mode"] == "ranging"


# ══════════════════════════════════════════════════════════════════
# T5：rejected result 的正确字段
# ══════════════════════════════════════════════════════════════════

def test_rejected_result_captures_rejection_reason():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = _rejected_result()
    asyncio.run(rec.record(result, "BTCUSDT"))

    assert sink.calls == 1
    payload = sink.payloads[0]
    assert payload["direction"] == "FLAT"          # 无 direction → FLAT
    assert payload["accepted"] is False
    assert payload["rejection_reason"] == "ADX 低于阈值 25"
    assert payload["entry_mode"] == "unknown"     # rejected → unknown


# ══════════════════════════════════════════════════════════════════
# T6：sink.upsert_evidence 抛异常 → recorder 吞掉、result 不变
# ══════════════════════════════════════════════════════════════════

def test_sink_exception_is_swallowed_result_unchanged():
    sink = FakeSink(boom=True)
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = _accepted_result()
    original = dict(result)

    # 不应抛异常
    asyncio.run(rec.record(result, "BTCUSDT"))
    # result 不应被修改（recorder 只读）
    assert result == original


# ══════════════════════════════════════════════════════════════════
# 特征提取正确性（accepted/rejected 分别有不同特征）
# ══════════════════════════════════════════════════════════════════

def test_accepted_result_has_features():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    asyncio.run(rec.record(_accepted_result(), "BTCUSDT"))

    payload = sink.payloads[0]
    features = {f["name"]: f for f in payload.get("features", [])}
    # accepted result 里这些 key 都存在，应都被提取
    assert "score" in features
    assert features["score"]["value"] == 82
    assert features["grade"]["value"] == "A"
    assert features["market_state"]["value"] == "STRONG_TREND"
    assert features["entry_price"]["value"] == 50000.50
    assert features["atr"]["value"] == 750.50


def test_rejected_result_fewer_features():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    asyncio.run(rec.record(_rejected_result(), "BTCUSDT"))

    payload = sink.payloads[0]
    features = {f["name"]: f for f in payload.get("features", [])}
    # rejected result 只有 score / grade（其他缺失）
    assert "score" in features
    assert "grade" in features
    assert "entry_price" not in features  # rejected 没有
    assert "market_state" not in features


# ══════════════════════════════════════════════════════════════════
# account_id 三级兜底 & cycle_anchor 取整（纯函数单元）
# ══════════════════════════════════════════════════════════════════

def test_resolve_account_id_config_priority(monkeypatch):
    """config.account_id 优先于 env。"""
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "env_account")
    cfg = _config(account_id="cfg_account")
    assert _resolve_account_id(cfg) == "cfg_account"


def test_resolve_account_id_env_fallback(monkeypatch):
    """config 空 → env → default。"""
    monkeypatch.setenv("TRADE_ACCOUNT_ID", "env_account")
    assert _resolve_account_id(_config(account_id="")) == "env_account"


def test_resolve_account_id_default_fallback(monkeypatch):
    monkeypatch.delenv("TRADE_ACCOUNT_ID", raising=False)
    assert _resolve_account_id(_config(account_id="")) == "default"


def test_hourly_anchor_ms_int():
    """毫秒输入直接取整。"""
    ts = 1729000000000   # 2024-10-15 08:26:40 UTC 的毫秒
    anchor = _hourly_anchor_ms(ts)
    assert anchor == 1728997200000  # 向下取整到 08:00 UTC（= 1729000000000 // 3600000 * 3600000）


def test_hourly_anchor_ms_datetime():
    """datetime 输入：先转毫秒再取整。"""
    dt = datetime(2024, 10, 15, 8, 30, 0, tzinfo=timezone.utc)
    anchor = _hourly_anchor_ms(dt)
    expected_hour = datetime(2024, 10, 15, 8, 0, 0, tzinfo=timezone.utc)
    assert anchor == int(expected_hour.timestamp() * 1000)


def test_safe_float_variants():
    assert _safe_float("123.45") == 123.45
    assert _safe_float(42) == 42.0
    assert _safe_float(None) is None
    assert _safe_float("not_a_number") is None


def test_build_entry_mode():
    assert _build_entry_mode({"direction": "LONG", "market_state": "STRONG_TREND"}, True) == "trend"
    assert _build_entry_mode({"market_state": "RANGING"}, True) == "ranging"
    assert _build_entry_mode({"market_state": "UNKNOWN_STATE"}, True) == "unknown"
    # rejected → 不管有没有 market_state 都返回 unknown
    assert _build_entry_mode({"market_state": "STRONG_TREND"}, False) == "unknown"


# ══════════════════════════════════════════════════════════════════
# edge：payload 缺失 accepted 关键字段时的容错
# ══════════════════════════════════════════════════════════════════

def test_accepted_false_when_no_direction():
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = {"symbol": "BTCUSDT", "score": 50, "grade": "D", "reason": "score too low"}
    asyncio.run(rec.record(result, "BTCUSDT"))
    assert sink.payloads[0]["accepted"] is False
    assert sink.payloads[0]["direction"] == "FLAT"
    assert sink.payloads[0]["rejection_reason"] == "score too low"


# ══════════════════════════════════════════════════════════════════
# A-1 回归：Decimal/datetime 特征值序列化不丢（serialize_decision.ok=True）
# ══════════════════════════════════════════════════════════════════

def test_decimal_and_datetime_features_serialize_ok():
    """analyze() 返回的 Decimal / datetime 特征经 _jsonable_feature_value
    归一化后，serialize_decision 应该 ok=True、特征不丢。
    """
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    # 模拟 analyze() 返回：entry_price 是 Decimal，timestamp 是 naive datetime
    now_naive = datetime.now()  # naive 本地时间
    result = {
        "symbol": "BTCUSDT",
        "score": Decimal("82.5"),
        "direction": "LONG",
        "market_state": "STRONG_TREND",
        "entry_price": Decimal("50000.50"),
        "atr": Decimal("750.25"),
        "quantity": Decimal("0.006"),
        "position_size_usdt": Decimal("300.0"),
        "timestamp": now_naive,
    }
    asyncio.run(rec.record(result, "BTCUSDT"))

    assert sink.calls == 1
    payload = sink.payloads[0]
    assert payload["accepted"] is True

    # 关键：特征数应该全保留（score/entry_price/atr/quantity/position_size_usdt）
    feature_names = {f["name"] for f in payload.get("features", [])}
    for expect_name in ("score", "entry_price", "atr", "quantity", "position_size_usdt"):
        assert expect_name in feature_names, f"特征 {expect_name} 应该保留"

    # entry_price 值应该是 str（Decimal 归一化后）
    entry_feat = next(f for f in payload["features"] if f["name"] == "entry_price")
    assert entry_feat["value"] == "50000.50"
    # decision_time_utc 经 serialize_decision 序列化后是 ISO 字符串（而非 datetime 对象）
    dt_iso = payload["decision_time_utc"]
    assert isinstance(dt_iso, str) and "T" in dt_iso  # ISO 8601 基本判据


# ══════════════════════════════════════════════════════════════════
# C-1 回归：naive datetime（北京时间容器）→ 正确 UTC，不偏移 8 小时
# ══════════════════════════════════════════════════════════════════

def test_normalize_decision_time_naive_tz_correct():
    """容器 TZ=Asia/Shanghai（UTC+8），analyze() 里 datetime.now() 返回
    naive 北京时间。_normalize_decision_time 应按 .timestamp() 本地时区
    解释，得到的 UTC datetime 才正确——不能直接 naive.replace(tzinfo=UTC)。
    """
    from strategies.btc_eth.evidence_recorder import _normalize_decision_time

    # 构造一个明确的 naive datetime（北京时间 2024-10-15 16:00:00 = UTC 08:00:00）
    naive_bj = datetime(2024, 10, 15, 16, 0, 0)
    utc_aware = _normalize_decision_time(naive_bj)
    # 如果直接 replace(tz=UTC)，utc_aware 会是 "2024-10-15 16:00 UTC"（错 8 小时）
    # 正确应该是容器时区对应的 UTC。本开发机 TZ=America/Los_Angeles 可能不同，
    # 所以断言重点是：utc_aware.timestamp() 应该等于 naive_bj.timestamp()
    # （naive .timestamp() 自动按系统本地时区解释——这就是整个口径的关键）
    assert utc_aware.timestamp() == naive_bj.timestamp()
    assert utc_aware.tzinfo == timezone.utc


# ══════════════════════════════════════════════════════════════════
# 补充覆盖：record 内部异常 / payload=None 跳过 / 落库超时 /
#          时间归一边界 / 特征值归一 / entry_mode 空值 / 兜底配置
# ══════════════════════════════════════════════════════════════════

def test_record_swallows_internal_exception():
    """_do_record_async 内部异常（非法 timestamp）→ record 吞掉、不调 sink。"""
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    result = {"symbol": "BTCUSDT", "score": 50, "timestamp": "not-a-number"}
    asyncio.run(rec.record(result, "BTCUSDT"))  # 不应抛
    assert sink.calls == 0


def test_record_skips_write_when_payload_none(monkeypatch):
    """serialize_decision 返回 payload=None → 跳过落库、sink 不被调用。"""
    from types import SimpleNamespace
    from strategies.btc_eth import evidence_recorder as rec_mod

    def fake_serialize(_decision):
        return SimpleNamespace(ok=True, payload=None, failed_records=0, errors=[])

    monkeypatch.setattr(rec_mod, "serialize_decision", fake_serialize)
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    asyncio.run(rec.record(_accepted_result(), "BTCUSDT"))
    assert sink.calls == 0


def test_write_or_drop_timeout_swallowed():
    """sink 落库超过 db_timeout → asyncio.TimeoutError 被吞、不向上抛。"""
    class SlowSink:
        def __init__(self):
            self.calls = 0

        async def upsert_evidence(self, payload):
            self.calls += 1
            await asyncio.sleep(1.0)

    cfg = _config(enabled=True)
    cfg["decision_evidence"]["db_timeout_seconds"] = 0.05
    sink = SlowSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=cfg)
    asyncio.run(rec.record(_accepted_result(), "BTCUSDT"))  # 不应抛
    assert sink.calls == 1


def test_hourly_anchor_ms_none_uses_current_time():
    """None → 当前时间取整；结果必须是 1 小时（毫秒）的整数倍。"""
    anchor = _hourly_anchor_ms(None)
    assert anchor % (3600 * 1000) == 0
    import time
    now_ms = int(time.time() * 1000)
    assert 0 <= now_ms - anchor < 3600 * 1000


def test_hourly_anchor_ms_float_input():
    """float 输入 → int(float(ts)) 后取整。"""
    assert _hourly_anchor_ms(1728999999999.9) == _hourly_anchor_ms(1728999999999)


def test_normalize_decision_time_none_returns_utc_now():
    """None → 当前 UTC 时间（aware）。"""
    out = _normalize_decision_time(None)
    assert out.tzinfo == timezone.utc


def test_normalize_decision_time_aware_converts_to_utc():
    """aware datetime（如 +08:00）→ astimezone(UTC)，时刻不变。"""
    from datetime import timedelta
    bj = timezone(timedelta(hours=8))
    dt = datetime(2024, 10, 15, 16, 0, 0, tzinfo=bj)
    out = _normalize_decision_time(dt)
    assert out.tzinfo == timezone.utc
    assert out.hour == 8  # 16:00 +08:00 → 08:00 UTC


def test_normalize_decision_time_millisecond_timestamp():
    """毫秒级数值时间戳 → /1000 转 UTC。"""
    ms = 1728997200000
    out = _normalize_decision_time(ms)
    assert out == datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def test_normalize_decision_time_second_vs_millisecond_threshold():
    """秒/毫秒自动判定：同一时刻的秒与毫秒两种形态归一结果一致。"""
    sec_ts = 1_728_999_719          # < 1e12 → 按秒解释
    ms_ts = 1_728_999_719_000       # > 1e12 → 按毫秒解释（与上一行同一时刻）
    out_sec = _normalize_decision_time(sec_ts)
    out_ms = _normalize_decision_time(ms_ts)
    assert out_sec == out_ms
    assert out_sec.tzinfo == timezone.utc


def test_normalize_decision_time_boundary_1e12_treated_as_seconds():
    """边界缺陷记录：恰好 1e12 不大于阈值（严格大于判定）→ 被当秒解释。

    1e12 秒 = 33658 年，超出 datetime 范围（1~9999 年）→ 抛异常。
    该异常会被 record() 外层吞掉不致崩溃，但属于真实边界缺陷：
    恰好等于 1e12 的毫秒时间戳无法归一（详见测试报告缺陷清单）。
    """
    with pytest.raises((ValueError, OSError, OverflowError)):
        _normalize_decision_time(1_000_000_000_000)


def test_jsonable_feature_value_datetime_becomes_isoformat():
    """datetime 特征值 → isoformat 字符串（保留时刻信息）。"""
    dt = datetime(2024, 10, 15, 8, 30, tzinfo=timezone.utc)
    out = _jsonable_feature_value(dt)
    assert out == dt.isoformat()
    assert isinstance(out, str)


def test_jsonable_feature_value_decimal_becomes_str():
    """Decimal 特征值 → str 保精度。"""
    assert _jsonable_feature_value(Decimal("1.23")) == "1.23"


def test_jsonable_feature_value_unserializable_becomes_str():
    """不可 JSON 序列化对象 → str() 兜底（except 分支）。"""
    out = _jsonable_feature_value(object())
    assert isinstance(out, str)


def test_jsonable_feature_value_jsonable_passthrough():
    """可 json.dumps 的非原语（tuple）→ try 分支原样返回。"""
    val = (1, 2, 3)
    assert _jsonable_feature_value(val) == val


def test_jsonable_feature_value_primitives_untouched():
    """str/int/float/bool/None 原样返回（第一个分支）。"""
    assert _jsonable_feature_value("x") == "x"
    assert _jsonable_feature_value(7) == 7
    assert _jsonable_feature_value(1.5) == 1.5
    assert _jsonable_feature_value(True) is True
    assert _jsonable_feature_value(None) is None


def test_build_entry_mode_missing_market_state():
    """market_state 缺失/None/空串 → unknown（if not ms 分支）。"""
    assert _build_entry_mode({}, True) == "unknown"
    assert _build_entry_mode({"market_state": None}, True) == "unknown"
    assert _build_entry_mode({"market_state": ""}, True) == "unknown"


def test_db_timeout_defaults_to_3s_when_missing():
    """config 缺 db_timeout_seconds → 默认 3.0 秒兜底。"""
    cfg = {"decision_evidence": {"enabled": True}}
    rec = DecisionEvidenceRecorder(sink=FakeSink(), config=cfg)
    assert rec.db_timeout == 3.0


def test_resolve_account_id_non_dict_config(monkeypatch):
    """config 非 dict / 缺 decision_evidence → 走 env/default 兜底。"""
    monkeypatch.delenv("TRADE_ACCOUNT_ID", raising=False)
    assert _resolve_account_id(None) == "default"   # 非 dict config
    assert _resolve_account_id({}) == "default"     # 无 decision_evidence 键


def test_recorder_enabled_truthy_string():
    """enabled='1' 字符串按 bool() truthy 口径开启（与 main.py 开关口径统一）。"""
    cfg = {"decision_evidence": {"enabled": "1"}}
    rec = DecisionEvidenceRecorder(sink=FakeSink(), config=cfg)
    assert rec.enabled is True


def test_record_empty_result_no_crash():
    """空 result dict（连 timestamp 都没有）→ 正常记录 rejected 决策。"""
    sink = FakeSink()
    rec = DecisionEvidenceRecorder(sink=sink, config=_config(enabled=True))
    asyncio.run(rec.record({}, "BTCUSDT"))
    assert sink.calls == 1
    payload = sink.payloads[0]
    assert payload["accepted"] is False
    assert payload["direction"] == "FLAT"
