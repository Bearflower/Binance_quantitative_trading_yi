"""
C1「决策与特征证据契约」—— 纯离线单元测试

全部不联网、不连库、不 mock 交易所。
"""
from datetime import datetime, timezone
from typing import Any, List, Optional

import pytest

from shared.decision_evidence import (
    FeatureEvidence,
    DecisionEvidence,
    EvidenceSerializeResult,
    generate_cycle_id,
    generate_decision_id,
    serialize_decision,
    check_available_at_constraint,
)


# 通用测试数据 -----------------------------------------------------------------


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间（便于断言比较）。"""
    return datetime.now(timezone.utc)


def _sample_feature(
    name: str = "rsi_14",
    value: Any = 62.5,
    event_time: Optional[datetime] = None,
    available_at: Optional[datetime] = None,
    availability: str = "live",
    source: str = "indicator",
) -> FeatureEvidence:
    """构造一个默认合法的 FeatureEvidence。"""
    return FeatureEvidence(
        name=name,
        value=value,
        unit=None,
        event_time=event_time or _utc_now(),
        available_at=available_at,
        availability=availability,  # type: ignore[arg-type]
        fetched_at=_utc_now(),
        source=source,
        quality_flag="ok",
        fallback_reason=None,
        lookback_actual="14",
    )


def _sample_features() -> List[FeatureEvidence]:
    """返回 sample decision 默认的特征列表（拆开主函数，控制长度）。"""
    return [_sample_feature()]


def _sample_decision(
    features: Optional[List[FeatureEvidence]] = None,
    account_id: str = "main",
    score_components: Any = ...,
    **overrides: Any,
) -> DecisionEvidence:
    """构造一个默认合法的 DecisionEvidence。

    score_components 用 Ellipsis (...) 作为 sentinel：
      没传 → 用默认值 {"rsi": 62.5, "macd": 0.8}
      显式传 None → 保留 None（测试边界用）
      显式传 dict → 使用传入值
    """
    now = _utc_now()
    cycle_id = generate_cycle_id(
        strategy_id="mtpcs",
        symbol="BTCUSDT",
        direction="LONG",
        entry_mode="trend",
        cycle_anchor=1728806400000,
    )
    decision_id = generate_decision_id(
        account_id=account_id,
        strategy_id="mtpcs",
        symbol="BTCUSDT",
        direction="LONG",
        entry_mode="trend",
        cycle_id=cycle_id,
    )
    sc_value = (
        {"rsi": 62.5, "macd": 0.8} if score_components is ... else score_components
    )
    payload = dict(
        account_id=account_id,
        strategy_id="mtpcs",
        decision_id=decision_id,
        cycle_id=cycle_id,
        decision_time_utc=now,
        code_hash="a" * 64,
        config_hash="b" * 64,
        entry_mode="trend",
        universe_snapshot_id="uni_20241014",
        symbol="BTCUSDT",
        direction="LONG",
        score_components=sc_value,
        thresholds={"rsi_max": 70.0, "score_min": 0.6},
        planned_qty=0.1,
        planned_notional=6000.0,
        margin_limit=10000.0,
        margin_limit_source="config_default",
        accepted=True,
        rejection_reason=None,
        features=features if features is not None else _sample_features(),
    )
    payload.update(overrides)
    return DecisionEvidence(**payload)


# ---------------------------------------------------------------------------
# 1. 正常路径
# ---------------------------------------------------------------------------


class TestSerializeHappyPath:
    """测试完整 DecisionEvidence + 多个 FeatureEvidence 的正常序列化。"""

    def test_full_decision_with_multiple_features(self):
        """完整决策 + 多个特征 → ok=True，payload 含全部字段。"""
        feats = [
            _sample_feature(name="rsi_14", value=62.5),
            _sample_feature(name="macd_hist", value=0.8, source="indicator"),
            _sample_feature(name="volume_ratio", value=1.3, source="market"),
        ]
        decision = _sample_decision(features=feats)

        result = serialize_decision(decision)

        assert result.ok is True, f"ok 应为 True，errors={result.errors}"
        assert result.failed_records == 0
        assert result.errors == []

        payload = result.payload
        # 决策级字段全部在 payload 中
        assert payload["account_id"] == "main"
        assert payload["strategy_id"] == "mtpcs"
        assert payload["decision_id"] == decision.decision_id
        assert payload["cycle_id"] == decision.cycle_id
        assert payload["symbol"] == "BTCUSDT"
        assert payload["direction"] == "LONG"
        assert payload["accepted"] is True
        assert payload["score_components"] == {"rsi": 62.5, "macd": 0.8}
        assert payload["planned_qty"] == 0.1

        # 特征列表全部在 payload 中
        assert len(payload["features"]) == 3
        feat_names = {f["name"] for f in payload["features"]}
        assert feat_names == {"rsi_14", "macd_hist", "volume_ratio"}


# ---------------------------------------------------------------------------
# 2. 边界条件
# ---------------------------------------------------------------------------


class TestBoundaryCases:
    """测试空特征、None score、零值 planned_qty 等边界。"""

    def test_empty_features_list(self):
        """features=[] → ok=True，payload features 为空列表。"""
        decision = _sample_decision(features=[])
        result = serialize_decision(decision)

        assert result.ok is True
        assert result.failed_records == 0
        assert result.payload["features"] == []

    def test_none_score_components(self):
        """score_components=None → ok=True，payload 中为 None（不是缺失）。"""
        decision = _sample_decision(score_components=None, features=[])
        result = serialize_decision(decision)

        assert result.ok is True
        assert result.payload["score_components"] is None
        # 其他决策级字段仍存在
        assert result.payload["symbol"] == "BTCUSDT"

    def test_planned_qty_zero_vs_none(self):
        """planned_qty=0 与 planned_qty=None 行为不同。"""
        decision_zero = _sample_decision(planned_qty=0.0, features=[])
        decision_none = _sample_decision(planned_qty=None, features=[])

        r1 = serialize_decision(decision_zero)
        r2 = serialize_decision(decision_none)

        assert r1.payload["planned_qty"] == 0.0
        assert r2.payload["planned_qty"] is None
        assert r1.ok is True and r2.ok is True


# ---------------------------------------------------------------------------
# 3. available_at 语义
# ---------------------------------------------------------------------------


class TestAvailableAtSemantics:
    """测试 available_at 为 None 时必须是 unknown，且不得冒充 event_time。"""

    def test_available_at_none_must_be_unknown(self):
        """available_at=None → payload 中 available_at=None，availability 必须是 unknown。"""
        feat = FeatureEvidence(
            name="rsi_14",
            value=55.0,
            event_time=_utc_now(),
            available_at=None,
            availability="unknown",  # type: ignore[arg-type]
            source="indicator",
        )
        decision = _sample_decision(features=[feat])
        result = serialize_decision(decision)

        assert result.ok is True
        f_payload = result.payload["features"][0]
        assert f_payload["available_at"] is None
        # 严禁 available_at 等于 event_time
        assert f_payload["available_at"] != f_payload["event_time"]
        assert f_payload["availability"] == "unknown"

    def test_available_at_after_decision_time_fails_check(self):
        """available_at 晚于 decision_time → check_available_at_constraint 返回不通过。"""
        event_t = _utc_now()
        decision_t = datetime(2024, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
        # available_at 明显晚于 decision_time
        avail_t = datetime(2024, 10, 16, 0, 0, 0, tzinfo=timezone.utc)

        feat = FeatureEvidence(
            name="rsi_14",
            value=55.0,
            event_time=event_t,
            available_at=avail_t,
            availability="live",  # type: ignore[arg-type]
            source="indicator",
        )

        passed, reason = check_available_at_constraint(feat, decision_t)
        assert passed is False
        assert reason is not None
        assert "available_at" in reason

    def test_check_passes_when_unknown(self):
        """availability=unknown 时 → check 直接返回通过（不适用）。"""
        feat = FeatureEvidence(
            name="rsi_14",
            value=55.0,
            event_time=_utc_now(),
            available_at=None,
            availability="unknown",  # type: ignore[arg-type]
            source="indicator",
        )
        passed, reason = check_available_at_constraint(feat, _utc_now())
        assert passed is True
        assert reason is None

    def test_available_at_after_decision_time_exposed_by_serialize(self):
        """available_at 晚于 decision_time 时 serialize_decision 必须暴露失败。

        新增行为（整改 #6 接线）：不阻塞 payload 产出，但 ok=False、
        failed_records>=1、errors 含原因、该特征仍在 payload 里。
        """
        event_t = _utc_now()
        decision_t = datetime(2024, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
        avail_t = datetime(2024, 10, 16, 0, 0, 0, tzinfo=timezone.utc)

        feat_violate = FeatureEvidence(
            name="late_feat",
            value=55.0,
            event_time=event_t,
            available_at=avail_t,
            availability="live",  # type: ignore[arg-type]
            source="indicator",
        )
        decision = _sample_decision(
            features=[feat_violate],
            decision_time_utc=decision_t,
        )

        result = serialize_decision(decision)

        assert result.ok is False
        assert result.failed_records >= 1
        assert any("available_at" in e for e in result.errors), \
            f"errors 应含 available_at 违约原因，实际: {result.errors}"
        # 关键：特征仍在 payload 里（单特征失败不阻塞整体）
        assert len(result.payload["features"]) == 1
        assert result.payload["features"][0]["name"] == "late_feat"


# ---------------------------------------------------------------------------
# 4. 异常路径（单特征序列化失败）
# ---------------------------------------------------------------------------


class TestSingleFeatureSerializeFailure:
    """单个特征序列化失败：不抛异常，ok=False，failed_records=1，其余正常。"""

    def test_unjsonizable_value_does_not_break(self):
        """value 为不可序列化对象 → 不抛异常，ok=False，failed_records=1。"""

        class _Unserializable:
            """没有 __dict__，json.dumps 直接炸。"""

        feat_good = _sample_feature(name="rsi_14", value=62.5)
        feat_bad = FeatureEvidence(
            name="weird_feat",
            value=_Unserializable(),  # 不可 JSON 序列化
            event_time=_utc_now(),
            available_at=_utc_now(),
            availability="live",  # type: ignore[arg-type]
            source="custom",
        )
        feat_also_good = _sample_feature(name="volume", value=12345.0, source="market")

        decision = _sample_decision(features=[feat_good, feat_bad, feat_also_good])

        # 关键：不得抛异常
        result: EvidenceSerializeResult = serialize_decision(decision)

        assert result.ok is False
        assert result.failed_records == 1
        # errors 里必须有该特征名
        assert any("weird_feat" in e for e in result.errors)
        # 成功的特征仍出现在 payload
        assert len(result.payload["features"]) == 2
        feat_names = {f["name"] for f in result.payload["features"]}
        assert "rsi_14" in feat_names
        assert "volume" in feat_names


# ---------------------------------------------------------------------------
# 5. 幂等性
# ---------------------------------------------------------------------------


class TestIdempotency:
    """同一输入多次调用必须一致。"""

    def test_serialize_twice_identical(self):
        """同一 DecisionEvidence 连续 serialize 两次，payload 完全相同（==）。"""
        decision = _sample_decision(features=[
            _sample_feature(name="rsi", value=55.0),
            _sample_feature(name="volume", value=1000.0, source="market"),
        ])
        r1 = serialize_decision(decision)
        r2 = serialize_decision(decision)

        assert r1.ok == r2.ok
        assert r1.failed_records == r2.failed_records
        assert r1.payload == r2.payload  # 深等值比较

    def test_same_intent_same_decision_id(self):
        """相同意图键 → decision_id 完全相同。"""
        cycle_id = generate_cycle_id(
            strategy_id="mtpcs", symbol="BTCUSDT", direction="LONG",
            entry_mode="trend", cycle_anchor=1728806400000,
        )
        d1 = generate_decision_id(
            account_id="main", strategy_id="mtpcs", symbol="BTCUSDT",
            direction="LONG", entry_mode="trend", cycle_id=cycle_id,
        )
        d2 = generate_decision_id(
            account_id="main", strategy_id="mtpcs", symbol="BTCUSDT",
            direction="LONG", entry_mode="trend", cycle_id=cycle_id,
        )
        assert d1 == d2

    def test_cycle_anchor_change_changes_ids(self):
        """cycle_anchor +1ms → cycle_id 变，进而 decision_id 变。"""
        cycle_1 = generate_cycle_id(
            strategy_id="mtpcs", symbol="BTCUSDT", direction="LONG",
            entry_mode="trend", cycle_anchor=1728806400000,
        )
        cycle_2 = generate_cycle_id(
            strategy_id="mtpcs", symbol="BTCUSDT", direction="LONG",
            entry_mode="trend", cycle_anchor=1728806400001,  # +1ms
        )
        assert cycle_1 != cycle_2

        d1 = generate_decision_id(
            account_id="main", strategy_id="mtpcs", symbol="BTCUSDT",
            direction="LONG", entry_mode="trend", cycle_id=cycle_1,
        )
        d2 = generate_decision_id(
            account_id="main", strategy_id="mtpcs", symbol="BTCUSDT",
            direction="LONG", entry_mode="trend", cycle_id=cycle_2,
        )
        assert d1 != d2


# ---------------------------------------------------------------------------
# 6. 脱敏
# ---------------------------------------------------------------------------


class TestSanitization:
    """account_id 疑似密钥 + 混入密钥字段 → 脱敏正确。"""

    def test_account_id_whitelisted_preserved(self):
        """account_id=main（白名单）→ payload 内原样保留。"""
        decision = _sample_decision(account_id="main")
        result = serialize_decision(decision)
        assert result.payload["account_id"] == "main"
        assert result.ok is True
        assert result.failed_records == 0

    def test_suspicious_account_id_hashed(self):
        """account_id 疑似密钥 → payload 内是哈希，不是原串。"""
        suspicious = "k" * 64 + ":secret"  # 命中 len>=64 + 含冒号
        decision = _sample_decision(account_id=suspicious, features=[])
        result = serialize_decision(decision)

        assert result.ok is True  # account_id 脱敏不算失败
        assert suspicious not in result.payload["account_id"]
        # payload 内是 16 位小写 hex 哈希
        hashed = result.payload["account_id"]
        assert len(hashed) == 16
        int(hashed, 16)  # 能解析为 hex
        assert hashed == hashed.lower()
        assert any("account_id" in e and "脱敏" in e for e in result.errors)

    def test_suspicious_account_id_no_colon_but_long_enough(self):
        """无冒号但长度 >= 64 的纯字母 account_id → 也应被哈希。

        覆盖整改前未覆盖的 `len(value) >= 64` 分支。
        """
        suspicious = "x" * 64  # 无冒号，但长度 64
        decision = _sample_decision(account_id=suspicious, features=[])
        result = serialize_decision(decision)

        assert result.ok is True
        assert result.payload["account_id"] != suspicious
        assert any("account_id" in e and "脱敏" in e for e in result.errors)

    def test_secret_key_in_dict_gets_stripped(self):
        """score_components 里混入 api_key 键 → 被剔除且计入 failed_records。"""
        decision = _sample_decision(
            score_components={"api_key": "xxx", "rsi": 55.0},
            features=[],
        )
        result = serialize_decision(decision)

        assert result.ok is False  # 剔除导致 ok=False
        assert result.failed_records == 1
        # api_key 键不在 payload 里
        assert "api_key" not in result.payload["score_components"]
        # 正常字段保留
        assert result.payload["score_components"]["rsi"] == 55.0
        assert any("api_key" in e for e in result.errors)

    def test_invalid_code_hash_and_config_hash_get_hashed(self):
        """非法 code_hash / config_hash 被替换为哈希、告警进 errors、ok 仍为 True。

        覆盖整改前 0 覆盖的非法形态分支。
        """
        decision = _sample_decision(
            features=[],
            code_hash="deadbeef",   # 8 位，非 64 位 hex
            config_hash="zz" * 32,  # 64 位但含非 hex 字符
        )
        result = serialize_decision(decision)

        # 核心：非法哈希形态不计入 failed_records，ok 保持 True
        assert result.ok is True
        assert result.failed_records == 0
        # payload 中已被替换为 16 位小写 hex（哈希）
        assert len(result.payload["code_hash"]) == 16
        assert len(result.payload["config_hash"]) == 16
        # errors 含两条中文告警
        assert any("code_hash" in e and "脱敏" in e for e in result.errors)
        assert any("config_hash" in e and "脱敏" in e for e in result.errors)


# ---------------------------------------------------------------------------
# 7. 标识形态
# ---------------------------------------------------------------------------


class TestIdForm:
    """generate_cycle_id / generate_decision_id 输出的长度与 hex 形态。"""

    def test_cycle_id_16_hex(self):
        """cycle_id 是 16 位全小写 hex。"""
        cid = generate_cycle_id("s", "SYM", "LONG", "trend", 1728806400000)
        assert len(cid) == 16
        assert cid == cid.lower()
        int(cid, 16)  # 能解析为 hex

    def test_decision_id_32_hex(self):
        """decision_id 是 32 位全小写 hex。"""
        cid = generate_cycle_id("s", "SYM", "LONG", "trend", 1728806400000)
        did = generate_decision_id("main", "s", "SYM", "LONG", "trend", cid)
        assert len(did) == 32
        assert did == did.lower()
        int(did, 16)  # 能解析为 hex
