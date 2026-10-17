"""
R1 + R4 回归测试（先红后绿转正式）

来源：docs/reviews/strategy_reaudit_2026-10-16/reproduce_findings.py 场景反转
验收标准见 docs/research/strategy_remediation/requirements_R1-R6_2026-10-16.md
"""
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from strategies.hrs.candidate_pool import DynamicThresholds
from strategies.hrs.scoring_engine import ScoringEngine, ScoringResult
from strategies.hrs.strategy import HRSStrategy

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as _f:
    CONFIG = yaml.safe_load(_f)


# ==================== R1 回归 ====================


class TestR1PrimaryResultConsistency:
    """R1: price_change_24h 接入前后，旧版 standard 主结果意图必须一致"""

    @pytest.fixture
    def engine(self):
        e = ScoringEngine(CONFIG)
        # 固定技术/情绪分（模拟稳定输入），消除其他变量影响
        e.calculate_technical_score = lambda *a, **kw: (
            10, {"primary_pattern_score": 5},
        )
        e.calculate_sentiment_score = lambda *a, **kw: (10, {})
        return e

    def test_r1_main_result_entry_mode_always_standard(self, engine):
        """主结果 entry_mode 恒 'standard'——EMM/半EMM 只走 shadow 链"""
        r_no_change = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=None,
        )
        r_with_change = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=30,
        )
        assert r_no_change.entry_mode == "standard"
        assert r_with_change.entry_mode == "standard"

    def test_r1_main_result_intent_unchanged_by_price_change(self, engine):
        """同一输入，price_change_24h=None vs 30 → 主结果 should_entry 完全一致"""
        r1 = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=None,
        )
        r2 = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=30,
        )
        assert engine.should_entry(r1) == engine.should_entry(r2)
        assert r1.total_score == r2.total_score
        assert r1.contract_score == r2.contract_score
        assert r1.technical_score == r2.technical_score
        assert r1.sentiment_score == r2.sentiment_score

    def test_r1_main_result_extreme_bonus_always_zero(self, engine):
        """主结果 extreme_bonus 必须恒 0（extreme_bonus 从 standard 迁到 shadow 链）"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=999,
        )
        assert r.extreme_bonus == 0.0

    def test_r1_shadow_results_attached_when_price_change_available(self, engine):
        """price_change_24h 可得时，shadow 链至少产出 EMM 或半EMM 结果"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=30,
        )
        assert r.shadows, "price_change_24h 可得时 shadows 不应为空"
        shadow_modes = {s.entry_mode for s in r.shadows}
        # EMM 或 semi_emm 至少有一个
        assert shadow_modes & {"emm", "semi_emm"}

    def test_r1_no_shadow_when_price_change_missing(self, engine):
        """price_change_24h=None → shadows 为空（EMM/半EMM 无法判定）"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=None,
        )
        assert len(r.shadows) == 0

    def test_r1_shadow_are_independent_scoring_results(self, engine):
        """shadow 结果是独立 ScoringResult，主结果不应因 shadow 存在而改变"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=30,
        )
        for shadow in r.shadows:
            assert isinstance(shadow, ScoringResult)
            assert shadow.symbol == r.symbol
            assert shadow.direction == r.direction
            # shadow 的 shadows 子列表应为空（叶子节点）
            assert shadow.shadows == []

    def test_r1_to_dict_includes_shadows(self, engine):
        """ScoringResult.to_dict() 必须包含 shadows 字段"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, price_change_24h=30,
        )
        d = r.to_dict()
        assert "shadows" in d
        assert isinstance(d["shadows"], list)


class TestR1IsShadowEntryModeOriginAware:
    """R1: _is_shadow_entry_mode 对 lv_rm 按 signal_origin 区分影子范围"""

    @pytest.fixture
    def strategy(self):
        return SimpleNamespace(config=CONFIG)

    def test_lv_rm_nonempty_pool_not_shadow(self, strategy):
        """非空池 LV-RM 保持原政策，不影子化"""
        assert HRSStrategy._is_shadow_entry_mode(
            strategy, "lv_rm", "nonempty_pool",
        ) is False

    def test_lv_rm_empty_pool_is_shadow(self, strategy):
        """空池 LV-RM 按 config.lv_rm.execution_mode 判定（默认 shadow）"""
        assert HRSStrategy._is_shadow_entry_mode(
            strategy, "lv_rm", "empty_pool",
        ) is True

    def test_lv_rm_missing_origin_fail_closed(self, strategy):
        """signal_origin 缺失 → fail-closed 判为影子（防御性）"""
        assert HRSStrategy._is_shadow_entry_mode(strategy, "lv_rm", None) is True

    def test_emm_always_by_config_not_origin(self, strategy):
        """EMM 影子判定不看 signal_origin，只按 config.emm.execution_mode"""
        assert HRSStrategy._is_shadow_entry_mode(strategy, "emm", None) is True
        assert HRSStrategy._is_shadow_entry_mode(
            strategy, "emm", "nonempty_pool",
        ) is True
        assert HRSStrategy._is_shadow_entry_mode(
            strategy, "emm", "empty_pool",
        ) is True

    def test_standard_never_shadow(self, strategy):
        """standard 模式永远不影子化（不在 SHADOW_CAPABLE_ENTRY_MODES 里）"""
        assert HRSStrategy._is_shadow_entry_mode(strategy, "standard", None) is False
        assert HRSStrategy._is_shadow_entry_mode(
            strategy, "standard", "empty_pool",
        ) is False


# ==================== R4 回归 ====================


class TestR4MarketCapVeto:
    """R4: has_market_cap=False → 开仓链显式 veto，不吃 fallback 分"""

    @pytest.fixture
    def engine(self):
        e = ScoringEngine(CONFIG)
        e.calculate_technical_score = lambda *a, **kw: (10, {"primary_pattern_score": 5})
        e.calculate_sentiment_score = lambda *a, **kw: (10, {})
        return e

    def test_score_veto_when_no_market_cap_short(self, engine):
        """score() 入口 veto：has_market_cap=False, short"""
        r = engine.score(
            "TESTUSDT", "short", 0.3, {}, 0.01, has_market_cap=False,
        )
        assert r.veto is True
        assert "缺少真实未过期市值" in (r.veto_reason or "")
        assert engine.should_entry(r) is False
        # 不吃 fallback 分（contract_score 应为 0 而非 fallback 10）
        assert r.contract_score == 0.0

    def test_score_veto_when_no_market_cap_long(self, engine):
        """score() 入口 veto：has_market_cap=False, long（不吃 fallback long=5）"""
        r = engine.score(
            "TESTUSDT", "long", 0.3, {}, 0.01, has_market_cap=False,
        )
        assert r.veto is True
        assert engine.should_entry(r) is False
        # 原 fallback long=5 不再被吃（contract_score 为 0）
        assert r.contract_score == 0.0

    def test_score_allows_when_market_cap_ok(self, engine):
        """has_market_cap=True → 正常评分不受 veto"""
        r = engine.score(
            "TESTUSDT", "long", 0.3, {}, 0.01, has_market_cap=True,
        )
        assert r.veto is False

    @pytest.mark.asyncio
    async def test_score_lv_rm_veto_when_no_market_cap(self, engine):
        """score_lv_rm() 入口同样 veto（LV-RM 同为开仓侧）"""
        klines_1h = [
            {"open": 100 + i, "close": 100 + i, "high": 100 + i + 1,
             "low": 100 + i - 1}
            for i in range(50)
        ]
        klines_4h = [
            {"open": 100 + i * 4, "close": 100 + i * 4, "high": 100 + i * 4 + 1,
             "low": 100 + i * 4 - 1}
            for i in range(20)
        ]
        r = await engine.score_lv_rm(
            symbol="TESTUSDT", direction="short",
            oi_market_cap_ratio=0.3, funding_rate=0.0001,
            has_market_cap=False, klines_1h=klines_1h, klines_4h=klines_4h,
        )
        assert r.veto is True
        assert "缺少真实未过期市值" in (r.veto_reason or "")
        assert r.entry_mode == "lv_rm"
        assert r.contract_score == 0.0


class TestR4MarketCapSourceMapping:
    """R4 补充：_fetch_market_cap_ratio 对「缺失/代理/过期」来源的 fail-closed 映射，
    以及退出侧（require_true_for_entry=False）代理值不阻断（D5 存量退出不受阻）。"""

    @staticmethod
    def _make_strategy_with_source(source: str, value: float) -> HRSStrategy:
        """绕过 __init__ 注入最小依赖，market_data 返回指定来源的 provenance。"""
        from types import SimpleNamespace as NS

        s = object.__new__(HRSStrategy)
        s.market_data = NS(
            get_market_cap_provenance=AsyncMock(
                return_value=NS(
                    source=source, value=value,
                    as_of=None, available_at=None,
                    is_proxy=(source == "oi_proxy"), missing_reason=None,
                )
            ),
        )
        return s

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["oi_proxy", "missing", "expired"])
    async def test_entry_side_non_true_source_maps_to_no_cap(self, source):
        """开仓侧：非真实来源（代理/缺失/过期）→ has_market_cap=False（fail-closed）。"""
        s = self._make_strategy_with_source(source, value=1_000_000.0)
        ratio, has_cap, out_source, cap = await s._fetch_market_cap_ratio(
            "TESTUSDT", oi_usd=100.0, volume_24h_usd=0.0, require_true_for_entry=True,
        )
        assert has_cap is False, f"来源 {source} 开仓侧必须判为无真实市值"
        assert ratio == 0.0
        assert out_source == source
        assert cap == 1_000_000.0

    @pytest.mark.asyncio
    async def test_entry_side_true_source_maps_to_cap(self):
        """开仓侧：真实来源（coingecko/cache_hit）+ 值>0 + OI>0 → 正常出比率。"""
        for source in ("coingecko", "cache_hit"):
            s = self._make_strategy_with_source(source, value=1_000_000.0)
            ratio, has_cap, out_source, _ = await s._fetch_market_cap_ratio(
                "TESTUSDT", oi_usd=100.0, volume_24h_usd=0.0, require_true_for_entry=True,
            )
            assert has_cap is True, f"来源 {source} 开仓侧应视为有真实市值"
            assert abs(ratio - 100.0 / 1_000_000.0) < 1e-12

    @pytest.mark.asyncio
    async def test_exit_side_proxy_value_not_blocked(self):
        """退出侧（D5）：代理值也允许 has_market_cap=True，不阻断保护/减仓。"""
        s = self._make_strategy_with_source("oi_proxy", value=2_000_000.0)
        ratio, has_cap, out_source, cap = await s._fetch_market_cap_ratio(
            "TESTUSDT", oi_usd=100.0, volume_24h_usd=0.0, require_true_for_entry=False,
        )
        assert has_cap is True, "退出侧代理值不应阻断存量仓位保护"
        assert out_source == "oi_proxy"
        assert abs(ratio - 100.0 / 2_000_000.0) < 1e-12


class TestR4DynamicThresholdsPerSection:
    """R4: DynamicThresholds 逐截面样本门槛（禁止以费率样本数冒充）"""

    def test_valid_when_both_sections_enough(self):
        """资金费率截面 + OI/市值截面都达标 → is_valid()=True"""
        dt = DynamicThresholds()
        dt.funding_sample_count = 20
        dt.oi_market_cap_sample_count = 20
        dt.computed_at = datetime.datetime.now()
        assert dt.is_valid(min_sample_size=10) is True

    def test_invalid_when_oi_section_insufficient(self):
        """OI/市值截面样本不足 → is_valid()=False（即使费率截面充足）"""
        dt = DynamicThresholds()
        dt.funding_sample_count = 50
        dt.oi_market_cap_sample_count = 5  # OI 样本少
        dt.computed_at = datetime.datetime.now()
        assert dt.is_valid(min_sample_size=10) is False

    def test_invalid_when_funding_section_insufficient(self):
        """资金费率截面样本不足 → is_valid()=False（即使 OI 截面充足）"""
        dt = DynamicThresholds()
        dt.funding_sample_count = 5  # 费率样本少
        dt.oi_market_cap_sample_count = 50
        dt.computed_at = datetime.datetime.now()
        assert dt.is_valid(min_sample_size=10) is False

    def test_invalid_when_computed_at_missing(self):
        """computed_at=None → is_valid()=False（兼容旧调用方也一样）"""
        dt = DynamicThresholds()
        dt.funding_sample_count = 50
        dt.oi_market_cap_sample_count = 50
        dt.computed_at = None
        assert dt.is_valid(min_sample_size=10) is False

    def test_backward_compat_old_sample_count_only(self):
        """向后兼容：只设 sample_count（旧调用方）→ 回退到 sample_count > min 逻辑"""
        dt = DynamicThresholds()
        dt.sample_count = 50
        dt.funding_sample_count = 0  # 逐截面字段都是 0
        dt.oi_market_cap_sample_count = 0
        dt.computed_at = datetime.datetime.now()
        assert dt.is_valid(min_sample_size=10) is True

    def test_backward_compat_old_sample_count_insufficient(self):
        """向后兼容：sample_count 不足 → is_valid()=False"""
        dt = DynamicThresholds()
        dt.sample_count = 5
        dt.funding_sample_count = 0
        dt.oi_market_cap_sample_count = 0
        dt.computed_at = datetime.datetime.now()
        assert dt.is_valid(min_sample_size=10) is False

    def test_to_dict_includes_per_section_counts(self):
        """to_dict() 必须包含逐截面样本数字段（R4 新增）"""
        dt = DynamicThresholds()
        dt.funding_sample_count = 20
        dt.oi_market_cap_sample_count = 15
        dt.ema_sample_count = 30
        d = dt.to_dict()
        assert d["funding_sample_count"] == 20
        assert d["oi_market_cap_sample_count"] == 15
        assert d["ema_sample_count"] == 30
