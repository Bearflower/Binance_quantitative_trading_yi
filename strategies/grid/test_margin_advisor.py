"""
V2.5 多因子保证金智能引导系统单元测试

覆盖范围：
  1. 五因子系数分段边界（状态 / 趋势强度 / 波动率 / BTC一致性 / 资金费率）
  2. 公式综合示例（compute_advice 全链路，建议保证金取整）
  3. 动作触发 / 限幅 / 冷却（含反向立即推送、清仓与减码不同方向）
  4. 推送模板（format_section 加码示例 / CLEAR / 当前无保证金）
  5. 降级路径（BTC 检测异常 / 基准ATR失败 / 费率异常）
  6. BTC 背离检测（假突破 / 假跌破 / 非趋势态跳过 / 异常）
  7. BTC 独立 detector 不污染（实例状态隔离）
  8. signal_bot 集成（板块拼装 / 基准ATR复用 / 趋势态警报追加背离行）

测试风格与 strategies/grid/test_cooldown_v22.py 保持一致（unittest + mock）。
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import yaml

# 将项目根目录加入 sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, PROJECT_ROOT)

from strategies.grid.margin_advisor import (
    MarginAdvisor, MarginAdvice, ACTION_ADD, ACTION_REDUCE, ACTION_CLEAR, ACTION_NONE,
)
from strategies.grid.market_state import MarketState, MarketAnalysis


def _load_config() -> dict:
    """加载真实 config.yaml（验证配置结构完整性）"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
    with open(config_path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def _make_klines(count: int, close: float, high: float = None, low: float = None) -> list:
    """生成 count 根 K 线（open/high/low/close 结构）"""
    high = high if high is not None else close * 1.01
    low = low if low is not None else close * 0.99
    return [
        {'open': close, 'high': high, 'low': low, 'close': close, 'volume': 100.0}
        for _ in range(count)
    ]


def _make_eth_analysis(
    state: MarketState,
    adx_1h: Decimal = Decimal('16.5'),
    atr_smooth: Decimal = Decimal('50'),
    ema20: Decimal = Decimal('2500'),
    ema50: Decimal = Decimal('2480'),
    current_price: Decimal = Decimal('2500')
) -> MarketAnalysis:
    """构建测试用 ETH MarketAnalysis"""
    return MarketAnalysis(
        state=state,
        trend_strength=Decimal('0.1'),
        adx_1h=adx_1h,
        adx_4h=Decimal('20'),
        ema20_1h=ema20,
        ema50_1h=ema50,
        current_price=current_price,
        atr_smooth=atr_smooth,
        confidence=Decimal('0.7'),
        ema20_4h=ema20,
        ema50_4h=ema50,
        atr_2h_ago=Decimal('45'),
        atr_abnormal_count=0,
        atr_peak=Decimal('0'),
        is_volatility_alarm_active=False
    )


def _make_mock_kline_service(baseline_klines=None, eth_div_klines=None, btc_div_klines=None):
    """
    创建 mock KLineService。

    - 1d 调用：返回 baseline_klines（供基准ATR，内容无关紧要，计算器被 mock）
    - 1h 调用：按 symbol 返回背离检测 K 线
    """
    mock = MagicMock()
    baseline_klines = baseline_klines if baseline_klines is not None else _make_klines(100, close=3000)

    async def _get_klines(symbol: str, interval: str, limit: int = 100):
        if interval == '1d':
            return baseline_klines
        if interval == '1h':
            if symbol == 'BTCUSDT':
                return btc_div_klines if btc_div_klines is not None else _make_klines(
                    24, close=69000, high=70000, low=68000)
            return eth_div_klines if eth_div_klines is not None else _make_klines(
                24, close=3000, high=3100, low=2900)
        return []

    mock.get_klines = AsyncMock(side_effect=_get_klines)
    return mock


def _make_btc_analysis(
    state: MarketState,
    ema20: Decimal = Decimal('69000'),
    ema50: Decimal = Decimal('68000')
) -> MarketAnalysis:
    """构建测试用 BTC MarketAnalysis"""
    return _make_eth_analysis(
        state=state, adx_1h=Decimal('30'),
        atr_smooth=Decimal('800'), ema20=ema20, ema50=ema50,
        current_price=Decimal('69000')
    )


def _make_advisor(
    config: dict,
    btc_analysis: MarketAnalysis = None,
    funding_rate: float = 0.0001,
    baseline_atr: Decimal = Decimal('100'),
    btc_detect_raises: bool = False,
    funding_raises: bool = False
) -> MarginAdvisor:
    """构建 MarginAdvisor（mock binance/kline/btc_detector/grid_calculator）"""
    kline_service = _make_mock_kline_service()

    binance_client = MagicMock()
    if funding_raises:
        binance_client.get_funding_rate = AsyncMock(side_effect=Exception("费率接口故障"))
    else:
        binance_client.get_funding_rate = AsyncMock(return_value=funding_rate)

    btc_detector = MagicMock()
    if btc_detect_raises:
        btc_detector.detect_market_state = AsyncMock(side_effect=Exception("BTC检测故障"))
    else:
        btc_detector.detect_market_state = AsyncMock(
            return_value=btc_analysis if btc_analysis is not None else _make_btc_analysis(MarketState.OSCILLATION)
        )

    grid_calculator = MagicMock()
    grid_calculator.calculate_baseline_atr = MagicMock(return_value=baseline_atr)

    return MarginAdvisor(
        config=config,
        binance_client=binance_client,
        kline_service=kline_service,
        btc_detector=btc_detector,
        grid_calculator=grid_calculator
    )


def _run(coro):
    """同步执行异步协程（unittest 风格）"""
    return asyncio.run(coro)


class TestStateCoeff(unittest.TestCase):
    """状态系数按 MarketState 枚举映射 + 震荡 ADX 细分"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config())

    def test_market_state_mapping(self):
        """枚举映射：紧急/极端/危险态->0，普通强趋势->0.3，弱趋势->0.6"""
        cases = [
            (MarketState.PRICE_EMERGENCY, Decimal('0')),
            (MarketState.EXTREME_STRONG_TREND, Decimal('0')),
            (MarketState.NORMAL_STRONG_TREND, Decimal('0.3')),
            (MarketState.WEAK_TREND, Decimal('0.6')),
            (MarketState.EARLY_WARNING_15M, Decimal('0')),
            (MarketState.TREND_CONFIRMED_1H, Decimal('0')),
            (MarketState.TREND_ACCELERATING, Decimal('0')),
            (MarketState.VOLATILITY_ABNORMAL, Decimal('0')),
        ]
        for state, expected in cases:
            with self.subTest(state=state):
                result = self.advisor._state_coeff(state, Decimal('30'))
                self.assertEqual(result, expected, f"state={state} 期望={expected} 实际={result}")

    def test_oscillation_adx_strong(self):
        """震荡且 ADX<18 -> 1.2（强震荡，加码）"""
        result = self.advisor._state_coeff(MarketState.OSCILLATION, Decimal('17.99'))
        self.assertEqual(result, Decimal('1.2'))

    def test_oscillation_adx_normal(self):
        """震荡且 ADX>=18 -> 1.0"""
        result = self.advisor._state_coeff(MarketState.OSCILLATION, Decimal('18'))
        self.assertEqual(result, Decimal('1.0'))


class TestTrendStrengthCoeff(unittest.TestCase):
    """趋势强度系数分段"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config())

    def test_segments(self):
        """10->1.10 / 20->1.0 / 25->0.90 / 30->0.5 / 50->0.0 / 90->0.0"""
        cases = [
            (Decimal('10'), Decimal('1.1')),
            (Decimal('20'), Decimal('1.0')),
            (Decimal('25'), Decimal('0.9')),
            (Decimal('30'), Decimal('0.5')),
            (Decimal('50'), Decimal('0.0')),
            (Decimal('90'), Decimal('0.0')),
        ]
        for adx, expected in cases:
            with self.subTest(adx=adx):
                result = self.advisor._trend_strength_coeff(adx)
                self.assertEqual(result, expected, f"adx={adx} 期望={expected} 实际={result}")


class TestVolatilityCoeff(unittest.TestCase):
    """波动率系数：clamp / degrade"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config())

    def test_clamp_upper(self):
        """基准/当前=2.0 超过 max_coeff=1.5 -> 1.5"""
        self.assertEqual(self.advisor._volatility_coeff(Decimal('100'), Decimal('50')), Decimal('1.5'))

    def test_clamp_lower(self):
        """基准/当前=0.5 达到 min_coeff=0.5 -> 0.5"""
        self.assertEqual(self.advisor._volatility_coeff(Decimal('50'), Decimal('100')), Decimal('0.5'))

    def test_normal_ratio(self):
        """基准/当前=1.3333 -> 保留4位小数"""
        self.assertEqual(self.advisor._volatility_coeff(Decimal('80'), Decimal('60')), Decimal('1.3333'))

    def test_current_atr_zero_degrades(self):
        """当前 ATR<=0 -> degrade(1.0)"""
        self.assertEqual(self.advisor._volatility_coeff(Decimal('100'), Decimal('0')), Decimal('1.0'))

    def test_baseline_none_degrades(self):
        """基准ATR 计算失败 -> 1.0"""
        self.assertEqual(self.advisor._volatility_coeff(None, Decimal('50')), Decimal('1.0'))


class TestBtcConsistencyCoeff(unittest.TestCase):
    """BTC 一致性系数"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config())

    def test_btc_oscillation(self):
        """BTC 震荡 -> 0.7"""
        eth = _make_eth_analysis(MarketState.WEAK_TREND)
        btc = _make_btc_analysis(MarketState.OSCILLATION)
        self.assertEqual(self.advisor._btc_consistency_coeff(eth, btc), Decimal('0.7'))

    def test_same_trend(self):
        """BTC 与 ETH 同向（均为多头排列）-> 1.0"""
        eth = _make_eth_analysis(MarketState.WEAK_TREND, ema20=Decimal('2500'), ema50=Decimal('2480'))
        btc = _make_btc_analysis(MarketState.NORMAL_STRONG_TREND, ema20=Decimal('69000'), ema50=Decimal('68000'))
        self.assertEqual(self.advisor._btc_consistency_coeff(eth, btc), Decimal('1.0'))

    def test_reverse_trend(self):
        """BTC 与 ETH 反向（ETH 多头 / BTC 空头）-> 0.4"""
        eth = _make_eth_analysis(MarketState.WEAK_TREND, ema20=Decimal('2500'), ema50=Decimal('2480'))
        btc = _make_btc_analysis(MarketState.NORMAL_STRONG_TREND, ema20=Decimal('67000'), ema50=Decimal('68000'))
        self.assertEqual(self.advisor._btc_consistency_coeff(eth, btc), Decimal('0.4'))


class TestFundingRateCoeff(unittest.TestCase):
    """资金费率系数：三段 / 缓存 / 异常降级"""

    def test_three_segments(self):
        """0.0006->0.7 / 0.0005->1.0 / 0->1.0 / -0.0005->1.0 / -0.0006->1.2"""
        cases = [
            (0.0006, Decimal('0.7')),
            (0.0005, Decimal('1.0')),
            (0.0, Decimal('1.0')),
            (-0.0005, Decimal('1.0')),
            (-0.0006, Decimal('1.2')),
        ]
        for rate, expected in cases:
            with self.subTest(rate=rate):
                advisor = _make_advisor(_load_config(), funding_rate=rate)
                result = _run(advisor._funding_rate_coeff('ETHUSDT'))
                self.assertEqual(result, expected, f"rate={rate} 期望={expected} 实际={result}")

    def test_cache_within_8h(self):
        """8h 缓存：第二次调用不重复请求费率接口"""
        advisor = _make_advisor(_load_config(), funding_rate=0.0001)
        _run(advisor._funding_rate_coeff('ETHUSDT'))
        _run(advisor._funding_rate_coeff('ETHUSDT'))
        self.assertEqual(advisor.binance_client.get_funding_rate.await_count, 1)

    def test_exception_degrades(self):
        """费率接口异常 -> 1.0 且进入缓存"""
        advisor = _make_advisor(_load_config(), funding_raises=True)
        result = _run(advisor._funding_rate_coeff('ETHUSDT'))
        self.assertEqual(result, Decimal('1.0'))
        self.assertIsNone(advisor._funding_cache['ETHUSDT'][2])


class TestComputeAdviceFormula(unittest.TestCase):
    """公式综合示例：全链路计算建议保证金"""

    def test_full_formula_add(self):
        """
        综合示例：ETH 震荡 adx=16.5, ATR=50, 基准ATR=100, 费率=0.0001
        状态1.2 × 趋势1.035 × 波动1.5 × BTC 1.0 × 费率1.0 = 1.863
        建议 = round(500 × 1.863) = 932 -> ADD，调整量 = min(432, 150) = 150
        """
        config = _load_config()
        advisor = _make_advisor(config, funding_rate=0.0001)
        eth = _make_eth_analysis(
            MarketState.OSCILLATION,
            adx_1h=Decimal('16.5'),
            atr_smooth=Decimal('50')
        )
        advice = _run(advisor.compute_advice('ETHUSDT', eth))

        self.assertEqual(advice.suggested_margin, Decimal('932'))
        self.assertEqual(advice.current_margin, Decimal('500'))
        self.assertEqual(advice.action, ACTION_ADD)
        self.assertEqual(advice.adjust_amount, Decimal('150'))
        self.assertEqual(advice.coeffs['state'], Decimal('1.2'))
        self.assertEqual(advice.coeffs['trend'], Decimal('1.035'))
        self.assertEqual(advice.coeffs['volatility'], Decimal('1.5'))
        self.assertEqual(advice.coeffs['btc'], Decimal('1.0'))
        self.assertEqual(advice.coeffs['funding'], Decimal('1.0'))
        self.assertFalse(advice.skipped)
        # ETH 震荡：跳过 BTC 检测，btc_state 为 None，不检测背离
        self.assertIsNone(advice.btc_state)
        self.assertIsNone(advice.divergence_line)

    def test_current_zero_shows_entry(self):
        """当前保证金==0 -> action=NONE（防御路径：配置 margin=0）"""
        config = _load_config()
        config['trading']['margin'] = 0
        advisor = _make_advisor(config)
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(advice.action, ACTION_NONE)
        self.assertEqual(advice.current_margin, Decimal('0'))
        section = advisor.format_section(advice)
        self.assertIn("当前无保证金，建议按建议值入场", section)


class TestActionAndCooldown(unittest.TestCase):
    """动作触发 / 限幅 / 冷却"""

    def setUp(self):
        self.config = _load_config()
        self.advisor = _make_advisor(self.config, funding_rate=0.0001)

    def test_evaluate_action_boundaries(self):
        """建议=0->CLEAR；ratio 1.2/0.8 边界"""
        self.assertEqual(self.advisor._evaluate_action(Decimal('0'), Decimal('500')), ACTION_CLEAR)
        self.assertEqual(self.advisor._evaluate_action(Decimal('601'), Decimal('500')), ACTION_ADD)
        self.assertEqual(self.advisor._evaluate_action(Decimal('600'), Decimal('500')), ACTION_NONE)
        self.assertEqual(self.advisor._evaluate_action(Decimal('399'), Decimal('500')), ACTION_REDUCE)
        self.assertEqual(self.advisor._evaluate_action(Decimal('400'), Decimal('500')), ACTION_NONE)

    def test_clear_adjust_amount(self):
        """CLEAR 的调整量 = min(|0-当前|, max_step)"""
        # 极端强趋势 -> 状态系数 0，建议=0 -> CLEAR
        eth = _make_eth_analysis(MarketState.EXTREME_STRONG_TREND, adx_1h=Decimal('45'))
        result = _run(self.advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(result.action, ACTION_CLEAR)
        self.assertEqual(result.adjust_amount, Decimal('150'))

    def test_cooldown_same_direction_blocked(self):
        """同方向 4h 内拦截：action 置 NONE + cooldown_blocked=True，但建议值仍展示"""
        eth = _make_eth_analysis(MarketState.OSCILLATION, adx_1h=Decimal('16.5'))
        first = _run(self.advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(first.action, ACTION_ADD)
        second = _run(self.advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(second.action, ACTION_NONE, "同方向 4h 内应被冷却拦截")
        self.assertTrue(second.cooldown_blocked, "冷却拦截时应标记 cooldown_blocked")
        self.assertEqual(second.blocked_direction, ACTION_ADD)
        self.assertEqual(second.suggested_margin, first.suggested_margin, "拦截后仍展示建议值")

    def test_cooldown_reverse_direction_immediate(self):
        """反向立即推送：ADD 冷却中，REDUCE 不被拦截"""
        eth_high = _make_eth_analysis(MarketState.EXTREME_STRONG_TREND, adx_1h=Decimal('45'))
        # 极端强趋势 -> 建议0 -> CLEAR（先记录一个冷却）
        _run(self.advisor.compute_advice('ETHUSDT', eth_high))
        # 低 ADX -> ADD 场景，CLEAR 与 ADD 不同方向，不受拦截
        eth_low = _make_eth_analysis(MarketState.OSCILLATION, adx_1h=Decimal('16.5'))
        result = _run(self.advisor.compute_advice('ETHUSDT', eth_low))
        self.assertEqual(result.action, ACTION_ADD, "清仓与加码不同方向，不应拦截")

    def test_cooldown_none_not_recorded(self):
        """无需调整(NONE)不记录冷却"""
        # 构造 NONE：震荡 adx=20（>=18 状态1.0）×趋势1.0(adx=20)×波动1.0(ATR=基准)
        # ×BTC 1.0（ETH震荡跳过）×费率1.0 -> 建议=500=当前 -> ratio=1.0 -> NONE
        advisor = _make_advisor(self.config, funding_rate=0.0001)
        eth = _make_eth_analysis(MarketState.OSCILLATION, adx_1h=Decimal('20'), atr_smooth=Decimal('100'))
        result = _run(advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(result.action, ACTION_NONE, "各系数乘积=1.0 时应为无需调整")
        self.assertNotIn(ACTION_NONE, advisor._cooldowns)


class TestFormatSection(unittest.TestCase):
    """推送模板"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config(), funding_rate=0.0001)

    def test_add_template(self):
        """加码模板：关键子串完整"""
        eth = _make_eth_analysis(MarketState.OSCILLATION, adx_1h=Decimal('16.5'), atr_smooth=Decimal('50'))
        advice = _run(self.advisor.compute_advice('ETHUSDT', eth))
        section = self.advisor.format_section(advice)

        self.assertIn("💰 保证金引导（建议加码）", section)
        self.assertIn("- 当前保证金: 500 USDT", section)
        self.assertIn("- 建议保证金: 932 USDT（+86%）", section)
        self.assertIn("- 调整幅度: 建议增加 150 USDT（限幅30%）", section)
        self.assertIn(
            "- 因子: 状态1.2（强震荡 ADX=16.5） / 趋势1.035 / 波动1.5 / BTC 1.0 / 费率1.0",
            section
        )
        self.assertIn("- 资金费率: 0.01%（中性）", section)
        self.assertIn("- BTC一致性: 跳过（ETH震荡）", section)
        # 刚记录 ADD 冷却，冷却行显示剩余时间（与需求模板「剩余 3.2h」一致）
        self.assertIn("- 冷却: 剩余", section)
        self.assertIn("- 注意: 加码后请确认每格下单张数≥1张", section)

    def test_clear_template(self):
        """清仓模板：显示「建议立即清仓」"""
        eth = _make_eth_analysis(MarketState.EXTREME_STRONG_TREND, adx_1h=Decimal('45'))
        advice = _run(self.advisor.compute_advice('ETHUSDT', eth))
        section = self.advisor.format_section(advice)
        self.assertIn("💰 保证金引导（清仓）", section)
        self.assertIn("- 调整幅度: 建议立即清仓", section)

    def test_funding_high_text(self):
        """费率偏高文案"""
        advisor = _make_advisor(_load_config(), funding_rate=0.0006)
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        section = advisor.format_section(advice)
        self.assertIn("- 资金费率: 0.06%（偏高）", section)


class TestDegradePaths(unittest.TestCase):
    """降级路径"""

    def test_btc_detect_raises(self):
        """BTC 检测异常 -> 一致性 1.0，btc_state=None，不阻断"""
        advisor = _make_advisor(_load_config(), btc_detect_raises=True)
        eth = _make_eth_analysis(MarketState.WEAK_TREND, adx_1h=Decimal('28'))
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(advice.coeffs['btc'], Decimal('1.0'))
        self.assertIsNone(advice.btc_state)
        self.assertIsNone(advice.divergence_line)

    def test_baseline_atr_raises(self):
        """基准ATR计算失败 -> 波动率 1.0，last_baseline_atr=None"""
        advisor = _make_advisor(_load_config())
        advisor.grid_calculator.calculate_baseline_atr = MagicMock(side_effect=Exception("数据不足"))
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        self.assertEqual(advice.coeffs['volatility'], Decimal('1.0'))
        self.assertIsNone(advisor.last_baseline_atr)


class TestBtcDivergence(unittest.TestCase):
    """BTC 背离检测"""

    def _advisor_with_klines(self, eth_klines, btc_klines):
        config = _load_config()
        kline_service = _make_mock_kline_service(eth_div_klines=eth_klines, btc_div_klines=btc_klines)
        binance_client = MagicMock()
        binance_client.get_funding_rate = AsyncMock(return_value=0.0001)
        grid_calculator = MagicMock()
        grid_calculator.calculate_baseline_atr = MagicMock(return_value=Decimal('100'))
        advisor = MarginAdvisor(
            config=config, binance_client=binance_client, kline_service=kline_service,
            btc_detector=MagicMock(), grid_calculator=grid_calculator
        )
        return advisor

    def test_eth_high_btc_not_high(self):
        """ETH 创新高且 BTC 未创新高 -> 假突破提示"""
        # ETH 24 根最高 3000，当前价 3000（创新高）；BTC 最高 70000，当前 69000（未）
        eth_klines = _make_klines(24, close=3000, high=3000)
        btc_klines = _make_klines(24, close=69000, high=70000)
        advisor = self._advisor_with_klines(eth_klines, btc_klines)
        eth = _make_eth_analysis(
            MarketState.NORMAL_STRONG_TREND, current_price=Decimal('3000'), atr_smooth=Decimal('800')
        )
        result = _run(advisor._check_btc_divergence('ETHUSDT', eth))
        self.assertEqual(result, "BTC未创新高，ETH上涨可能为假突破")

    def test_eth_low_btc_not_low(self):
        """ETH 创区间新低且 BTC 未创新低 -> 假跌破提示"""
        eth_klines = _make_klines(24, close=3000, low=3000, high=3100)
        btc_klines = _make_klines(24, close=69000, high=70000, low=68000)
        advisor = self._advisor_with_klines(eth_klines, btc_klines)
        eth = _make_eth_analysis(
            MarketState.NORMAL_STRONG_TREND, current_price=Decimal('3000'), atr_smooth=Decimal('800')
        )
        result = _run(advisor._check_btc_divergence('ETHUSDT', eth))
        self.assertEqual(result, "BTC未创新低，ETH下跌可能为假跌破")

    def test_both_high_no_divergence(self):
        """ETH 与 BTC 均创新高 -> None"""
        eth_klines = _make_klines(24, close=3000, high=3000)
        btc_klines = _make_klines(24, close=70000, high=70000)
        advisor = self._advisor_with_klines(eth_klines, btc_klines)
        eth = _make_eth_analysis(
            MarketState.NORMAL_STRONG_TREND, current_price=Decimal('3000'), atr_smooth=Decimal('800')
        )
        result = _run(advisor._check_btc_divergence('ETHUSDT', eth))
        self.assertIsNone(result)

    def test_exception_returns_none(self):
        """K线获取异常 -> None"""
        advisor = self._advisor_with_klines([], [])
        advisor.kline_service.get_klines = AsyncMock(side_effect=Exception("K线服务故障"))
        eth = _make_eth_analysis(MarketState.NORMAL_STRONG_TREND)
        result = _run(advisor._check_btc_divergence('ETHUSDT', eth))
        self.assertIsNone(result)


class TestBtcDetectorIsolation(unittest.TestCase):
    """BTC 独立 detector 不污染 ETH 检测器实例状态"""

    def setUp(self):
        from strategies.grid.signal_bot import GridSignalBot
        config = _load_config()
        self.bot = GridSignalBot(
            binance_client=MagicMock(),
            kline_service=_make_mock_kline_service(),
            notification_client=MagicMock(),
            grid_calculator=MagicMock(),
            config=config
        )

    def test_instances_independent(self):
        """两个 detector 是不同实例"""
        self.assertIsNot(self.bot.market_detector, self.bot.btc_market_detector)
        self.assertIs(self.bot.margin_advisor.btc_detector, self.bot.btc_market_detector)

    def test_state_not_polluted(self):
        """BTC detector 更新历史状态不影响 ETH detector"""
        self.bot.btc_market_detector._update_adx_history(Decimal('30'))
        self.bot.btc_market_detector._update_atr_history(Decimal('100'))
        self.assertEqual(len(self.bot.market_detector._adx_history), 0)
        self.assertEqual(len(self.bot.market_detector._atr_history), 0)


class TestSignalBotIntegration(unittest.TestCase):
    """signal_bot 集成：板块拼装 / 基准ATR复用 / 趋势态警报追加背离行"""

    def setUp(self):
        from strategies.grid.signal_bot import GridSignalBot
        config = _load_config()
        self.bot = GridSignalBot(
            binance_client=MagicMock(),
            kline_service=_make_mock_kline_service(),
            notification_client=MagicMock(),
            grid_calculator=MagicMock(),
            config=config
        )

    def _make_advice(self, skipped=False, divergence=None) -> MarginAdvice:
        return MarginAdvice(
            suggested_margin=Decimal('638'),
            current_margin=Decimal('500'),
            action=ACTION_ADD,
            adjust_amount=Decimal('150'),
            coeffs={'state': Decimal('1.2'), 'trend': Decimal('0.96'), 'volatility': Decimal('1.33'),
                    'btc': Decimal('1.0'), 'funding': Decimal('1.0'), 'adx_1h': Decimal('16.5'),
                    'state_name': MarketState.OSCILLATION.name,
                    'eth_oscillation': Decimal('1')},
            funding_rate=0.0001,
            btc_state=MarketState.NORMAL_STRONG_TREND,
            divergence_line=divergence,
            skipped=skipped
        )

    def _make_grid_params(self):
        from strategies.grid.grid_calculator import DynamicGridParams, GridMode
        return DynamicGridParams(
            lower_boundary=Decimal('2400'), upper_boundary=Decimal('2600'),
            grid_count=8, grid_mode=GridMode.ARITHMETIC,
            stop_loss_low=Decimal('2300'), stop_loss_high=Decimal('2700'),
            profit_rate=Decimal('0.012'), grid_spacing=Decimal('25')
        )

    def test_signal_message_includes_margin_section(self):
        """可交易态：非 skipped 时拼装保证金引导板块"""
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        message = self.bot._generate_signal_message(
            symbol='ETHUSDT', market_analysis=eth,
            grid_params=self._make_grid_params(),
            position_valid=True, position_message="每格2.00张（取整后2张）",
            advice=self._make_advice(skipped=False)
        )
        self.assertIn("💰 保证金引导（建议加码）", message)
        self.assertIn("💡 操作指令：", message)
        # 板块位于资金配置之后、操作指令之前
        self.assertLess(message.index("💰 保证金引导"), message.index("💡 操作指令"))

    def test_signal_message_skipped_omits_section(self):
        """skipped=True 时不拼装板块"""
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        message = self.bot._generate_signal_message(
            symbol='ETHUSDT', market_analysis=eth,
            grid_params=self._make_grid_params(),
            position_valid=True, position_message="每格2.00张（取整后2张）",
            advice=self._make_advice(skipped=True)
        )
        self.assertNotIn("保证金引导", message)

    def test_trend_alert_appends_divergence(self):
        """趋势态警报末尾追加背离行（仅非空时）"""
        eth = _make_eth_analysis(MarketState.NORMAL_STRONG_TREND, adx_1h=Decimal('35'))
        message = self.bot._generate_normal_strong_message(
            'ETHUSDT', eth, advice=self._make_advice(divergence="BTC未创新高，ETH上涨可能为假突破")
        )
        self.assertIn("⚠️ 背离: BTC未创新高，ETH上涨可能为假突破", message)
        # advice 无背离行时保持原样
        message_plain = self.bot._generate_normal_strong_message('ETHUSDT', eth, advice=self._make_advice())
        self.assertNotIn("⚠️ 背离", message_plain)

    def test_calculate_grid_params_reuses_baseline(self):
        """传入 atr_baseline 时复用，不再请求 1d K 线"""
        grid_calculator = MagicMock()
        grid_calculator.calculate_dynamic_grid_params = MagicMock(return_value=self._make_grid_params())
        grid_calculator.validate_profit_rate = MagicMock(return_value=(True, None))
        self.bot.grid_calculator = grid_calculator
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        params = _run(self.bot._calculate_grid_params(
            'ETHUSDT', eth, atr_baseline=Decimal('100')
        ))
        self.assertEqual(params.grid_count, 8)
        self.bot.kline_service.get_klines.assert_not_awaited()
        grid_calculator.calculate_baseline_atr.assert_not_called()


class TestAdditionalCoverage(unittest.TestCase):
    """补充覆盖：防御入口 / 未知状态 / 模板分支 / 冷却模板 / 空K线"""

    def test_empty_config_raises(self):
        """空配置 -> ValueError"""
        kline = _make_mock_kline_service()
        with self.assertRaises(ValueError):
            MarginAdvisor(
                config={}, binance_client=MagicMock(), kline_service=kline,
                btc_detector=MagicMock(), grid_calculator=MagicMock()
            )

    def test_unknown_state_defaults_zero(self):
        """state_coeff 映射缺失的枚举 -> 0（保守处理）"""
        config = _load_config()
        config['margin_guide']['state_coeff'] = {}
        advisor = _make_advisor(config)
        result = advisor._state_coeff(MarketState.WEAK_TREND, Decimal('30'))
        self.assertEqual(result, Decimal('0'))

    def test_empty_klines_returns_none(self):
        """背离检测 K 线为空 -> None（非异常路径）"""
        advisor = self._phase_divergence_with_klines([], [])
        eth = _make_eth_analysis(MarketState.NORMAL_STRONG_TREND)
        result = _run(advisor._check_btc_divergence('ETHUSDT', eth))
        self.assertIsNone(result)

    @staticmethod
    def _phase_divergence_with_klines(eth_klines, btc_klines):
        config = _load_config()
        kline_service = _make_mock_kline_service(eth_div_klines=eth_klines, btc_div_klines=btc_klines)
        binance_client = MagicMock()
        binance_client.get_funding_rate = AsyncMock(return_value=0.0001)
        grid_calculator = MagicMock()
        grid_calculator.calculate_baseline_atr = MagicMock(return_value=Decimal('100'))
        return MarginAdvisor(
            config=config, binance_client=binance_client, kline_service=kline_service,
            btc_detector=MagicMock(), grid_calculator=grid_calculator
        )


class TestFundingFailedText(unittest.TestCase):
    """费率获取失败 / 偏低文案（推送模板分支）"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config(), funding_rate=0.0001)

    def test_funding_failed_text(self):
        """费率获取失败 -> 展示「获取失败（降级）」"""
        advisor = _make_advisor(_load_config(), funding_raises=True)
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        section = advisor.format_section(advice)
        self.assertIn("获取失败（降级）", section)

    def test_funding_low_text(self):
        """费率偏低文案（<-0.0005）"""
        advisor = _make_advisor(_load_config(), funding_rate=-0.0006)
        eth = _make_eth_analysis(MarketState.OSCILLATION)
        advice = _run(advisor.compute_advice('ETHUSDT', eth))
        section = advisor.format_section(advice)
        self.assertIn("（偏低）", section)


class TestBtcTextBranches(unittest.TestCase):
    """BTC 一致性行文案：异常 / 反向 / 同向（非震荡跳过）"""

    def setUp(self):
        self.advisor = _make_advisor(_load_config())
        self.base = {'state': Decimal('1.0'), 'trend': Decimal('1.0'),
                     'volatility': Decimal('1.0'), 'btc': Decimal('1.0'),
                     'funding': Decimal('1.0'), 'adx_1h': Decimal('20'),
                     'state_name': MarketState.WEAK_TREND.name,
                     'eth_oscillation': Decimal('0')}

    def _advice(self, **kw):
        d = dict(suggested_margin=Decimal('500'), current_margin=Decimal('500'),
                 action=ACTION_NONE, adjust_amount=Decimal('0'),
                 coeffs=dict(self.base), funding_rate=0.0001,
                 btc_state=MarketState.NORMAL_STRONG_TREND,
                 divergence_line=None, skipped=False)
        d.update(kw)
        return MarginAdvice(**d)

    def test_btc_text_abnormal(self):
        """btc_state=None（检测失败）-> 异常（降级）"""
        advice = self._advice(btc_state=None)
        self.assertEqual(self.advisor._btc_text(advice), "异常（降级）")

    def test_btc_text_reverse(self):
        """BTC 系数=反向 0.4 -> 反向（背离）"""
        advice = self._advice(coeffs={**self.base, 'btc': Decimal('0.4')})
        self.assertEqual(self.advisor._btc_text(advice), "反向（背离）")

    def test_btc_text_same(self):
        """BTC 系数=1.0（非同向判定的反向）-> 同向（确认）"""
        advice = self._advice(coeffs={**self.base, 'btc': Decimal('1.0')})
        self.assertEqual(self.advisor._btc_text(advice), "同向（确认）")


class TestCooldownBlockedTemplate(unittest.TestCase):
    """冷却拦截时的板块标题 + 说明行"""

    def test_cooldown_blocked_template(self):
        advisor = _make_advisor(_load_config(), funding_rate=0.0001)
        eth = _make_eth_analysis(MarketState.OSCILLATION, adx_1h=Decimal('16.5'))
        _run(advisor.compute_advice('ETHUSDT', eth))   # 记录 ADD 冷却
        second = _run(advisor.compute_advice('ETHUSDT', eth))  # 同方向拦截
        self.assertTrue(second.cooldown_blocked)
        section = advisor.format_section(second)
        self.assertIn("💰 保证金引导（冷却中，暂不操作）", section)
        self.assertIn("距上次加码未满4h冷却期", section)


if __name__ == '__main__':
    unittest.main(verbosity=2)
