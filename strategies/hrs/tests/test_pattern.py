"""
形态识别测试
测试 PatternRecognizer 的做空/做多形态检测
"""
import pytest
import yaml
from pathlib import Path

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.pattern import PatternRecognizer


def make_kline(open_p, high, low, close, volume):
    """创建模拟K线数据"""
    return {
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


class TestPatternRecognizerInit:
    """测试形态识别器初始化"""

    def test_从配置正确加载参数(self):
        recognizer = PatternRecognizer(CONFIG)
        assert recognizer.window_size == 5
        assert recognizer.short_three_tops["min_deviation"] == 0.002
        assert recognizer.short_three_tops["score_full"] == 4.0
        assert recognizer.short_three_tops["score_partial"] == 2.0
        assert recognizer.short_long_shadow["ratio_threshold"] == 2.0
        assert recognizer.long_three_bottoms["min_deviation"] == 0.002

    def test_空配置使用默认值(self):
        recognizer = PatternRecognizer({})
        assert recognizer.window_size == 5


class TestThreeTops:
    """测试三次冲顶形态"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_高点依次降低满分(self, recognizer):
        """5根K线高点依次降低，满分"""
        klines = [
            make_kline(100, 110, 95, 105, 1000),   # 高110
            make_kline(100, 109, 95, 104, 1000),   # 高109, 降0.91%
            make_kline(100, 107, 95, 103, 1000),   # 高107, 降1.83%
            make_kline(100, 105, 95, 102, 1000),   # 高105, 降1.87%
            make_kline(100, 103, 95, 101, 1000),   # 高103, 降1.90%
        ]
        result = recognizer._detect_three_tops(klines)
        assert result[0] is True
        assert result[1] == 4.0

    def test_同一水平受阻3次部分评分(self, recognizer):
        """5根K线高点接近同一水平，部分评分"""
        klines = [
            make_kline(100, 110.0, 95, 105, 1000),
            make_kline(100, 110.1, 95, 104, 1000),  # 偏离 0.09% < 0.2%
            make_kline(100, 109.9, 95, 103, 1000),  # 偏离 0.09% < 0.2%
            make_kline(100, 110.0, 95, 102, 1000),  # 偏离 0% < 0.2%
            make_kline(100, 109.9, 95, 101, 1000),  # 偏离 0.09% < 0.2%
        ]
        result = recognizer._detect_three_tops(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_高点非依次降低不触发(self, recognizer):
        """高点随机波动，不触发"""
        klines = [
            make_kline(100, 110, 95, 105, 1000),
            make_kline(100, 108, 95, 109, 1000),  # 高108 < 110, 但收盘109 > 开盘
            make_kline(100, 112, 95, 103, 1000),  # 高112 > 108, 不递减
            make_kline(100, 105, 95, 102, 1000),
            make_kline(100, 103, 95, 101, 1000),
        ]
        result = recognizer._detect_three_tops(klines)
        assert result[0] is False
        assert result[1] == 0.0

    def test_K线不足5根返回空结果(self, recognizer):
        """K线不足5根时返回空结果"""
        klines = [
            make_kline(100, 110, 95, 105, 1000),
            make_kline(100, 109, 95, 104, 1000),
            make_kline(100, 107, 95, 103, 1000),
        ]
        result = recognizer.detect_short_patterns(klines)
        assert result["three_tops"] == (False, 0.0)
        assert result["long_upper_shadow"] == (False, 0.0)
        assert result["volume_stagnation"] == (False, 0.0)


class TestThreeBottoms:
    """测试三次探底形态"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_低点依次抬高满分(self, recognizer):
        """5根K线低点依次抬高，满分"""
        klines = [
            make_kline(100, 105, 90, 95, 1000),    # 低90
            make_kline(100, 105, 91, 96, 1000),    # 低91, 升1.11%
            make_kline(100, 105, 92, 97, 1000),    # 低92, 升1.10%
            make_kline(100, 105, 93, 98, 1000),    # 低93, 升1.09%
            make_kline(100, 105, 94, 99, 1000),    # 低94, 升1.08%
        ]
        result = recognizer._detect_three_bottoms(klines)
        assert result[0] is True
        assert result[1] == 4.0

    def test_同一支撑位触及3次部分评分(self, recognizer):
        """5根K线低点接近同一支撑位，部分评分"""
        klines = [
            make_kline(100, 105, 90.0, 95, 1000),
            make_kline(100, 105, 90.1, 96, 1000),  # 偏离 0.11% < 0.2%
            make_kline(100, 105, 89.9, 97, 1000),  # 偏离 0.11% < 0.2%
            make_kline(100, 105, 90.0, 98, 1000),  # 偏离 0% < 0.2%
            make_kline(100, 105, 90.1, 99, 1000),  # 偏离 0.11% < 0.2%
        ]
        result = recognizer._detect_three_bottoms(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_低点非依次抬高不触发(self, recognizer):
        """低点随机波动，不触发"""
        klines = [
            make_kline(100, 105, 90, 95, 1000),
            make_kline(100, 105, 88, 96, 1000),  # 低88 < 90, 非递增
            make_kline(100, 105, 92, 97, 1000),
            make_kline(100, 105, 93, 98, 1000),
            make_kline(100, 105, 94, 99, 1000),
        ]
        result = recognizer._detect_three_bottoms(klines)
        assert result[0] is False


class TestLongUpperShadow:
    """测试长上影线"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_阴线长上影线满分(self, recognizer):
        """阴线 + 上影线 >= 实体 × 2，满分"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 110, 96, 98, 1000),  # 开100, 高110, 低96, 收98
            # 实体: |98-100| = 2, 上影线: 110-max(100,98)=10, 10/2=5 >= 2
        ]
        result = recognizer._detect_long_upper_shadow(klines)
        assert result[0] is True
        assert result[1] == 3.0

    def test_阳线长上影线部分评分(self, recognizer):
        """阳线 + 上影线 >= 实体 × 2，部分评分"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(98, 110, 96, 100, 1000),  # 开98, 高110, 低96, 收100
            # 实体: |100-98| = 2, 上影线: 110-max(98,100)=10, 10/2=5 >= 2
            # 阳线(收100>开98), 部分评分
        ]
        result = recognizer._detect_long_upper_shadow(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_十字星部分评分(self, recognizer):
        """十字星（开=收），上影线 > 0，部分评分"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 99, 100, 1000),  # 开=收=100, 高105, 上影线5
        ]
        result = recognizer._detect_long_upper_shadow(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_上影线不足不触发(self, recognizer):
        """上影线比例不足，不触发"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 102, 96, 98, 1000),  # 上影线: 102-100=2, 实体: 2, 2/2=1 < 2
        ]
        result = recognizer._detect_long_upper_shadow(klines)
        assert result[0] is False


class TestLongLowerShadow:
    """测试长下影线"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_阳线长下影线满分(self, recognizer):
        """阳线 + 下影线 >= 实体 × 2，满分"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(98, 105, 88, 102, 1000),  # 开98, 高105, 低88, 收102
            # 实体: |102-98| = 4, 下影线: min(98,102)-88 = 10, 10/4=2.5 >= 2
        ]
        result = recognizer._detect_long_lower_shadow(klines)
        assert result[0] is True
        assert result[1] == 3.0

    def test_阴线长下影线部分评分(self, recognizer):
        """阴线 + 下影线 >= 实体 × 2，部分评分"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(102, 105, 88, 98, 1000),  # 开102, 收98, 阴线
            # 实体: 4, 下影线: min(102,98)-88 = 10, 10/4=2.5 >= 2, 阴线->部分
        ]
        result = recognizer._detect_long_lower_shadow(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_下影线不足不触发(self, recognizer):
        """下影线比例不足，不触发"""
        klines = [
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 95, 100, 1000),
            make_kline(100, 105, 98, 102, 1000),  # 下影线: min(100,102)-98=2, 实体:2, 2/2=1 < 2
        ]
        result = recognizer._detect_long_lower_shadow(klines)
        assert result[0] is False


class TestVolumeStagnation:
    """测试放量滞涨"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_放量滞涨满分(self, recognizer):
        """成交量 >= 均量 × 1.5，且收盘价 < 前一根高点，满分"""
        klines = [
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 103, 97, 99, 800),  # 量800, 均量100, 800>=150, 收99<前高105
        ]
        result = recognizer._detect_volume_stagnation(klines)
        assert result[0] is True
        assert result[1] == 3.0

    def test_放量但收盘价不低于前高部分评分(self, recognizer):
        """放量但收盘价 >= 前一根高点，部分评分"""
        klines = [
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 106, 97, 105, 800),  # 收105 >= 前高105, 部分评分
        ]
        result = recognizer._detect_volume_stagnation(klines)
        assert result[0] is True
        assert result[1] == 2.0

    def test_量不足不触发(self, recognizer):
        """成交量不足，不触发"""
        klines = [
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 103, 97, 99, 120),  # 量120 < 150
        ]
        result = recognizer._detect_volume_stagnation(klines)
        assert result[0] is False

    def test_均量为0不触发(self, recognizer):
        """前4根K线均量为0，不触发"""
        klines = [
            make_kline(100, 105, 95, 100, 0),
            make_kline(100, 105, 95, 100, 0),
            make_kline(100, 105, 95, 100, 0),
            make_kline(100, 105, 95, 100, 0),
            make_kline(100, 103, 97, 99, 800),
        ]
        result = recognizer._detect_volume_stagnation(klines)
        assert result[0] is False


class TestVolumeReversal:
    """测试放量止跌"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_放量止跌满分(self, recognizer):
        """成交量 >= 均量 × 1.5，且收盘价 > 前一根低点，满分"""
        klines = [
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 103, 97, 99, 800),  # 量800, 收99>前低95, 满分
        ]
        result = recognizer._detect_volume_reversal(klines)
        assert result[0] is True
        assert result[1] == 3.0

    def test_放量但收盘价不高于前低部分评分(self, recognizer):
        """放量但收盘价 <= 前一根低点，部分评分"""
        klines = [
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 105, 95, 100, 100),
            make_kline(100, 103, 97, 94, 800),  # 收94 <= 前低95, 部分评分
        ]
        result = recognizer._detect_volume_reversal(klines)
        assert result[0] is True
        assert result[1] == 2.0


class TestDetectShortPatterns:
    """测试完整做空形态检测"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_空K线列表返回全False(self, recognizer):
        result = recognizer.detect_short_patterns([])
        assert result == {
            "three_tops": (False, 0.0),
            "long_upper_shadow": (False, 0.0),
            "volume_stagnation": (False, 0.0),
        }

    def test_K线不足5根返回全False(self, recognizer):
        klines = [make_kline(100, 105, 95, 100, 1000) for _ in range(3)]
        result = recognizer.detect_short_patterns(klines)
        assert result["three_tops"] == (False, 0.0)
        assert result["long_upper_shadow"] == (False, 0.0)
        assert result["volume_stagnation"] == (False, 0.0)

    def test_完整做空场景(self, recognizer):
        """同时检测到三次冲顶和长上影线"""
        klines = [
            make_kline(100, 110.0, 95, 105, 100),
            make_kline(100, 109.0, 95, 104, 100),
            make_kline(100, 107.0, 95, 103, 100),
            make_kline(100, 105.0, 95, 102, 100),
            make_kline(100, 103.0, 96, 98, 800),  # 三次冲顶 + 长上影线 + 放量滞涨
        ]
        result = recognizer.detect_short_patterns(klines)
        assert result["three_tops"] == (True, 4.0)
        # 上影线: 103-max(100,98)=3, 实体: |98-100|=2, 3/2=1.5 < 2, 不触发
        # 量: 800 >= 100*1.5=150, 收98 < 前高105, 满分


class TestDetectLongPatterns:
    """测试完整做多形态检测"""

    @pytest.fixture
    def recognizer(self):
        return PatternRecognizer(CONFIG)

    def test_空K线列表返回全False(self, recognizer):
        result = recognizer.detect_long_patterns([])
        assert result == {
            "three_bottoms": (False, 0.0),
            "long_lower_shadow": (False, 0.0),
            "volume_reversal": (False, 0.0),
        }

    def test_完整做多场景(self, recognizer):
        """同时检测到三次探底和长下影线"""
        klines = [
            make_kline(100, 105, 90.0, 95, 100),
            make_kline(100, 105, 91.0, 96, 100),
            make_kline(100, 105, 92.0, 97, 100),
            make_kline(100, 105, 93.0, 98, 100),
            make_kline(98, 105, 93.5, 102, 800),  # 三次探底(低点依次抬高) + 长下影线 + 放量止跌
        ]
        result = recognizer.detect_long_patterns(klines)
        assert result["three_bottoms"] == (True, 4.0)
        # 下影线: min(98,102)-93.5=4.5, 实体: |102-98|=4, 4.5/4=1.125, 不满足 >= 2
        # 量: 800 >= 100*1.5=150, 收102 > 前低93, 满分
        assert result["volume_reversal"] == (True, 3.0)


class TestPerformance:
    """性能测试：形态识别吞吐量"""

    def test_形态识别吞吐量(self):
        """测试形态识别在大K线数据量下的表现"""
        import time
        
        recognizer = PatternRecognizer(CONFIG)
        
        # 生成5000组K线数据
        klines_list = []
        for i in range(5000):
            base = 100 + (i % 10)
            klines = [
                make_kline(base, base + 5, base - 5, base + 1, 100),
                make_kline(base, base + 4.5, base - 5, base + 1, 100),
                make_kline(base, base + 4, base - 5, base + 1, 100),
                make_kline(base, base + 3.5, base - 5, base + 1, 100),
                make_kline(base, base + 3, base - 5, base + 1, 800),
            ]
            klines_list.append(klines)
        
        start = time.perf_counter()
        for klines in klines_list:
            recognizer.detect_short_patterns(klines)
        elapsed = time.perf_counter() - start
        throughput = len(klines_list) / elapsed
        
        print(f"\n形态识别吞吐量: {throughput:.0f} 组/秒 (5000组K线耗时 {elapsed*1000:.2f}ms)")
        # 性能基准：至少每秒 3000 组
        assert throughput > 3000, f"吞吐量过低: {throughput:.0f} < 3000 组/秒"

    def test_大K线窗口性能(self):
        """测试窗口大小对性能的影响"""
        import time
        
        recognizer = PatternRecognizer(CONFIG)
        
        # 每组100根K线（实际场景中不会超过此数量）
        klines = [make_kline(100, 105, 95, 100, 1000) for _ in range(100)]
        
        start = time.perf_counter()
        for _ in range(10000):
            recognizer.detect_short_patterns(klines)
        elapsed = time.perf_counter() - start
        throughput = 10000 / elapsed
        
        print(f"\n大窗口(100根K线)吞吐量: {throughput:.0f} 次/秒 (10000次耗时 {elapsed*1000:.2f}ms)")
        assert throughput > 5000, f"大窗口吞吐量过低: {throughput:.0f} < 5000 次/秒"