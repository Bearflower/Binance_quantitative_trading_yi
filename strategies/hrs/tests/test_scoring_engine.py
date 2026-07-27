"""
评分引擎测试
测试 ScoringEngine 的评分逻辑、总分计算、入场判断
"""
import pytest
import yaml
from pathlib import Path

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.scoring_engine import ScoringEngine, ScoringResult


class TestScoringEngineInit:
    """测试评分引擎初始化"""

    def test_权重从配置正确加载(self):
        """验证权重从配置文件正确读取"""
        engine = ScoringEngine(CONFIG)
        assert engine.contract_weight == 0.25
        assert engine.technical_weight == 0.45
        assert engine.sentiment_weight == 0.30
        assert engine.entry_threshold == 6.5
        assert engine.min_technical_score == 6.0
        assert engine.min_primary_pattern_score == 2.0

    def test_年化费率参数正确加载(self):
        """验证年化费率参数从配置正确加载"""
        engine = ScoringEngine(CONFIG)
        assert engine.settlements_per_day == 3
        assert engine.days_per_year == 365

    def test_无配置时使用默认值(self):
        """验证空配置时使用默认值"""
        engine = ScoringEngine({})
        assert engine.contract_weight == 0.25
        assert engine.technical_weight == 0.45
        assert engine.sentiment_weight == 0.30
        assert engine.entry_threshold == 6.5


class TestContractScore:
    """测试合约数据评分"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_做空极度拥挤满分(self, engine):
        """做空方向：OI/市值比 > 0.25，极度拥挤，满分10"""
        score, details = engine.calculate_contract_score(0.30, "short")
        assert score == 10
        assert "极度拥挤" in details["reason"]

    def test_做空拥挤8分(self, engine):
        """做空方向：0.22 OI/市值比，拥挤，8分"""
        score, details = engine.calculate_contract_score(0.22, "short")
        assert score == 8

    def test_做空中等偏拥挤6分(self, engine):
        """做空方向：0.17 OI/市值比，中等偏拥挤，6分"""
        score, details = engine.calculate_contract_score(0.17, "short")
        assert score == 6

    def test_做空中性4分(self, engine):
        """做空方向：0.12 OI/市值比，中性，4分"""
        score, details = engine.calculate_contract_score(0.12, "short")
        assert score == 4

    def test_做空冷清2分(self, engine):
        """做空方向：0.07 OI/市值比，冷清，2分"""
        score, details = engine.calculate_contract_score(0.07, "short")
        assert score == 2

    def test_做空极度冷清0分(self, engine):
        """做空方向：0.02 OI/市值比，极度冷清，0分"""
        score, details = engine.calculate_contract_score(0.02, "short")
        assert score == 0

    def test_做多极度冷清满分(self, engine):
        """做多方向：高OI/市值比对做多不利，极度冷清=0分"""
        score, details = engine.calculate_contract_score(0.30, "long")
        assert score == 0

    def test_做多极度拥挤10分(self, engine):
        """做多方向：OI/市值比极低，极度拥挤（做多有利），10分"""
        score, details = engine.calculate_contract_score(0.02, "long")
        assert score == 10

    def test_做多冷清7分(self, engine):
        """做多方向：0.07 OI/市值比，冷清（做多有利），7分"""
        score, details = engine.calculate_contract_score(0.07, "long")
        assert score == 7

    def test_市值获取失败做空兜底0分(self, engine):
        """做空方向市值获取失败，兜底0分"""
        score, details = engine.calculate_contract_score(0.30, "short", has_market_cap=False)
        assert score == 0
        assert details["oi_market_cap_ratio"] is None

    def test_市值获取失败做多兜底5分(self, engine):
        """做多方向市值获取失败，兜底5分"""
        score, details = engine.calculate_contract_score(0.02, "long", has_market_cap=False)
        assert score == 5

    def test_边界值极端高阈值(self, engine):
        """边界值：刚好等于 0.25 极端高阈值"""
        score, _ = engine.calculate_contract_score(0.25, "short")
        # 0.25 >= extreme_high(0.25) is True
        assert score == 8  # high threshold 0.20, 0.25 >= 0.20 but 0.25 > 0.25 is False

    def test_边界值高阈值(self, engine):
        """边界值：刚好等于 0.20 高阈值"""
        score, _ = engine.calculate_contract_score(0.20, "short")
        assert score == 8  # 0.20 >= high(0.20)


class TestTechnicalScore:
    """测试技术面评分"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_做空全部形态命中满分(self, engine):
        """做空方向：三次冲顶+长上影线+放量滞涨，满分10"""
        patterns = {
            "three_tops": (True, 4.0),
            "long_upper_shadow": (True, 3.0),
            "volume_stagnation": (True, 3.0),
        }
        score, details = engine.calculate_technical_score(patterns, "short")
        assert score == 10.0
        assert details["primary_pattern"] is True
        assert details["primary_pattern_score"] == 4.0

    def test_做空仅三次冲顶部分得分(self, engine):
        """做空方向：仅三次冲顶（部分命中），2分"""
        patterns = {
            "three_tops": (True, 2.0),
            "long_upper_shadow": (False, 0.0),
            "volume_stagnation": (False, 0.0),
        }
        score, details = engine.calculate_technical_score(patterns, "short")
        assert score == 2.0

    def test_做空全部未命中0分(self, engine):
        """做空方向：全部未命中，0分"""
        patterns = {
            "three_tops": (False, 0.0),
            "long_upper_shadow": (False, 0.0),
            "volume_stagnation": (False, 0.0),
        }
        score, details = engine.calculate_technical_score(patterns, "short")
        assert score == 0.0

    def test_做多全部形态命中满分(self, engine):
        """做多方向：三次探底+长下影线+放量止跌，满分10"""
        patterns = {
            "three_bottoms": (True, 4.0),
            "long_lower_shadow": (True, 3.0),
            "volume_reversal": (True, 3.0),
        }
        score, details = engine.calculate_technical_score(patterns, "long")
        assert score == 10.0

    def test_做多缺基础形态(self, engine):
        """做多方向：仅长下影线和放量止跌，缺基础形态"""
        patterns = {
            "three_bottoms": (False, 0.0),
            "long_lower_shadow": (True, 3.0),
            "volume_reversal": (True, 3.0),
        }
        score, details = engine.calculate_technical_score(patterns, "long")
        assert score == 6.0
        assert details["primary_pattern"] is False


class TestSentimentScore:
    """测试情绪面评分"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_做空极高费率满分(self, engine):
        """做空：资金费率 0.002，年化 219%，极高，10分"""
        score, details = engine.calculate_sentiment_score(0.002, "short")
        assert score == 10
        assert details["annualized_rate"] > 150

    def test_做空高费率8分(self, engine):
        """做空：资金费率 0.001，年化 ~109.5%，高，8分"""
        score, details = engine.calculate_sentiment_score(0.001, "short")
        assert score == 8
        assert 100 <= details["annualized_rate"] < 150

    def test_做空中等费率6分(self, engine):
        """做空：资金费率 0.0006，年化 ~65.7%，中等，6分"""
        score, details = engine.calculate_sentiment_score(0.0006, "short")
        assert score == 6

    def test_做空低费率3分(self, engine):
        """做空：资金费率 0.0001，年化 ~10.95%，低，3分"""
        score, details = engine.calculate_sentiment_score(0.0001, "short")
        assert score == 3

    def test_做空负费率1分(self, engine):
        """做空：资金费率 -0.0001，年化 ~-10.95%，负，1分"""
        score, details = engine.calculate_sentiment_score(-0.0001, "short")
        assert score == 1

    def test_做空极端负费率0分(self, engine):
        """做空：资金费率 -0.0003，年化 ~-32.85%，极端负，0分"""
        score, details = engine.calculate_sentiment_score(-0.0003, "short")
        assert score == 0

    def test_做多负费率满分(self, engine):
        """做多：资金费率 -0.0003，年化 -32.85%，极端负，10分"""
        score, details = engine.calculate_sentiment_score(-0.0003, "long")
        assert score == 10

    def test_做多高费率0分(self, engine):
        """做多：高费率对做多不利，0分"""
        score, details = engine.calculate_sentiment_score(0.002, "long")
        assert score == 0

    def test_年化费率计算正确(self, engine):
        """验证年化费率计算公式：费率 × 3 × 365 × 100"""
        score, details = engine.calculate_sentiment_score(0.001, "short")
        expected_annualized = 0.001 * 3 * 365 * 100
        assert details["annualized_rate"] == pytest.approx(expected_annualized, rel=0.01)


class TestFullScoring:
    """测试完整评分流程"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_做空高评分场景(self, engine):
        """做空高评分：高OI/市值比 + 完整形态 + 高费率"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.30,
            patterns={
                "three_tops": (True, 4.0),
                "long_upper_shadow": (True, 3.0),
                "volume_stagnation": (True, 3.0),
            },
            funding_rate=0.002,
            has_market_cap=True,
        )
        assert result.total_score > 7.0
        assert result.contract_score == 10
        assert result.technical_score == 10
        assert result.sentiment_score == 10
        # 加权总分：10*0.25 + 10*0.45 + 10*0.30 = 10.0
        assert result.total_score == 10.0
        assert result.veto is False

    def test_做空低评分场景(self, engine):
        """做空低评分：低OI/市值比 + 无形态 + 负费率"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.02,
            patterns={
                "three_tops": (False, 0.0),
                "long_upper_shadow": (False, 0.0),
                "volume_stagnation": (False, 0.0),
            },
            funding_rate=-0.0003,
            has_market_cap=True,
        )
        assert result.total_score < 3.0
        assert result.contract_score == 0
        assert result.technical_score == 0
        assert result.sentiment_score == 0
        assert result.veto is False

    def test_做多高评分场景(self, engine):
        """做多高评分：低OI/市值比 + 完整做多形态 + 负费率"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="long",
            oi_market_cap_ratio=0.02,
            patterns={
                "three_bottoms": (True, 4.0),
                "long_lower_shadow": (True, 3.0),
                "volume_reversal": (True, 3.0),
            },
            funding_rate=-0.0003,
            has_market_cap=True,
        )
        assert result.total_score == 10.0
        assert result.contract_score == 10
        assert result.technical_score == 10
        assert result.sentiment_score == 10

    def test_评分结果转字典(self, engine):
        """验证 ScoringResult.to_dict() 正确"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.30,
            patterns={
                "three_tops": (True, 4.0),
                "long_upper_shadow": (True, 3.0),
                "volume_stagnation": (True, 3.0),
            },
            funding_rate=0.002,
        )
        d = result.to_dict()
        assert d["symbol"] == "DOGEUSDT"
        assert d["direction"] == "short"
        assert "total_score" in d
        assert "contract_score" in d
        assert "technical_score" in d
        assert "sentiment_score" in d
        assert d["veto"] is False


class TestEntryDecision:
    """测试入场判断"""

    @pytest.fixture
    def engine(self):
        return ScoringEngine(CONFIG)

    def test_满足全部入场条件应入场(self, engine):
        """总分>=6.5，技术总分>=6.0，基础形态>=2.0"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.22,
            patterns={
                "three_tops": (True, 4.0),
                "long_upper_shadow": (True, 3.0),
                "volume_stagnation": (False, 0.0),
            },
            funding_rate=0.001,
        )
        # 合约分: 8, 技术分: 7, 情绪分: 8
        # 总分: 8*0.25 + 7*0.45 + 8*0.30 = 7.55
        assert result.total_score >= 6.5
        assert result.technical_score >= 6.0
        assert engine.should_entry(result) is True

    def test_总分不足不应入场(self, engine):
        """总分低于6.5，不应入场"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.12,
            patterns={
                "three_tops": (True, 2.0),
                "long_upper_shadow": (False, 0.0),
                "volume_stagnation": (False, 0.0),
            },
            funding_rate=0.0001,
        )
        # 合约分: 4, 技术分: 2, 情绪分: 3
        # 总分: 4*0.25 + 2*0.45 + 3*0.30 = 2.8
        assert result.total_score < 6.5
        assert engine.should_entry(result) is False

    def test_技术总分不足不应入场(self, engine):
        """技术总分低于6.0，不应入场"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.30,
            patterns={
                "three_tops": (True, 4.0),
                "long_upper_shadow": (False, 0.0),
                "volume_stagnation": (False, 0.0),
            },
            funding_rate=0.002,
        )
        # 合约分: 10, 技术分: 4, 情绪分: 10
        # 总分: 10*0.25 + 4*0.45 + 10*0.30 = 7.3
        # 但技术总分 4 < 6.0
        assert result.technical_score < 6.0
        assert engine.should_entry(result) is False

    def test_基础形态评分不足不应入场(self, engine):
        """基础形态评分低于2.0，不应入场"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.30,
            patterns={
                "three_tops": (True, 1.0),  # 基础形态评分1.0 < 2.0
                "long_upper_shadow": (True, 3.0),
                "volume_stagnation": (True, 3.0),
            },
            funding_rate=0.002,
        )
        # 技术总分: 7.0 >= 6.0, 但基础形态评分只有1.0 < 2.0
        assert result.technical_score >= 6.0
        assert engine.should_entry(result) is False

    def test_一票否决不应入场(self, engine):
        """一票否决时不应入场"""
        result = engine.score(
            symbol="DOGEUSDT",
            direction="short",
            oi_market_cap_ratio=0.30,
            patterns={
                "three_tops": (True, 4.0),
                "long_upper_shadow": (True, 3.0),
                "volume_stagnation": (True, 3.0),
            },
            funding_rate=0.002,
        )
        result.veto = True
        result.veto_reason = "测试否决"
        assert engine.should_entry(result) is False


class TestPerformance:
    """性能测试：评分引擎吞吐量"""

    def test_评分引擎吞吐量(self):
        """测试评分引擎每秒可处理多少币种"""
        import time
        
        engine = ScoringEngine(CONFIG)
        
        # 模拟数据
        symbols = [f"COIN{i}USDT" for i in range(1000)]
        patterns = {
            "three_tops": (True, 4.0),
            "long_upper_shadow": (True, 3.0),
            "volume_stagnation": (True, 3.0),
        }
        
        start = time.perf_counter()
        for symbol in symbols:
            engine.score(
                symbol=symbol,
                direction="short",
                oi_market_cap_ratio=0.22,
                patterns=patterns,
                funding_rate=0.001,
            )
        elapsed = time.perf_counter() - start
        throughput = len(symbols) / elapsed
        
        print(f"\n评分引擎吞吐量: {throughput:.0f} 币种/秒 (1000个币种耗时 {elapsed*1000:.2f}ms)")
        # 性能基准：至少每秒 5000 个币种
        assert throughput > 5000, f"吞吐量过低: {throughput:.0f} < 5000 币种/秒"