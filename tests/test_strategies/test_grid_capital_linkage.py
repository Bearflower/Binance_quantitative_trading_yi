"""
V2.5.5 方案B：保证金-网格数联动测试

覆盖设计文档 AC-01 ~ AC-10：
- resolve_capital_feasibility 约束链（STRUCT/PROFIT/CAPITAL、不可行出口、tie-break、非法输入）
- DynamicGridParams 与 calculate_dynamic_grid_params 弱趋势状态感知下限（AC-08，D-3）
- GridSignalBot._calculate_grid_params 的 C1/C2/C3 合并与 profit/spacing 重算
- run_once 保证金口径统一（FR-01）、推送文案、危险态（AC-09）、开关整体回退（T5）
"""
import os
from decimal import Decimal, ROUND_CEILING
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from strategies.grid.grid_calculator import (
    GridCalculator, DynamicGridParams, GridMode, CapitalFeasibility,
    BINDING_STRUCT, BINDING_PROFIT, BINDING_CAPITAL, BINDING_INFEASIBLE,
)
from strategies.grid.margin_advisor import (
    MarginAdvice, ACTION_ADD, ACTION_NONE, DANGEROUS_STATES,
)
from strategies.grid.market_state import MarketState, MarketAnalysis
from strategies.grid.signal_bot import GridSignalBot


# ========== 测试常量（设计文档第 1/7 节线上样例） ==========

P_SAMPLE = Decimal('2558.48')   # 样例价格
LEV = 10                        # trading.leverage
M_ADVISED = Decimal('936')      # 五因子建议保证金
M_FEASIBLE = Decimal('1400')    # AC-04 可行用例
Q_MIN = Decimal('1')            # trading.min_quantity
N_MIN_OSC = 5                   # 震荡最小网格数
N_STRUCT_SAMPLE = 7             # 样例结构网格数
N_PROFIT_SAMPLE = 7             # 样例利润率约束上限（W=185.8 等差）


def make_calc_config(min_quantity=1, capital_enabled=True):
    """构造计算器最小可用配置"""
    return {
        'grid': {
            'type': 'dynamic',
            'count': 8,
            'spacing': 100,
            'spacing_ratio': 1.01,
            'base_quantity': 0.001,
            'min_grid_count': 5,
            'max_grid_count': 12,
            'weak_trend_min_grid_count': 4,
            'weak_trend_max_grid_count': 10,
            'capital_constraint': {'enabled': capital_enabled},
        },
        'trading': {'leverage': LEV, 'margin': 500, 'min_quantity': min_quantity},
    }


@pytest.fixture
def calculator():
    return GridCalculator(make_calc_config())


def load_yaml_config():
    """加载 strategies/grid/config.yaml 真实配置"""
    path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'strategies', 'grid', 'config.yaml'
    )
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def make_analysis(state=MarketState.OSCILLATION, price=P_SAMPLE,
                  atr_smooth=Decimal('15.48'), adx_1h=Decimal('15')):
    """构造市场分析（危险态生成器需要的字段全部给齐）"""
    return MarketAnalysis(
        state=state,
        trend_strength=Decimal('0.1'),
        adx_1h=adx_1h,
        adx_4h=Decimal('14'),
        adx_15m=Decimal('14'),
        ema20_1h=price,
        ema50_1h=price - Decimal('100'),
        ema20_4h=price,
        ema50_4h=price - Decimal('100'),
        current_price=price,
        atr_smooth=atr_smooth,
        atr_2h_ago=atr_smooth,
        adx_prev_1h=Decimal('10'),
        confidence=Decimal('0.5'),
        price_change_1h=Decimal('0.001'),
        price_change_15m=Decimal('0.001'),
    )


def make_advice(margin=M_ADVISED, action=ACTION_NONE, skipped=False):
    """构造保证金引导建议（coeffs 覆盖 format_section 读取的全部键）"""
    return MarginAdvice(
        suggested_margin=margin,
        current_margin=Decimal('500'),
        action=action,
        adjust_amount=Decimal('150') if action == ACTION_ADD else Decimal('0'),
        coeffs={
            'state': Decimal('1.0'), 'trend': Decimal('1.0'),
            'volatility': Decimal('1.0'), 'btc': Decimal('1.0'),
            'funding': Decimal('1.0'), 'adx_1h': Decimal('15'),
            'state_name': 'OSCILLATION', 'eth_oscillation': Decimal('1'),
        },
        funding_rate=None,
        btc_state=None,
        divergence_line=None,
        skipped=skipped,
    )


def make_bot(config=None):
    """用真实 GridCalculator + mock 客户端构造信号灯机器人"""
    config = config or load_yaml_config()
    bot = GridSignalBot(
        binance_client=AsyncMock(),
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        grid_calculator=GridCalculator(config),
        config=config,
    )
    return bot


def sample_struct_params(grid_count=N_STRUCT_SAMPLE, price=P_SAMPLE,
                         width=Decimal('185.76')):
    """构造与样例一致的动态网格参数（等差，W≈185.8，7 格利润率 1.04%）"""
    lower = price - width / 2
    upper = price + width / 2
    spacing = width / Decimal(str(grid_count))
    return DynamicGridParams(
        lower_boundary=lower,
        upper_boundary=upper,
        grid_count=grid_count,
        grid_mode=GridMode.ARITHMETIC,
        stop_loss_low=lower - Decimal('50'),
        stop_loss_high=upper + Decimal('50'),
        stop_move_up_price=upper + Decimal('25'),
        stop_move_down_price=lower - Decimal('25'),
        profit_rate=spacing / price,
        grid_spacing=spacing,
    )


# ========== T1：resolve_capital_feasibility 约束链 ==========

class TestResolveCapitalFeasibility:

    def test_ac02_struct_binding(self, calculator):
        """M 充足、利润率充足：N_final=N_struct，binding=STRUCT"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('5000'), 7, 7, N_MIN_OSC
        )
        assert result.feasible is True
        assert result.grid_count == 7
        assert result.binding == BINDING_STRUCT
        assert result.qty_per_grid >= Q_MIN
        assert result.required_margin is None

    def test_ac03_profit_binding(self, calculator):
        """利润率为绑定项：N_final=N_profit=6"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('5000'), 10, 6, N_MIN_OSC
        )
        assert result.feasible is True
        assert result.grid_count == 6
        assert result.binding == BINDING_PROFIT

    def test_ac04_capital_binding_feasible(self, calculator):
        """AC-04（A-review 修正用例）：M=1400 → N_cap=5，可行且 binding=CAPITAL"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_FEASIBLE, N_STRUCT_SAMPLE, N_PROFIT_SAMPLE, N_MIN_OSC
        )
        assert calculator.max_grids_by_capital(P_SAMPLE, LEV, M_FEASIBLE) == 5
        assert result.feasible is True
        assert result.grid_count == 5
        assert result.binding == BINDING_CAPITAL
        # 不变量1：可行时每格张数 >= q_min
        assert result.qty_per_grid >= Q_MIN

    def test_tie_break_struct_first(self, calculator):
        """并列 tie-break：N_struct=N_profit 最小 → STRUCT"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('5000'), 6, 6, N_MIN_OSC
        )
        assert result.binding == BINDING_STRUCT
        assert result.grid_count == 6

    def test_tie_break_profit_before_capital(self, calculator):
        """并列 tie-break：N_profit=N_cap 最小 → PROFIT（STRUCT>PROFIT>CAPITAL）"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_FEASIBLE, 9, 5, N_MIN_OSC
        )
        assert result.grid_count == 5
        assert result.binding == BINDING_PROFIT

    def test_ac05_infeasible_sample(self, calculator):
        """AC-05/第7节复算：M=936 → 不可行，M_required=1280、L_required=14"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_ADVISED, N_STRUCT_SAMPLE, N_PROFIT_SAMPLE, N_MIN_OSC
        )
        assert result.feasible is False
        assert result.binding == BINDING_CAPITAL
        assert result.grid_count == N_STRUCT_SAMPLE  # 不可行时返回 N_struct 供参考
        assert result.required_margin == Decimal('1280')
        assert result.required_leverage == 14
        # 不可行时 qty 按 N_min 格计算：936×10/(2558.48×5)=0.73
        assert result.qty_per_grid < Q_MIN

    def test_infeasible_profit_binding(self, calculator):
        """N_profit<N_min：不可行，binding=PROFIT（设计 4.6 区间过窄场景）"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('5000'), 7, 4, N_MIN_OSC
        )
        assert result.feasible is False
        assert result.binding == BINDING_PROFIT
        assert result.required_margin == Decimal('1280')

    def test_n_cap_zero_infeasible(self, calculator):
        """N_cap=0 边界：不可行，仍按 N_min 反推出口"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('1'), 7, 7, N_MIN_OSC
        )
        assert result.feasible is False
        assert result.binding == BINDING_CAPITAL
        assert result.required_leverage == 12793

    def test_boundary_n_cap_equals_n_min(self, calculator):
        """N_cap=N_min 临界：恰好可行"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('1280'), 7, 7, N_MIN_OSC
        )
        assert result.feasible is True
        assert result.grid_count == N_MIN_OSC
        assert result.binding == BINDING_CAPITAL

    def test_required_values_reachable(self, calculator):
        """ceil 出口可达性：M_required/L_required 满足，且减 1 不满足"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_ADVISED, 7, 7, N_MIN_OSC
        )
        need = N_MIN_OSC * P_SAMPLE * Q_MIN
        # 1280×10/(2558.48×5) >= 1，1279 则 <1（A-review 事实修正#2）
        assert result.required_margin * LEV / need >= 1
        assert (result.required_margin - 1) * LEV / need < 1
        # 14x×936 足够 5 格，13x 不足
        assert Decimal(result.required_leverage) * M_ADVISED / need >= 1
        assert Decimal(result.required_leverage - 1) * M_ADVISED / need < 1

    def test_ac07_min_quantity_from_config(self):
        """AC-07：q_min 改配置即生效（2 张门槛下 M=1400 由可行变不可行）"""
        calc_q1 = GridCalculator(make_calc_config(min_quantity=1))
        calc_q2 = GridCalculator(make_calc_config(min_quantity=2))
        r1 = calc_q1.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_FEASIBLE, 7, 7, N_MIN_OSC
        )
        r2 = calc_q2.resolve_capital_feasibility(
            P_SAMPLE, LEV, M_FEASIBLE, 7, 7, N_MIN_OSC
        )
        assert r1.feasible is True and r1.grid_count == 5
        assert r2.feasible is False
        assert calc_q2.max_grids_by_capital(P_SAMPLE, LEV, M_FEASIBLE) == 2

    @pytest.mark.parametrize("kwargs", [
        {'price': Decimal('0')},
        {'leverage': 0},
        {'margin': Decimal('0')},
        {'min_quantity': Decimal('0')},
        {'n_min': 0},
    ])
    def test_illegal_inputs_return_infeasible(self, calculator, kwargs):
        """4.6：非法输入返回不可行+INFEASIBLE，不抛异常"""
        defaults = dict(price=P_SAMPLE, leverage=LEV, margin=M_ADVISED,
                        n_struct=7, n_profit_max=7, n_min=N_MIN_OSC)
        defaults.update(kwargs)
        result = calculator.resolve_capital_feasibility(**defaults)
        assert result.feasible is False
        assert result.binding == BINDING_INFEASIBLE
        assert result.required_margin is None
        assert result.required_leverage is None

    def test_max_grids_illegal_returns_zero(self, calculator):
        """max_grids_by_capital 非法输入返回 0"""
        assert calculator.max_grids_by_capital(Decimal('0'), LEV, M_ADVISED) == 0

    def test_enforce_capital_false_rollback(self, calculator):
        """enforce_capital=False：C3 不压制（灰度回退复用同一计算链）"""
        result = calculator.resolve_capital_feasibility(
            P_SAMPLE, LEV, Decimal('500'), 7, 7, N_MIN_OSC, enforce_capital=False
        )
        assert result.feasible is True
        assert result.grid_count == 7
        assert result.binding == BINDING_STRUCT
        # 每格张数按 500/7 格真实计算（V2.5 口径 0.28 张）
        assert result.qty_per_grid == Decimal('500') * LEV / (P_SAMPLE * 7)

    def test_validate_profit_rate_missing_min_grid_config(self):
        """防御分支：未传 min_grid_count 且配置缺失时抛 ValueError"""
        config = make_calc_config()
        config['grid']['min_profit_rate'] = 0.01
        del config['grid']['min_grid_count']
        calc = GridCalculator(config)
        # W=60、7 格利润率 0.34%<1%，进入降网格循环后读不到最小格数配置
        params = sample_struct_params(grid_count=7, width=Decimal('60'))
        with pytest.raises(ValueError, match="配置缺失：grid.min_grid_count"):
            calc.validate_profit_rate(params)

    def test_validate_profit_rate_reads_min_grid_from_config(self):
        """未传 min_grid_count 时从配置读取（真实 config 含 min_grid_count=5），进入逐试循环"""
        calc = GridCalculator(load_yaml_config())
        # W=162：7 格 0.90%<1%，降到 6 格 1.06% 达标 → 返回建议值
        params = sample_struct_params(grid_count=7, width=Decimal('162'))
        valid, suggested = calc.validate_profit_rate(params)
        assert valid is False
        assert suggested == 6


# ========== T2：D-3 弱趋势下限状态感知（AC-08） ==========

class TestDynamicGridParamsStateAware:

    def test_ac08b_dataclass_accepts_four_grids(self):
        """AC-08b：弱趋势 4 格构造成功；3 格仍被硬下限拒绝"""
        params = DynamicGridParams(
            Decimal('2900'), Decimal('3100'), 4, GridMode.ARITHMETIC,
            Decimal('2800'), Decimal('3200')
        )
        assert params.grid_count == 4
        with pytest.raises(ValueError, match="网格数量必须至少为4"):
            DynamicGridParams(
                Decimal('2900'), Decimal('3100'), 3, GridMode.ARITHMETIC,
                Decimal('2800'), Decimal('3200')
            )

    def test_ac08a_weak_four_grids_not_lifted(self):
        """AC-08a：弱趋势算到 4 格时最终 grid_count==4（不被静默抬回 5）"""
        calc = GridCalculator(load_yaml_config())
        # ratio=0.8 -> round(0.8×6×0.8)=round(3.84)=4
        params = calc.calculate_dynamic_grid_params(
            current_price=Decimal('3000'),
            atr_smooth=Decimal('100'),
            atr_baseline=Decimal('80'),
            market_state='弱趋势',
        )
        assert params.grid_count == 4

    def test_weak_clamps_to_own_bounds(self):
        """弱趋势状态感知夹逼：raw=2 夹回 4，raw=14 夹到 10"""
        calc = GridCalculator(load_yaml_config())
        low = calc.calculate_dynamic_grid_params(
            Decimal('3000'), Decimal('100'), Decimal('50'), '弱趋势'
        )
        high = calc.calculate_dynamic_grid_params(
            Decimal('3000'), Decimal('100'), Decimal('300'), '弱趋势'
        )
        assert low.grid_count == 4
        assert high.grid_count == 10

    def test_oscillation_floor_remains_five(self):
        """震荡状态下限仍是 5（状态感知不扩大弱趋势下限）"""
        calc = GridCalculator(load_yaml_config())
        params = calc.calculate_dynamic_grid_params(
            Decimal('3000'), Decimal('100'), Decimal('40'), '震荡市场'
        )
        assert params.grid_count == 5

    def test_step7_second_clamp_defensive(self):
        """第7步防御性二次夹逼：第2步已夹到范围后口径再变化，第7步仍能按状态夹回"""

        class _GridSection(dict):
            """同一调用内第二次读 weak_trend_min_grid_count 时返回更高下限"""

            def __init__(self, real):
                super().__init__(real)
                self._min_reads = 0

            def get(self, key, default=None):
                if key == 'weak_trend_min_grid_count':
                    self._min_reads += 1
                    if self._min_reads >= 2:
                        return 6
                return super().get(key, default)

        config = load_yaml_config()
        config['grid'] = _GridSection(config['grid'])
        calc = GridCalculator(config)
        # raw=2：第2步按[4,10]夹到4；第7步读到下限6，二次夹逼到 max(6,min(10,4))=6
        params = calc.calculate_dynamic_grid_params(
            Decimal('3000'), Decimal('100'), Decimal('50'), '弱趋势'
        )
        assert params.grid_count == 6


# ========== T3/T4/T5：signal_bot 约束链接入与文案 ==========

class TestGridParamsConstraintChain:
    """_calculate_grid_params 合并 C1/C2/C3（真实 calculator + mock 外部服务）"""

    @pytest.mark.asyncio
    async def test_chain_presses_count_and_recalcs_spacing(self):
        """AC-04 链路口径：M=1400 下压到 5 格，spacing/profit 按 N_final 重算"""
        bot = make_bot()
        analysis = make_analysis()  # atr=15.48, baseline=18 → N_struct=7 等差
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=M_FEASIBLE, atr_baseline=Decimal('18')
        )
        assert params.grid_count == 5
        assert feasibility.binding == BINDING_CAPITAL
        width = params.upper_boundary - params.lower_boundary
        # spacing/profit 与最终格数严格一致（修正旧实现不同步怪癖）
        assert params.grid_spacing == width / 5
        assert params.profit_rate == params.grid_spacing / analysis.current_price

    @pytest.mark.asyncio
    async def test_chain_infeasible_keeps_struct(self):
        """不可行时保留 N_struct 供参考，出口值 1280/14"""
        bot = make_bot()
        analysis = make_analysis()
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=M_ADVISED, atr_baseline=Decimal('18')
        )
        assert params.grid_count == N_STRUCT_SAMPLE
        assert feasibility.feasible is False
        assert feasibility.required_margin == Decimal('1280')
        assert feasibility.required_leverage == 14

    @pytest.mark.asyncio
    async def test_profit_rate_still_binds(self):
        """C2 生效：窄区间下利润率约束参与 min（精确口径，非近似公式）"""
        bot = make_bot()
        # atr=5 → W=60，P=2558.48，等差下 7 格利润率仅 0.33%，
        # 最大满足 1% 的格数 floor(60/(2558.48×0.01))=2 < N_min=5 → 不可行 PROFIT
        analysis = make_analysis(atr_smooth=Decimal('5'))
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=Decimal('5000'), atr_baseline=Decimal('5')
        )
        assert feasibility.feasible is False
        assert feasibility.binding == BINDING_PROFIT

    @pytest.mark.asyncio
    async def test_profit_cap_intermediate_suggested_count(self):
        """C2 中间值：N_struct=7 利润率不足、降到 6 格可达（_resolve_profit_cap 建议值分支）"""
        bot = make_bot()
        # 真实配置 base_grid_count=6：ratio=15.5/13.5=1.148 → raw=round(6.89)=7；
        # W=12×13.5=162 → 7 格利润率 0.90%<1%，6 格 1.06%≥1% → suggested=6
        analysis = make_analysis(atr_smooth=Decimal('13.5'))
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=Decimal('5000'), atr_baseline=Decimal('15.5')
        )
        assert feasibility.feasible is True
        assert feasibility.grid_count == 6
        assert feasibility.binding == BINDING_PROFIT
        # spacing/profit 按 N_final=6 重算并与区间宽度一致
        width = params.upper_boundary - params.lower_boundary
        assert params.grid_spacing == width / 6

    @pytest.mark.asyncio
    async def test_weak_trend_chain_end_to_end(self):
        """弱趋势端到端：状态网格界限 (4,10)，资金充足时 N_final 按结构（6 格 STRUCT）"""
        bot = make_bot()
        assert bot._state_grid_bounds(MarketState.WEAK_TREND) == (4, 10)
        analysis = make_analysis(state=MarketState.WEAK_TREND)
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=Decimal('5000'), atr_baseline=Decimal('18')
        )
        assert params.grid_count == 6
        assert feasibility.feasible is True
        assert feasibility.binding == BINDING_STRUCT


class TestRunOnceMarginCaliber:
    """run_once 保证金口径（FR-01）与 AC-01"""

    async def _run_once(self, bot, advice=None):
        analysis = make_analysis()
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(return_value=advice)
        # last_baseline_atr 为只读 property，compute_advice 被 mock 后直接写底层字段
        bot.margin_advisor._last_baseline_atr = Decimal('18')
        return await bot.run_once('ETHUSDT')

    @pytest.mark.asyncio
    async def test_ac01_uses_advised_margin(self):
        """AC-01：可行性计算传入 advice.suggested_margin=936"""
        bot = make_bot()
        spy = MagicMock(wraps=bot.grid_calculator.resolve_capital_feasibility)
        bot.grid_calculator.resolve_capital_feasibility = spy
        signal = await self._run_once(bot, advice=make_advice(M_ADVISED))
        assert spy.call_count == 1
        assert spy.call_args.kwargs['margin'] == M_ADVISED
        assert signal.margin_used == M_ADVISED

    @pytest.mark.asyncio
    async def test_fallback_when_advice_none(self):
        """advice 不可得：回退 trading.margin=500"""
        bot = make_bot()
        spy = MagicMock(wraps=bot.grid_calculator.resolve_capital_feasibility)
        bot.grid_calculator.resolve_capital_feasibility = spy
        signal = await self._run_once(bot, advice=None)
        assert spy.call_args.kwargs['margin'] == Decimal('500')
        assert signal.margin_used == Decimal('500')

    @pytest.mark.asyncio
    async def test_ac06_message_self_consistency(self):
        """AC-06：同一推送三处数值可互相复算（样例 M=936 不可行分支）"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(M_ADVISED))
        msg = signal.message
        assert '建议保证金: 936 USDT' in msg
        assert '无法支撑最少 5 格' in msg
        assert '保证金提高至 1280 USDT' in msg
        assert '杠杆提高至 14x' in msg
        assert '可达上限：3 格' in msg
        # 不再出现 V2.5 不可行建议「减少网格数量至5格」（修正 D-1，AC-05）
        assert '减少网格数量至5格' not in msg
        assert '方案1（保守）' not in msg
        # 数值复算（与实现同口径 ROUND_CEILING，禁用 Decimal // 负数截断）：
        # 1280=ceil(5×2558.48/10)，14=ceil(5×2558.48/936)
        ceil_margin = (P_SAMPLE * 5 / LEV).to_integral_value(rounding=ROUND_CEILING)
        ceil_leverage = (P_SAMPLE * 5 / M_ADVISED).to_integral_value(rounding=ROUND_CEILING)
        assert Decimal('1280') == ceil_margin
        assert Decimal('14') == ceil_leverage

    @pytest.mark.asyncio
    async def test_feasible_message_struct(self):
        """可行 STRUCT 文案（设计 4.5 模板）"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(Decimal('5000')))
        assert signal.position_valid is True
        assert '网格数量 7 格（受结构约束）' in signal.message
        assert '资金可行性提醒' not in signal.message

    @pytest.mark.asyncio
    async def test_feasible_message_capital(self):
        """可行 CAPITAL 文案：M=1400 → 5 格（受资本约束）"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(M_FEASIBLE))
        assert '网格数量 5 格（受资本约束）' in signal.message

    def test_feasible_message_profit_binding_direct(self):
        """可行 PROFIT 文案（_build_funding_text 直造）"""
        bot = make_bot()
        feasibility = CapitalFeasibility(
            feasible=True, grid_count=6,
            qty_per_grid=Decimal('3.25'), binding=BINDING_PROFIT
        )
        text = bot._build_funding_text(
            make_analysis(), sample_struct_params(6), feasibility, Decimal('5000')
        )
        assert '网格数量 6 格（受利润率约束）' in text

    def test_infeasible_profit_copy_direct(self):
        """4.6：PROFIT 绑定不可行文案含区间过窄提示，与 D-4 分开表述"""
        bot = make_bot()
        feasibility = CapitalFeasibility(
            feasible=False, grid_count=7, qty_per_grid=Decimal('0.33'),
            binding=BINDING_PROFIT,
            required_margin=Decimal('1280'), required_leverage=2,
        )
        text = bot._build_funding_text(
            make_analysis(), sample_struct_params(7), feasibility, Decimal('5000')
        )
        assert '区间过窄/波动率过低' in text
        assert '保证金提高至 1280 USDT' in text

    @pytest.mark.asyncio
    async def test_margin_section_qty_note_injected(self):
        """4.5：保证金引导尾部行注入实际每格张数，替换固定文案"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(
            Decimal('5000'), action=ACTION_ADD
        ))
        assert '按建议保证金 5000 USDT 计算，每格' in signal.message
        assert '请确认每格下单张数≥1张' not in signal.message

    @pytest.mark.asyncio
    async def test_ac10_non_funding_sections_unchanged(self):
        """AC-10：非资金板块逐字不变（标题/市场数据/网格参数/止盈止损/上下移/步骤1~6）"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(Decimal('5000')))
        msg = signal.message
        analysis = make_analysis(price=P_SAMPLE, atr_smooth=Decimal('15.48'))
        expected_lines = [
            '【网格信号灯】震荡市场',
            '- 价格: 2558.48 USDT',
            '- 网格模式: 等差',
            '- 网格数量: 7 格',
            '📈 上移功能（启用）',
            '📉 下移功能（启用）',
            '1. 登录币安APP',
            '2. 点击"创建网格" → 合约网格。',
            '3. 填入以上价格区间、网格数量、网格模式。',
            '6. 确认创建前请检查每格下单数量≥1张。',
        ]
        for line in expected_lines:
            assert line in msg, f"缺失固定行: {line}"
        # 资金相关行已随方案B改造
        assert analysis.state.value in msg

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", sorted(DANGEROUS_STATES, key=lambda s: s.name))
    async def test_ac09_dangerous_states_no_grid_params(self, state):
        """AC-09：所有危险态不计算网格参数、不拼装资金板块"""
        bot = make_bot()
        analysis = make_analysis(state=state)
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(return_value=None)
        signal = await bot.run_once('ETHUSDT')
        assert signal.grid_params is None
        assert signal.capital_feasibility is None
        assert '资金配置' not in signal.message
        assert '建议网格参数' not in signal.message

    @pytest.mark.asyncio
    async def test_ac10_dangerous_template_snapshot(self):
        """AC-10 快照：价格紧急态模板保持 V2.5 原文"""
        bot = make_bot()
        analysis = make_analysis(state=MarketState.PRICE_EMERGENCY)
        expected = bot._generate_price_emergency_message('ETHUSDT', analysis)
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(return_value=None)
        signal = await bot.run_once('ETHUSDT')
        assert signal.message == expected

    @pytest.mark.asyncio
    async def test_unknown_state_defaults_to_oscillation(self):
        """防御分支：未知市场状态落到 else，默认按震荡计算网格参数"""

        class _UnknownState:
            """与任何 MarketState 枚举均不等（落入 else），value 映射到合法状态字符串"""

            value = '震荡市场'

        bot = make_bot()
        analysis = make_analysis()
        analysis.state = _UnknownState()
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(
            return_value=make_advice(Decimal('5000')))
        bot.margin_advisor._last_baseline_atr = Decimal('18')
        signal = await bot.run_once('ETHUSDT')
        assert signal.grid_params is not None
        assert signal.capital_feasibility is not None
        assert signal.position_valid is True

    @pytest.mark.asyncio
    async def test_infeasible_ncap_zero_hides_reachable_line(self):
        """N_cap=0：资金连 1 格都不足时，不可行文案不展示「可达上限」行"""
        bot = make_bot()
        signal = await self._run_once(bot, advice=make_advice(Decimal('1')))
        assert signal.position_valid is False
        assert signal.capital_feasibility.feasible is False
        assert '可达上限' not in signal.message
        assert '无法支撑最少 5 格' in signal.message
        # 出口方案仍给出（按 N_min 反推）
        assert '保证金提高至 1280 USDT' in signal.message


class TestCapitalConstraintRollback:
    """T5：capital_constraint.enabled=false 整体回退 V2.5（含 FR-01 口径）"""

    @pytest.mark.asyncio
    async def test_rollback_margin_caliber_and_no_press(self):
        config = load_yaml_config()
        config['grid']['capital_constraint']['enabled'] = False
        bot = make_bot(config)
        spy = MagicMock(wraps=bot.grid_calculator.resolve_capital_feasibility)
        bot.grid_calculator.resolve_capital_feasibility = spy
        analysis = make_analysis()
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(
            return_value=make_advice(M_ADVISED))
        # last_baseline_atr 为只读 property，compute_advice 被 mock 后直接写底层字段
        bot.margin_advisor._last_baseline_atr = Decimal('18')
        signal = await bot.run_once('ETHUSDT')

        # FR-01 口径回退：即使建议保证金 936，可行性仍用固定 500
        assert spy.call_args.kwargs['margin'] == Decimal('500')
        assert spy.call_args.kwargs['enforce_capital'] is False
        assert signal.margin_used == Decimal('500')
        # C3 不压制：网格数保持 N_struct=7
        assert signal.grid_params.grid_count == 7
        # V2.5 旧文案（每格 500×10/(2558.48×7)=0.28 张，7 格需 1791）
        assert '每格仅0.28张，不足1张' in signal.message
        assert '请将保证金增至1791 USDT' in signal.message
        assert '方案1（保守）' in signal.message

    @pytest.mark.asyncio
    async def test_rollback_valid_funding_copy(self):
        """回退分支：资金充足时沿用 V2.5 有效文案（取整后N张）"""
        config = load_yaml_config()
        config['grid']['capital_constraint']['enabled'] = False
        config['trading']['margin'] = 2000
        bot = make_bot(config)
        analysis = make_analysis()
        params, feasibility = await bot._calculate_grid_params(
            'ETHUSDT', analysis, margin=Decimal('2000'), atr_baseline=Decimal('18')
        )
        text = bot._build_legacy_funding_text(analysis, params, feasibility)
        assert '每格1.12张（取整后1张）' in text

    @pytest.mark.asyncio
    async def test_rollback_margin_section_keeps_v25_copy(self):
        """回退分支：保证金引导尾部行保持 V2.5 固定文案"""
        config = load_yaml_config()
        config['grid']['capital_constraint']['enabled'] = False
        bot = make_bot(config)
        analysis = make_analysis()
        bot.market_detector.detect_market_state = AsyncMock(return_value=analysis)
        bot.margin_advisor.compute_advice = AsyncMock(
            return_value=make_advice(M_ADVISED, action=ACTION_ADD))
        # last_baseline_atr 为只读 property，compute_advice 被 mock 后直接写底层字段
        bot.margin_advisor._last_baseline_atr = Decimal('18')
        signal = await bot.run_once('ETHUSDT')
        assert '请确认每格下单张数≥1张' in signal.message
        assert '按建议保证金' not in signal.message
