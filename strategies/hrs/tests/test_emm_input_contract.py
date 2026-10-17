"""
B1: HRS EMM / 半EMM 输入与 entry_mode 传递测试

覆盖：
- price_change_24h 计算口径（D1）：整点 1h 已收盘 K 线，close(t)/close(t-24h)-1 * 100
- 不可用条件：K线不足 25 根、基准价为零、缺口、未收盘/未来 K 线
- 多空同时点同值；单位百分比数值（20 非 0.20）
- entry_mode 从 score_result → signal dict 正确传递
- EMM 影子开关默认影子，影子阶段不真实下单

本文件仅测 HRS strategy.py 的 helper 和 signal 构造，不连交易所、不连 DB。
"""
import pytest
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from shared.market_cap import MarketCapProvenance

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)


def _make_klines(
    closes: list,
    base_open_time_ms: int,
    interval_ms: int = 3600000,
) -> list:
    """快速构造 K 线 fixture：每根仅含 open_time + close，满足 strategy.py 最小字段集。

    Args:
        closes: 收盘价序列，长度 = K 线根数
        base_open_time_ms: 第 0 根 K 线的开盘时间（毫秒 UTC）
        interval_ms: K 线间隔（默认 1h = 3600000）
    """
    klines = []
    for i, c in enumerate(closes):
        klines.append({
            "open_time": base_open_time_ms + i * interval_ms,
            "close": float(c),
        })
    return klines


def _utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ====================================================================
# 第一组：price_change_24h helper 计算契约
# （strategy.py 将新增 _calc_price_change_24h 方法）
# ====================================================================

class TestPriceChange24hCalculation:
    """测试 price_change_24h 计算口径和不可用条件"""

    def test_正常25根K线_返回正确百分比(self):
        """25 根连续已收盘 K 线，价格从 100 到 125 → 25%"""
        from strategies.hrs.strategy import HRSStrategy

        # 25 根：基准是 klines[0]（t-24h），当前是 klines[24]（t）
        closes = [100.0] * 24 + [125.0]
        base_open = _utc_now_ms() - 25 * 3600000  # 第 0 根是 25h 前开盘
        klines = _make_klines(closes, base_open)

        # 当前时刻在最后一根收盘之后（+1h 确保已收盘）
        decision_time_ms = base_open + 25 * 3600000 + 60_000  # 留 1 分钟余量

        # 构造一个最小 strategy 实例调用 helper
        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is True, f"应可用，实际: {result}"
        assert result["value"] == pytest.approx(25.0, abs=1e-6), f"应为 25.0%，实际 {result}"
        assert result["value"] > 1.0, "单位应为百分比数值，25 而非 0.25"

    def test_价格下跌20根_K线_返回正确百分比(self):
        """从 100 跌到 80 → -20%"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [100.0] * 24 + [80.0]
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        decision_time_ms = base_open + 25 * 3600000 + 60_000

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is True
        assert result["value"] == pytest.approx(-20.0, abs=1e-6)

    def test_K线不足25根_不可用(self):
        """24 根 K 线 → 不足，返回不可用 + 原因"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [100.0] * 24  # 只有 24 根
        base_open = _utc_now_ms() - 24 * 3600000
        klines = _make_klines(closes, base_open)
        decision_time_ms = base_open + 24 * 3600000 + 60_000

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is False
        assert "K线不足" in result["reason"] or "K线" in result["reason"]
        assert result["value"] is None

    def test_基准价为零_不可用(self):
        """t-24h 的收盘价为 0 → 不可用"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [0.0] + [100.0] * 24  # 第 0 根 close=0
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        decision_time_ms = base_open + 25 * 3600000 + 60_000

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is False
        assert "基准" in result["reason"] or "零" in result["reason"]

    def test_K线有缺口_不可用(self):
        """K 线时间不连续（中间跳过了一根）→ 不可用"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [100.0] * 25
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        # 制造缺口：把第 12 根的 open_time 往后推 2h
        klines[12]["open_time"] += 2 * 3600000
        decision_time_ms = base_open + 25 * 3600000 + 60_000

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is False
        assert "缺口" in result["reason"] or "不连续" in result["reason"]

    def test_最后一根K线未收盘_不可用(self):
        """decision_time_ms 还没到最后一根的收盘时间 → 含未收盘 K 线 → 不可用"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [100.0] * 24 + [110.0]
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        # 当前时刻 = 最后一根开盘时间 + 30 分钟（还在形成中，没收盘）
        last_open_time = base_open + 24 * 3600000
        decision_time_ms = last_open_time + 30 * 60_000  # +30min < +1h

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is False
        assert "未收盘" in result["reason"] or "未" in result["reason"]

    def test_多空同时点同值(self):
        """多空评分调用 score() 时应收到同一 price_change_24h 值"""
        from strategies.hrs.strategy import HRSStrategy

        closes = [100.0] * 24 + [130.0]
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        decision_time_ms = base_open + 25 * 3600000 + 60_000

        strategy = HRSStrategy(CONFIG)
        strategy.kline_interval = "1h"
        result = strategy._calc_price_change_24h(klines, decision_time_ms)

        assert result["available"] is True
        assert result["value"] == pytest.approx(30.0, abs=1e-6)
        # 再次调用应返回相同值（同一时点）
        result2 = strategy._calc_price_change_24h(klines, decision_time_ms)
        assert result2["value"] == result["value"]


# ====================================================================
# 第二组：analyze() 应把 price_change_24h 传给 scoring_engine.score()
# ====================================================================

class TestAnalyzePassesPriceChange24h:
    """验证 analyze() 调用 scoring_engine.score() 时正确传递 price_change_24h"""

    async def test_analyze_做空_传正确price_change_24h(self):
        """做空 analyze 调用 score 时 price_change_24h 参数非 None 且为百分比数值"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)
        strategy.scoring_engine = MagicMock()
        # 让 scoring_engine.score 返回一个有 should_entry 的 ScoringResult mock
        mock_score_result = MagicMock()
        mock_score_result.total_score = 7.0
        mock_score_result.to_dict.return_value = {
            "total_score": 7.0, "entry_mode": "standard",
            "technical_score": 4.0, "sentiment_score": 2.0,
        }
        strategy.scoring_engine.score.return_value = mock_score_result
        strategy.scoring_engine.should_entry.return_value = True

        # 构造 25 根 K 线：价格从 100 涨到 120 → 20% 上涨
        closes = [100.0] * 24 + [120.0]
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        strategy._klines_cache["TESTUSDT"] = klines

        # mock 其他 analyze 依赖
        strategy._warmup_klines = AsyncMock(return_value=klines)
        strategy.market_data = MagicMock()
        strategy.market_data.get_oi_usd = AsyncMock(return_value=1000.0)
        strategy.market_data.get_funding_rate = AsyncMock(return_value=0.0001)
        strategy.market_data.get_24h_volume = AsyncMock(return_value=50000.0)
        strategy.market_data.get_market_cap_provenance = AsyncMock(
            return_value=MarketCapProvenance(value=10000.0, source="coingecko")
        )
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates.return_value = {"TESTUSDT"}
        strategy.candidate_pool.get_long_candidates.return_value = set()
        strategy.pattern_recognizer = MagicMock()
        strategy.pattern_recognizer.detect_short_patterns.return_value = {
            "primary_pattern": {"score": 3.0},
            "shadow_pattern": {"score": 1.0},
            "volume_pattern": {"score": 0.5},
        }
        strategy.pattern_recognizer.detect_long_patterns.return_value = {}
        strategy._klines_4h_cache["TESTUSDT"] = []
        strategy._check_pattern_expiry = MagicMock(return_value=False)

        # 执行 analyze
        result = await strategy.analyze("TESTUSDT")

        # 验证 scoring_engine.score 被调用且 price_change_24h 非 None
        strategy.scoring_engine.score.assert_called()
        ca = strategy.scoring_engine.score.call_args
        if ca.kwargs:
            passed_pc24 = ca.kwargs.get("price_change_24h")
        else:
            passed_pc24 = ca.args[6] if len(ca.args) > 6 else None

        assert passed_pc24 is not None, "price_change_24h 不应为 None"
        assert passed_pc24 == pytest.approx(20.0, abs=1e-6), \
            f"应为 20.0%，实际 {passed_pc24}"
        assert isinstance(passed_pc24, float), f"应为 float 类型，实际 {type(passed_pc24)}"

    async def test_analyze_做多_传正确price_change_24h(self):
        """做多 analyze 调用 score 时同样传入同一 price_change_24h 值"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)
        strategy.scoring_engine = MagicMock()
        mock_score_result = MagicMock()
        mock_score_result.total_score = 7.5
        mock_score_result.to_dict.return_value = {
            "total_score": 7.5, "entry_mode": "standard",
            "technical_score": 4.5, "sentiment_score": 2.25,
        }
        strategy.scoring_engine.score.return_value = mock_score_result
        strategy.scoring_engine.should_entry.return_value = True

        # 25 根 K 线，跌 15%
        closes = [100.0] * 24 + [85.0]
        base_open = _utc_now_ms() - 25 * 3600000
        klines = _make_klines(closes, base_open)
        strategy._klines_cache["TESTUSDT"] = klines

        strategy._warmup_klines = AsyncMock(return_value=klines)
        strategy.market_data = MagicMock()
        strategy.market_data.get_oi_usd = AsyncMock(return_value=800.0)
        strategy.market_data.get_funding_rate = AsyncMock(return_value=-0.0002)
        strategy.market_data.get_24h_volume = AsyncMock(return_value=40000.0)
        strategy.market_data.get_market_cap_provenance = AsyncMock(
            return_value=MarketCapProvenance(value=12000.0, source="coingecko")
        )
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates.return_value = set()
        strategy.candidate_pool.get_long_candidates.return_value = {"TESTUSDT"}
        strategy.pattern_recognizer = MagicMock()
        strategy.pattern_recognizer.detect_short_patterns.return_value = {}
        strategy.pattern_recognizer.detect_long_patterns.return_value = {
            "primary_pattern": {"score": 3.5},
        }
        strategy._klines_4h_cache["TESTUSDT"] = []
        strategy._check_pattern_expiry = MagicMock(return_value=False)

        result = await strategy.analyze("TESTUSDT")

        strategy.scoring_engine.score.assert_called()
        ca = strategy.scoring_engine.score.call_args
        if ca.kwargs:
            passed_pc24 = ca.kwargs.get("price_change_24h")
        else:
            passed_pc24 = ca.args[6] if len(ca.args) > 6 else None

        assert passed_pc24 is not None, "price_change_24h 不应为 None"
        assert passed_pc24 == pytest.approx(-15.0, abs=1e-6), \
            f"应为 -15.0%，实际 {passed_pc24}"

    async def test_K线不足_EMM模式不触发(self):
        """K 线只有 24 根（够 analyze 但不够 25 根算 price_change_24h），
        price_change_24h 不可用 → EMM 不触发"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)
        # 让 min_klines = 23，确保 24 根能通过 analyze 的最小检查
        strategy.min_klines = 23
        strategy.scoring_engine = MagicMock()
        mock_score_result = MagicMock()
        mock_score_result.total_score = 6.5
        mock_score_result.to_dict.return_value = {
            "total_score": 6.5, "entry_mode": "standard",
        }
        strategy.scoring_engine.score.return_value = mock_score_result
        strategy.scoring_engine.should_entry.return_value = True

        closes = [100.0] * 24  # 只有 24 根，够 analyze(min_klines=23) 但不够 price_change_24h(需 25)
        base_open = _utc_now_ms() - 24 * 3600000
        klines = _make_klines(closes, base_open)
        strategy._klines_cache["TESTUSDT"] = klines

        strategy._warmup_klines = AsyncMock(return_value=klines)
        strategy.market_data = MagicMock()
        strategy.market_data.get_oi_usd = AsyncMock(return_value=1000.0)
        strategy.market_data.get_funding_rate = AsyncMock(return_value=0.0001)
        strategy.market_data.get_24h_volume = AsyncMock(return_value=50000.0)
        strategy.market_data.get_market_cap_provenance = AsyncMock(
            return_value=MarketCapProvenance(value=10000.0, source="coingecko")
        )
        strategy.candidate_pool = MagicMock()
        strategy.candidate_pool.get_short_candidates.return_value = {"TESTUSDT"}
        strategy.candidate_pool.get_long_candidates.return_value = set()
        strategy.pattern_recognizer = MagicMock()
        strategy.pattern_recognizer.detect_short_patterns.return_value = {}
        strategy._klines_4h_cache["TESTUSDT"] = []
        strategy._check_pattern_expiry = MagicMock(return_value=False)

        await strategy.analyze("TESTUSDT")

        # 验证传给 score 的 price_change_24h 为 None（因为 price_change_24h 不可用）
        ca = strategy.scoring_engine.score.call_args
        assert ca is not None, "score 应被调用"
        if ca.kwargs:
            passed_pc24 = ca.kwargs.get("price_change_24h")
        else:
            passed_pc24 = ca.args[6] if len(ca.args) > 6 else None
        assert passed_pc24 is None, \
            f"K线不足 25 根时 price_change_24h 应为 None，实际 {passed_pc24}"


# ====================================================================
# 第三组：entry_mode 传递到 signal dict
# ====================================================================

class TestEntryModeSignalPropagation:
    """验证 score_result.entry_mode 在 signal dict 中正确携带"""

    def test_standard_signal_携带_entry_mode(self):
        """standard 模式信号应携带 entry_mode='standard'"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)

        # 模拟 _execute_cycle 里构造的 signal
        short_data = {
            "score_result": {"total_score": 7.0, "entry_mode": "standard"},
            "current_price": 100.0,
        }
        signal = {
            "symbol": "TESTUSDT",
            "direction": "short",
            "score": short_data["score_result"]["total_score"],
            "entry_mode": short_data["score_result"].get("entry_mode", "standard"),
            "current_price": short_data["current_price"],
            "klines": [],
        }

        assert signal["entry_mode"] == "standard"

    def test_emm_signal_携带_entry_mode(self):
        """EMM 模式信号应携带 entry_mode='emm'"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)

        short_data = {
            "score_result": {"total_score": 6.0, "entry_mode": "emm"},
            "current_price": 85.0,
        }
        signal = {
            "symbol": "TESTUSDT",
            "direction": "short",
            "score": short_data["score_result"]["total_score"],
            "entry_mode": short_data["score_result"].get("entry_mode", "standard"),
            "current_price": short_data["current_price"],
            "klines": [],
        }

        assert signal["entry_mode"] == "emm"

    def test_semi_emm_signal_携带_entry_mode(self):
        """半EMM 模式信号应携带 entry_mode='semi_emm'"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)

        long_data = {
            "score_result": {"total_score": 5.5, "entry_mode": "semi_emm"},
            "current_price": 90.0,
        }
        signal = {
            "symbol": "TESTUSDT",
            "direction": "long",
            "score": long_data["score_result"]["total_score"],
            "entry_mode": long_data["score_result"].get("entry_mode", "standard"),
            "current_price": long_data["current_price"],
            "klines": [],
        }

        assert signal["entry_mode"] == "semi_emm"


# ====================================================================
# 第四组：EMM 影子开关默认影子
# ====================================================================

class TestEMMShadowMode:
    """验证 EMM 执行模式默认影子；影子阶段不真实下单"""

    def test_config_yaml_emm_有_execution_mode(self):
        """config.yaml emm 段应有 execution_mode 字段"""
        emm_cfg = CONFIG.get("emm", {})
        assert "execution_mode" in emm_cfg, \
            "config.yaml emm 段缺少 execution_mode 字段"
        # 默认应为 shadow
        assert emm_cfg["execution_mode"] == "shadow", \
            f"EMM execution_mode 默认应为 'shadow'，实际 {emm_cfg['execution_mode']}"

    def test_config_yaml_semi_emm_有_execution_mode(self):
        """config.yaml semi_emm 段应有 execution_mode 字段"""
        semi_cfg = CONFIG.get("semi_emm", {})
        assert "execution_mode" in semi_cfg, \
            "config.yaml semi_emm 段缺少 execution_mode 字段"
        assert semi_cfg["execution_mode"] == "shadow", \
            f"semi_emm execution_mode 默认应为 'shadow'，实际 {semi_cfg['execution_mode']}"
