"""
decision_evidence_sink 离线测试。

覆盖点：
  - 两条 SQL 安全（无注释/分号/危险词）
  - 决策级 upsert 冲突目标 (decision_id)、参数映射
  - 特征级 upsert 冲突目标 (decision_id, name, source)
  - updated_at 显式 now()（SQL 串里要有 now()）
  - 特征 value 列 TEXT：dict/list → json.dumps；str → 原样
  - db 抛异常时 sink 吞掉
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List

import pytest

from shared.decision_evidence_sink import (
    PostgresDecisionEvidenceSink,
    _to_text,
    _jsonb_or_none,
    _iso_to_timestamptz,
)


# ── Fake DB ────────────────────────────────────────────────────

class FakeDb:
    def __init__(self) -> None:
        self.transactions: List[List[tuple]] = []

    async def execute_transaction(self, queries: List[tuple]) -> bool:
        self.transactions.append(list(queries))
        return True


# ── SQL 安全断言 ──────────────────────────────────────────────

def assert_sql_safe(sql: str) -> None:
    assert "--" not in sql
    assert "/*" not in sql
    assert ";" not in sql
    up = sql.upper()
    for kw in ("CREATE", "GRANT", "ALTER", "DROP", "TRUNCATE", "REVOKE"):
        assert not re.search(rf"\b{kw}\b", up), f"含危险词 {kw}"


# ── 构造 payload ──────────────────────────────────────────────

def _sample_payload() -> Dict[str, Any]:
    return {
        "account_id": "main",
        "strategy_id": "btc_eth",
        "decision_id": "abc123def456abc123def456abc123def",
        "cycle_id": "cycle001",
        "decision_time_utc": "2024-10-15T08:30:00+00:00",
        "code_hash": None,
        "config_hash": None,
        "entry_mode": "trend",
        "universe_snapshot_id": None,
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score_components": {"trend": 25, "pattern": 50, "momentum": 25},
        "thresholds": {"min_score": 75},
        "planned_qty": 0.123,
        "planned_notional": 6150.0,
        "margin_limit": 200.0,
        "margin_limit_source": "config",
        "accepted": True,
        "rejection_reason": None,
        "features": [
            {
                "name": "score",
                "value": 82,
                "unit": None,
                "event_time": "2024-10-15T08:30:00+00:00",
                "available_at": "2024-10-15T08:30:00+00:00",
                "availability": "live",
                "fetched_at": "2024-10-15T08:30:01+00:00",
                "source": "analyze",
                "quality_flag": None,
                "fallback_reason": None,
                "lookback_actual": "default",
            },
            {
                "name": "entry_price",
                "value": "50000.50",
                "unit": "USDT",
                "event_time": "2024-10-15T08:30:00+00:00",
                "available_at": None,
                "availability": "unknown",
                "fetched_at": None,
                "source": "analyze",
                "quality_flag": None,
                "fallback_reason": None,
                "lookback_actual": None,
            },
            {
                "name": "trend_scores",
                "value": {"trend_strength": 25, "pattern_quality": 30},  # dict → json.dumps
                "unit": None,
                "event_time": None,
                "available_at": None,
                "availability": "unknown",
                "fetched_at": None,
                "source": "analyze",
                "quality_flag": None,
                "fallback_reason": None,
                "lookback_actual": None,
            },
        ],
    }


@pytest.fixture
def sink() -> PostgresDecisionEvidenceSink:
    return PostgresDecisionEvidenceSink(FakeDb())


# ══════════════════════════════════════════════════════════════════
# 核心测试
# ══════════════════════════════════════════════════════════════════

def test_upsert_evidence_sql_safe(sink: PostgresDecisionEvidenceSink):
    """两条 SQL 都安全；决策级 updated_at 显式 now()。"""
    asyncio.run(sink.upsert_evidence(_sample_payload()))

    db: FakeDb = sink.db
    assert len(db.transactions) == 1
    queries = db.transactions[0]
    assert len(queries) == 1 + 3  # 决策级 1 + 3 条特征

    for sql, _ in queries:
        assert_sql_safe(sql)

    decision_sql = queries[0][0]
    assert "ON CONFLICT (decision_id) DO UPDATE" in decision_sql
    assert "updated_at = now()" in decision_sql
    # 参数个数：决策级 SQL VALUES 有 20 个参数 + updated_at now()，VALUES 里 19 个参数
    assert len(queries[0][1]) == 19


def test_feature_upsert_conflict_target(sink: PostgresDecisionEvidenceSink):
    """特征级冲突目标 (decision_id, name, source)。"""
    asyncio.run(sink.upsert_evidence(_sample_payload()))
    db: FakeDb = sink.db

    # 第一条 SQL 决策级，后续三条特征级
    for sql, _ in db.transactions[0][1:]:
        assert "decision_feature_evidence" in sql
        assert "ON CONFLICT (decision_id, name, source) DO UPDATE" in sql


def test_feature_value_text_conversion(sink: PostgresDecisionEvidenceSink):
    """特征 value 列 TEXT：dict → json.dumps；str 原样。"""
    asyncio.run(sink.upsert_evidence(_sample_payload()))
    db: FakeDb = sink.db

    # 找到 name=trend_scores 的特征（dict value）
    feature_args_list = db.transactions[0][1:]
    trend_scores_args = None
    for _, args in feature_args_list:
        if args[1] == "trend_scores":
            trend_scores_args = args
            break
    assert trend_scores_args is not None
    # value 应该是 json.dumps 后的 str
    value_col = trend_scores_args[2]
    assert isinstance(value_col, str)
    parsed = json.loads(value_col)
    assert parsed == {"trend_strength": 25, "pattern_quality": 30}

    # entry_price（str value）应原样传
    entry_args = None
    for _, args in feature_args_list:
        if args[1] == "entry_price":
            entry_args = args
            break
    assert entry_args is not None
    assert entry_args[2] == "50000.50"  # 原样字符串


def test_upsert_evidence_empty_payload_noop(sink: PostgresDecisionEvidenceSink):
    """None/空 payload 不应触发 DB 调用。"""
    asyncio.run(sink.upsert_evidence({}))
    asyncio.run(sink.upsert_evidence({"decision_id": "x", "features": []}))
    # 第二条会触发决策级 upsert（features 空也写）
    assert len(sink.db.transactions) == 1  # 空 {} 被跳过；只有第二条


def test_jsonb_fields_converted(sink: PostgresDecisionEvidenceSink):
    """决策级 score_components / thresholds → JSON 串。"""
    asyncio.run(sink.upsert_evidence(_sample_payload()))
    db: FakeDb = sink.db
    decision_args = db.transactions[0][0][1]
    # 决策级 args 顺序：account_id, strategy_id, decision_id, cycle_id,
    # decision_time_utc, code_hash, config_hash, entry_mode, universe_snapshot_id,
    # symbol, direction, score_components, thresholds, planned_qty, planned_notional,
    # margin_limit, margin_limit_source, accepted, rejection_reason
    assert isinstance(decision_args[11], str)  # score_components JSON
    assert isinstance(decision_args[12], str)  # thresholds JSON
    assert json.loads(decision_args[11]) == {"trend": 25, "pattern": 50, "momentum": 25}
    assert json.loads(decision_args[12]) == {"min_score": 75}


def test_exception_is_swallowed():
    """db 抛异常时 sink 吞掉、不向上抛。"""
    class BoomDb:
        async def execute_transaction(self, queries):
            raise RuntimeError("boom")

    sink = PostgresDecisionEvidenceSink(BoomDb())
    asyncio.run(sink.upsert_evidence(_sample_payload()))  # 不应抛


# ══════════════════════════════════════════════════════════════════
# 工具函数单元测试
# ══════════════════════════════════════════════════════════════════

def test_to_text_variants():
    assert _to_text("hello") == "hello"                      # str 原样
    assert _to_text({"a": 1}) == '{"a": 1}'                   # dict → json
    assert _to_text([1, 2, 3]) == '[1, 2, 3]'                  # list → json
    assert _to_text(42) == "42"                                # 其他 → str()


def test_jsonb_or_none_variants():
    assert _jsonb_or_none(None) is None
    result = _jsonb_or_none({"z": 1, "a": 2})
    assert result is not None
    parsed = json.loads(result)
    assert parsed == {"a": 2, "z": 1}  # sort_keys=True 按字母排序


def test_iso_to_timestamptz_basic():
    """_iso_to_timestamptz：ISO 字符串→aware datetime；None→None；datetime 透传。"""
    iso = "2024-10-15T08:30:00+00:00"
    dt = _iso_to_timestamptz(iso)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert dt.isoformat() == iso

    # None → None
    assert _iso_to_timestamptz(None) is None

    # datetime aware 原样返回
    aware = datetime(2024, 10, 15, 8, 30, 0, tzinfo=dt.tzinfo)
    assert _iso_to_timestamptz(aware) is aware


# ══════════════════════════════════════════════════════════════════
# 补充覆盖：naive datetime 补 UTC / Z 后缀 / list JSONB /
#          accepted 与 availability 默认分支
# ══════════════════════════════════════════════════════════════════

def test_iso_to_timestamptz_naive_datetime_gets_utc():
    """naive datetime → 补 UTC tzinfo（不得原样透传给 asyncpg）。"""
    from datetime import timezone
    naive = datetime(2024, 10, 15, 8, 30, 0)
    out = _iso_to_timestamptz(naive)
    assert out.tzinfo == timezone.utc
    assert out == datetime(2024, 10, 15, 8, 30, 0, tzinfo=timezone.utc)


def test_iso_to_timestamptz_z_suffix():
    """ISO 字符串带 Z 后缀 → replace('Z','+00:00') 后正常解析为 UTC datetime。"""
    out = _iso_to_timestamptz("2024-10-15T08:30:00Z")
    assert out is not None
    assert out.utcoffset() is not None
    assert out.utcoffset().total_seconds() == 0
    assert (out.year, out.month, out.day, out.hour) == (2024, 10, 15, 8)


def test_jsonb_or_none_list_value():
    """list 值也可序列化为 JSON 串（sort_keys 对 list 顺序无影响）。"""
    out = _jsonb_or_none([3, 1, 2])
    assert out is not None
    assert json.loads(out) == [3, 1, 2]


def test_build_decision_args_accepted_defaults_false():
    """payload 缺 accepted 字段 → bool 默认 False；rejection_reason 缺失为 None。"""
    sink = PostgresDecisionEvidenceSink(FakeDb())
    payload = {
        "decision_id": "d1",
        "decision_time_utc": "2024-10-15T08:30:00+00:00",
    }
    args = sink._build_decision_args(payload)
    assert args[17] is False   # accepted
    assert args[18] is None    # rejection_reason


def test_build_feature_args_availability_default():
    """feat 缺 availability → 'unknown' 默认兜底。"""
    sink = PostgresDecisionEvidenceSink(FakeDb())
    payload = {"decision_id": "d1"}
    feat = {"name": "score", "value": 1}
    args = sink._build_feature_args(payload, feat)
    assert args[0] == "d1"
    assert args[1] == "score"
    assert args[2] == "1"        # value: int → str()
    assert args[6] == "unknown"  # availability 默认


def test_to_text_none_value():
    """None value → str(None)='None'（TEXT 列的兜底行为，如实断言）。"""
    assert _to_text(None) == "None"


def test_timestamptz_args_are_aware_datetime(sink: PostgresDecisionEvidenceSink):
    """回归：build 出的 4 个 TIMESTAMPTZ 列参数必须是 aware datetime。

    payload 里 decision_time_utc/event_time/available_at/fetched_at 都是
    ISO 字符串，asyncpg 的 timestamptz 编码器只接受 datetime 实例。
    此用例直接调用 _build_decision_args / _build_feature_args 断言类型。
    """
    payload = _sample_payload()
    feat = payload["features"][0]  # 第一条全是 ISO 字符串

    dec_args = sink._build_decision_args(payload)
    assert isinstance(dec_args[4], datetime), (
        f"decision_time_utc 应为 datetime，实际 {type(dec_args[4]).__name__}"
    )
    assert dec_args[4].tzinfo is not None

    feat_args = sink._build_feature_args(payload, feat)
    assert isinstance(feat_args[4], datetime) and feat_args[4].tzinfo is not None  # event_time
    assert isinstance(feat_args[5], datetime) and feat_args[5].tzinfo is not None  # available_at
    assert isinstance(feat_args[7], datetime) and feat_args[7].tzinfo is not None  # fetched_at

    # None 时间列（entry_price）应保持 None
    feat2 = payload["features"][1]  # available_at=None, fetched_at=None
    feat2_args = sink._build_feature_args(payload, feat2)
    assert feat2_args[5] is None  # available_at
    assert feat2_args[7] is None  # fetched_at
