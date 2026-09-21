"""
保证金智能引导模块（V2.5 多因子）

基于五因子（市场状态、趋势强度、波动率、BTC 一致性、资金费率）计算建议保证金，
并输出加码 / 减码 / 清仓 / 无需调整的动作建议，供网格信号灯推送。

设计约束：
- 所有业务阈值一律从 config.yaml 的 margin_guide 配置节读取（零硬编码）
- 所有计算使用 Decimal，中间系数保留 4 位小数（quantize）
- 建议保证金 = round(基础保证金 × 状态 × 趋势强度 × 波动率 × BTC一致性 × 资金费率, 0)
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple
import structlog

from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from .grid_calculator import GridCalculator
from .market_state import MarketState, MarketAnalysis, MarketStateDetector


logger = structlog.get_logger()

# 动作方向常量（与推送模板文案对应）
ACTION_ADD = 'ADD'
ACTION_REDUCE = 'REDUCE'
ACTION_CLEAR = 'CLEAR'
ACTION_NONE = 'NONE'

# 危险状态集合：这些状态下 skipped=True，不拼装保证金引导板块
DANGEROUS_STATES = frozenset({
    MarketState.PRICE_EMERGENCY,
    MarketState.EARLY_WARNING_15M,
    MarketState.TREND_CONFIRMED_1H,
    MarketState.TREND_ACCELERATING,
    MarketState.EXTREME_STRONG_TREND,
    MarketState.NORMAL_STRONG_TREND,
    MarketState.VOLATILITY_ABNORMAL,
})

# 趋势强度系数公式常数（数学常数，非可调业务阈值，故未列入配置结构）
TREND_SOFT_SLOPE_DIVISOR = Decimal('100')  # ADX<soft_max 段的斜率除数
TREND_MID_SLOPE_DIVISOR = Decimal('50')    # soft_max≤ADX<hard_max 段的斜率除数
TREND_HARD_SLOPE_DIVISOR = Decimal('40')   # ADX≥hard_max 段的斜率除数
TREND_HARD_BASE = Decimal('0.5')           # 强趋势段基准下限
TREND_UNITY = Decimal('1')                 # 中性基准 1.0


@dataclass
class MarginAdvice:
    """
    保证金引导建议数据类

    Attributes:
        suggested_margin: 建议保证金（round 取整）
        current_margin: 当前保证金（trading.margin，第一版不查实际持仓）
        action: ADD / REDUCE / CLEAR / NONE
        adjust_amount: min(|建议-当前|, max_step)；NONE 为 0
        coeffs: 五因子明细（state/trend/volatility/btc/funding，另含 adx_1h 展示辅助）
        funding_rate: 原始资金费率（获取失败为 None）
        btc_state: BTC 市场状态（跳过/异常时为 None）
        divergence_line: BTC 背离一行提示（仅趋势态且检测成功）
        skipped: 危险态时为 True，不拼装板块
    """
    suggested_margin: Decimal
    current_margin: Decimal
    action: str
    adjust_amount: Decimal
    coeffs: Dict[str, Decimal]
    funding_rate: Optional[float]
    btc_state: Optional[MarketState]
    divergence_line: Optional[str]
    skipped: bool
    cooldown_blocked: bool = False   # 冷却拦截（同方向未满冷却期）
    blocked_direction: Optional[str] = None  # 被拦截的原动作方向（ADD/REDUCE/CLEAR）


class MarginAdvisor:
    """
    保证金智能引导器

    负责计算五因子系数、建议保证金、动作判定与冷却控制，
    并格式化推送板块文本。内部方法均可独立单测。
    """

    def __init__(
        self,
        config: dict,
        binance_client: BinanceClient,
        kline_service: KLineService,
        btc_detector: MarketStateDetector,
        grid_calculator: GridCalculator
    ):
        """
        初始化保证金引导器

        Args:
            config: 策略配置字典（含 margin_guide 配置节）
            binance_client: 币安客户端（资金费率）
            kline_service: K 线服务（基准 ATR / 背离检测）
            btc_detector: BTC 市场状态检测器（独立实例，不污染 ETH 检测器）
            grid_calculator: 网格计算器（基准 ATR 计算）
        """
        if not config or not isinstance(config, dict):
            raise ValueError("配置不能为空且必须是字典")

        self.binance_client = binance_client
        self.kline_service = kline_service
        self.btc_detector = btc_detector
        self.grid_calculator = grid_calculator

        # 基础保证金（与 signal_bot 中 trading.margin 保持一致）
        self.base_margin = Decimal(str(config.get('trading', {}).get('margin', 500)))
        # K 线数量（基准 ATR 取数，与 signal_bot 共用 kline.limit 配置）
        self._kline_limit = int(config.get('kline', {}).get('limit', 100))

        # 读取 margin_guide 配置节（缺失时兜底默认值，与 config.yaml 缺省一致）
        mg = config.get('margin_guide', {}) or {}
        self._load_state_config(mg)
        self._load_trend_config(mg)
        self._load_factor_config(mg)
        self._load_trigger_config(mg)

        # 实例级状态缓存（重启即清空）
        self._cooldowns: Dict[str, datetime] = {}
        # 资金费率缓存：symbol -> (时间戳, 系数, 原始费率)
        self._funding_cache: Dict[str, Tuple[datetime, Decimal, Optional[float]]] = {}
        self._last_baseline_atr: Optional[Decimal] = None

    @property
    def last_baseline_atr(self) -> Optional[Decimal]:
        """最近一次计算的基准 ATR（供网格参数复用，避免重复拉取 K 线）"""
        return self._last_baseline_atr

    def _load_state_config(self, mg: dict) -> None:
        """读取状态系数与强震荡细分配置"""
        self._state_coeff_cfg = mg.get('state_coeff', {}) or {}
        so = mg.get('strong_oscillation', {}) or {}
        self._strong_osc_coeff = Decimal(str(so.get('coeff', 1.2)))
        self._strong_osc_adx = Decimal(str(so.get('adx_threshold', 18)))

    def _load_trend_config(self, mg: dict) -> None:
        """读取趋势强度系数配置"""
        ts = mg.get('trend_strength', {}) or {}
        self._ts_soft_max = Decimal(str(ts.get('adx_soft_max', 20)))
        self._ts_hard_max = Decimal(str(ts.get('adx_hard_max', 30)))
        self._ts_min_value = Decimal(str(ts.get('min_value', 0.0)))

    def _load_factor_config(self, mg: dict) -> None:
        """读取波动率 / BTC 一致性 / 资金费率 / 冷却 / 背离 / 取整配置"""
        vol = mg.get('volatility', {}) or {}
        self._vol_min_coeff = Decimal(str(vol.get('min_coeff', 0.5)))
        self._vol_max_coeff = Decimal(str(vol.get('max_coeff', 1.5)))
        self._vol_degrade = Decimal(str(vol.get('degrade', 1.0)))

        bc = mg.get('btc_consistency', {}) or {}
        self._btc_symbol = bc.get('btc_symbol', 'BTCUSDT')
        self._btc_osc_coeff = Decimal(str(bc.get('oscillation_coeff', 0.7)))
        self._btc_same_coeff = Decimal(str(bc.get('same_trend_coeff', 1.0)))
        self._btc_reverse_coeff = Decimal(str(bc.get('reverse_trend_coeff', 0.4)))
        self._btc_eth_osc_coeff = Decimal(str(bc.get('eth_oscillation_coeff', 1.0)))
        self._btc_degrade = Decimal(str(bc.get('degrade', 1.0)))

        fr = mg.get('funding_rate', {}) or {}
        self._fr_high_threshold = Decimal(str(fr.get('high_threshold', 0.0005)))
        self._fr_low_threshold = Decimal(str(fr.get('low_threshold', -0.0005)))
        self._fr_high_coeff = Decimal(str(fr.get('high_coeff', 0.7)))
        self._fr_neutral_coeff = Decimal(str(fr.get('neutral_coeff', 1.0)))
        self._fr_low_coeff = Decimal(str(fr.get('low_coeff', 1.2)))
        self._fr_cache_hours = float(fr.get('cache_hours', 8))
        self._fr_degrade = Decimal(str(fr.get('degrade', 1.0)))

        cd = mg.get('cooldown', {}) or {}
        self._cooldown_hours = float(cd.get('same_direction_hours', 4))

        dv = mg.get('divergence', {}) or {}
        self._div_kline_count = int(dv.get('kline_count', 24))
        self._div_interval = dv.get('interval', '1h')

        self._rounding = int(mg.get('rounding', 0))

    def _load_trigger_config(self, mg: dict) -> None:
        """读取触发阈值与调整幅度配置"""
        tr = mg.get('trigger', {}) or {}
        self._tr_increase_ratio = Decimal(str(tr.get('increase_ratio', 1.2)))
        self._tr_decrease_ratio = Decimal(str(tr.get('decrease_ratio', 0.8)))

        adj = mg.get('adjustment', {}) or {}
        self._max_step = Decimal(str(adj.get('max_step', 150)))
        self._max_step_ratio = Decimal(str(adj.get('max_step_ratio', 0.3)))

    @staticmethod
    def _q4(value: Decimal) -> Decimal:
        """中间系数保留 4 位小数"""
        return value.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)

    @staticmethod
    def _fmt_coeff(value: Decimal) -> str:
        """系数展示：去尾零但至少保留 1 位小数（1.0 / 0.96 / 1.3333）"""
        text = f"{float(value):.4f}".rstrip('0').rstrip('.')
        return text if '.' in text else f"{text}.0"

    # ============================================================
    # 五因子系数计算
    # ============================================================

    def _state_coeff(self, state: MarketState, adx_1h: Decimal) -> Decimal:
        """
        状态系数：按 MarketState 枚举映射

        基础系数 = 映射 MarketState 枚举（价格紧急/极端->0，普通强趋势->0.3，
        弱趋势->0.6，震荡->1.0）；细分修正：震荡且 1h ADX<阈值时按强震荡（1.2）。
        未在配置中列出的枚举返回 0（危险态，保守处理）。
        """
        if state == MarketState.OSCILLATION and adx_1h < self._strong_osc_adx:
            return self._q4(self._strong_osc_coeff)
        coeff = self._state_coeff_cfg.get(state.name)
        if coeff is None:
            return self._q4(Decimal('0'))
        return self._q4(Decimal(str(coeff)))

    def _state_desc(self, state: MarketState, adx_1h: Decimal) -> str:
        """状态描述（因子行展示用，与 _state_coeff 判定一致）"""
        if state == MarketState.OSCILLATION and adx_1h < self._strong_osc_adx:
            return '强震荡'
        descs = {
            'PRICE_EMERGENCY': '价格紧急', 'EARLY_WARNING_15M': '早期预警',
            'TREND_CONFIRMED_1H': '趋势确认', 'TREND_ACCELERATING': '趋势加速',
            'EXTREME_STRONG_TREND': '极端强趋势', 'NORMAL_STRONG_TREND': '普通强趋势',
            'VOLATILITY_ABNORMAL': '波动率异常', 'WEAK_TREND': '弱趋势',
            'OSCILLATION': '震荡',
        }
        return descs.get(state.name, state.value)

    def _trend_strength_coeff(self, adx_1h: Decimal) -> Decimal:
        """
        趋势强度系数

        公式（soft_max=20, hard_max=30，除数/基准为公式常数）：
        - ADX<20: 1.0+(20-ADX)/100
        - 20≤ADX<30: 1.0-(ADX-20)/50
        - ADX≥30: max(min_value, 0.5-(ADX-30)/40)
        """
        if adx_1h < self._ts_soft_max:
            coeff = TREND_UNITY + (self._ts_soft_max - adx_1h) / TREND_SOFT_SLOPE_DIVISOR
        elif adx_1h < self._ts_hard_max:
            coeff = TREND_UNITY - (adx_1h - self._ts_soft_max) / TREND_MID_SLOPE_DIVISOR
        else:
            coeff = TREND_HARD_BASE - (adx_1h - self._ts_hard_max) / TREND_HARD_SLOPE_DIVISOR
            coeff = max(self._ts_min_value, coeff)
        return self._q4(coeff)

    def _volatility_coeff(self, baseline_atr: Optional[Decimal], current_atr: Decimal) -> Decimal:
        """
        波动率系数：clamp(基准ATR/当前ATR, min_coeff, max_coeff)

        基准 ATR 计算失败 -> 1.0；当前 ATR<=0 -> degrade(1.0)
        """
        if baseline_atr is None or current_atr <= Decimal('0'):
            return self._q4(self._vol_degrade)
        ratio = baseline_atr / current_atr
        clamped = min(self._vol_max_coeff, max(self._vol_min_coeff, ratio))
        return self._q4(clamped)

    @staticmethod
    def _direction(analysis: MarketAnalysis) -> str:
        """EMA 方向判定：UP=多头排列，DOWN=空头排列"""
        return 'UP' if analysis.ema20_1h > analysis.ema50_1h else 'DOWN'

    def _btc_consistency_coeff(self, eth_analysis: MarketAnalysis, btc_analysis: MarketAnalysis) -> Decimal:
        """
        BTC 一致性系数

        - BTC 震荡 -> 0.7
        - BTC 与 ETH 同向 -> 1.0
        - BTC 与 ETH 反向 -> 0.4
        """
        if btc_analysis.state == MarketState.OSCILLATION:
            return self._q4(self._btc_osc_coeff)
        if self._direction(btc_analysis) == self._direction(eth_analysis):
            return self._q4(self._btc_same_coeff)
        return self._q4(self._btc_reverse_coeff)

    async def _funding_rate_coeff(self, symbol: str) -> Decimal:
        """
        资金费率系数（实例级缓存 cache_hours 小时）

        - r>high_threshold -> high_coeff(0.7)
        - 区间内 -> neutral_coeff(1.0)
        - r<low_threshold -> low_coeff(1.2)
        - 获取异常 -> degrade(1.0)，同样进入缓存避免频繁请求
        """
        now = datetime.now()
        cached = self._funding_cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < self._fr_cache_hours * 3600:
            return cached[1]
        rate: Optional[float] = None
        coeff = self._fr_degrade
        try:
            raw_rate = await self.binance_client.get_funding_rate(symbol)
            rate = float(raw_rate)
            r = Decimal(str(raw_rate))
            if r > self._fr_high_threshold:
                coeff = self._fr_high_coeff
            elif r < self._fr_low_threshold:
                coeff = self._fr_low_coeff
            else:
                coeff = self._fr_neutral_coeff
        except Exception as e:
            logger.warning(f"资金费率获取失败，系数降级为{self._fr_degrade}",
                           symbol=symbol, error=str(e))
        self._funding_cache[symbol] = (now, coeff, rate)
        return self._q4(coeff)

    # ============================================================
    # 动作判定与冷却控制
    # ============================================================

    def _evaluate_action(self, suggested: Decimal, current: Decimal) -> str:
        """
        动作判定：CLEAR > ADD > REDUCE > NONE

        - suggested<=0 -> CLEAR（建议清仓）
        - ratio>increase_ratio -> ADD；ratio<decrease_ratio -> REDUCE；否则 NONE
        """
        if suggested <= Decimal('0'):
            return ACTION_CLEAR
        ratio = suggested / current
        if ratio > self._tr_increase_ratio:
            return ACTION_ADD
        if ratio < self._tr_decrease_ratio:
            return ACTION_REDUCE
        return ACTION_NONE

    def _check_cooldown(self, direction: str, now: datetime) -> bool:
        """
        冷却检查：同方向距上次 < cooldown_hours 时拦截（返回 False）

        不同 direction（含清仓 vs 减码）互不拦截，反向立即推送。
        """
        last = self._cooldowns.get(direction)
        if last is None:
            return True
        hours_since = (now - last).total_seconds() / 3600
        return hours_since >= self._cooldown_hours

    # ============================================================
    # 基准 ATR 与 BTC 背离检测
    # ============================================================

    async def _load_baseline_atr(self, symbol: str) -> Optional[Decimal]:
        """加载基准 ATR（1d K 线，与 signal_bot 取数方式一致），失败返回 None"""
        try:
            klines = await self.kline_service.get_klines(
                symbol=symbol, interval='1d', limit=self._kline_limit
            )
            return self.grid_calculator.calculate_baseline_atr(klines)
        except Exception as e:
            logger.warning("基准ATR计算失败，波动率系数降级", symbol=symbol, error=str(e))
            return None

    async def _check_btc_divergence(self, symbol: str, eth_analysis: MarketAnalysis) -> Optional[str]:
        """
        BTC 背离检测（仅趋势态且 BTC 检测成功时由 compute_advice 调用）

        取近 24 根 1h K 线（ETH/BTC 各一次）：
        - ETH 创新高且 BTC 未创新高 -> 假突破提示
        - ETH 创新低且 BTC 未创新低 -> 假跌破提示
        - 任何异常 -> None
        """
        try:
            eth_klines = await self.kline_service.get_klines(
                symbol=symbol, interval=self._div_interval, limit=self._div_kline_count
            )
            btc_klines = await self.kline_service.get_klines(
                symbol=self._btc_symbol, interval=self._div_interval, limit=self._div_kline_count
            )
            if not eth_klines or not btc_klines:
                return None
            eth_high = max(Decimal(str(k['high'])) for k in eth_klines)
            eth_low = min(Decimal(str(k['low'])) for k in eth_klines)
            btc_high = max(Decimal(str(k['high'])) for k in btc_klines)
            btc_low = min(Decimal(str(k['low'])) for k in btc_klines)
            eth_price = eth_analysis.current_price
            btc_price = Decimal(str(btc_klines[-1]['close']))
            if eth_price >= eth_high and btc_price < btc_high:
                return "BTC未创新高，ETH上涨可能为假突破"
            if eth_price <= eth_low and btc_price > btc_low:
                return "BTC未创新低，ETH下跌可能为假跌破"
            return None
        except Exception as e:
            logger.warning("BTC背离检测失败，跳过", symbol=symbol, error=str(e))
            return None

    # ============================================================
    # 主流程
    # ============================================================

    async def _compute_factors(self, symbol: str, eth_analysis: MarketAnalysis) -> Dict:
        """
        计算五因子系数并组装明细（含 BTC 一致性检测、资金费率）

        Returns:
            dict: {coeffs, btc_state, funding_rate}
        """
        state_coeff = self._state_coeff(eth_analysis.state, eth_analysis.adx_1h)
        trend_coeff = self._trend_strength_coeff(eth_analysis.adx_1h)
        baseline_atr = await self._load_baseline_atr(symbol)
        self._last_baseline_atr = baseline_atr
        vol_coeff = self._volatility_coeff(baseline_atr, eth_analysis.atr_smooth)

        # BTC 一致性：ETH 震荡时跳过 BTC 检测，直接 1.0
        btc_state: Optional[MarketState] = None
        btc_coeff = self._btc_degrade
        if eth_analysis.state == MarketState.OSCILLATION:
            btc_coeff = self._btc_eth_osc_coeff
        else:
            try:
                btc_analysis = await self.btc_detector.detect_market_state(self._btc_symbol)
                btc_state = btc_analysis.state
                btc_coeff = self._btc_consistency_coeff(eth_analysis, btc_analysis)
            except Exception as e:
                logger.warning("BTC市场状态检测失败，一致性系数降级", symbol=symbol, error=str(e))

        funding_coeff = await self._funding_rate_coeff(symbol)
        cached_fr = self._funding_cache.get(symbol)
        funding_rate = cached_fr[2] if cached_fr else None

        coeffs = {
            'state': state_coeff,
            'trend': trend_coeff,
            'volatility': vol_coeff,
            'btc': btc_coeff,
            'funding': funding_coeff,
            'adx_1h': eth_analysis.adx_1h,
            'state_name': eth_analysis.state.name,
            'eth_oscillation': (Decimal('1') if eth_analysis.state == MarketState.OSCILLATION
                                else Decimal('0')),
        }
        return {'coeffs': coeffs, 'btc_state': btc_state, 'funding_rate': funding_rate}

    def _resolve_action(self, suggested: Decimal, current: Decimal) -> Tuple[str, Decimal, bool, Optional[str]]:
        """
        动作判定 + 冷却控制 + 单次调整量

        Returns:
            (action, adjust_amount, cooldown_blocked, blocked_direction)
        """
        if current == Decimal('0'):
            action = ACTION_NONE
        else:
            action = self._evaluate_action(suggested, current)

        now = datetime.now()
        cooldown_blocked = False
        blocked_direction: Optional[str] = None
        if action in (ACTION_ADD, ACTION_REDUCE, ACTION_CLEAR) and not self._check_cooldown(action, now):
            blocked_direction = action
            action = ACTION_NONE
            cooldown_blocked = True
        if action in (ACTION_ADD, ACTION_REDUCE, ACTION_CLEAR):
            self._cooldowns[action] = now

        adjust_amount = Decimal('0')
        if action != ACTION_NONE:
            adjust_amount = min(abs(suggested - current), self._max_step)
        return action, adjust_amount, cooldown_blocked, blocked_direction

    async def compute_advice(self, symbol: str, eth_analysis: MarketAnalysis) -> MarginAdvice:
        """
        计算建议保证金（五因子全链路）

        Args:
            symbol: 交易对
            eth_analysis: ETH 市场分析结果

        Returns:
            保证金引导建议
        """
        # 1. 五因子系数 + BTC 一致性 + 资金费率
        factors = await self._compute_factors(symbol, eth_analysis)
        coeffs = factors['coeffs']
        btc_state = factors['btc_state']
        funding_rate = factors['funding_rate']

        # 2. 建议保证金（round 取整）
        product = self.base_margin
        for key in ('state', 'trend', 'volatility', 'btc', 'funding'):
            product *= coeffs[key]
        suggested = product.quantize(Decimal('1').scaleb(-self._rounding), rounding=ROUND_HALF_UP)
        current = self.base_margin

        # 3. 动作判定 + 冷却 + 调整量
        action, adjust_amount, cooldown_blocked, blocked_direction = self._resolve_action(
            suggested, current
        )

        # 4. BTC 背离（仅趋势态且 BTC 检测成功时计算）
        divergence_line = await self._maybe_check_divergence(symbol, eth_analysis, btc_state)

        # 5. skipped：危险态不拼装板块
        skipped = eth_analysis.state in DANGEROUS_STATES

        return MarginAdvice(
            suggested_margin=suggested,
            current_margin=current,
            action=action,
            adjust_amount=adjust_amount,
            coeffs=coeffs,
            funding_rate=funding_rate,
            btc_state=btc_state,
            divergence_line=divergence_line,
            skipped=skipped,
            cooldown_blocked=cooldown_blocked,
            blocked_direction=blocked_direction,
        )

    async def _maybe_check_divergence(self, symbol: str, analysis: MarketAnalysis,
                                      btc_state: Optional[MarketState]) -> Optional[str]:
        """BTC 背离检测（仅趋势态且 BTC 检测成功时执行），失败返回 None"""
        is_trend_state = analysis.state not in (MarketState.OSCILLATION, MarketState.WEAK_TREND)
        if not is_trend_state or btc_state is None:
            return None
        return await self._check_btc_divergence(symbol, analysis)

    # ============================================================
    # 推送模板
    # ============================================================

    @staticmethod
    def _action_texts(action: str) -> Tuple[str, str]:
        """动作文案与动词映射：(方向文案, 动词)"""
        mapping = {
            ACTION_ADD: ('建议加码', '加码'),
            ACTION_REDUCE: ('建议减码', '减码'),
            ACTION_CLEAR: ('清仓', '清仓'),
            ACTION_NONE: ('无需调整', '操作'),
        }
        return mapping.get(action, ('无需调整', '操作'))

    def _funding_text(self, advice: MarginAdvice) -> str:
        """资金费率行文案：>0.0005 偏高 / 区间内 中性 / <-0.0005 偏低"""
        if advice.funding_rate is None:
            return "- 资金费率: 获取失败（降级）"
        r = Decimal(str(advice.funding_rate))
        if r > self._fr_high_threshold:
            desc = '偏高'
        elif r < self._fr_low_threshold:
            desc = '偏低'
        else:
            desc = '中性'
        return f"- 资金费率: {advice.funding_rate * 100:.2f}%（{desc}）"

    def _btc_text(self, advice: MarginAdvice) -> str:
        """BTC 一致性行文案（依赖 btc_state 与展示辅助标记）"""
        if advice.coeffs.get('eth_oscillation', Decimal('0')) == Decimal('1'):
            return "跳过（ETH震荡）"
        if advice.btc_state is None:
            return "异常（降级）"
        if advice.btc_state == MarketState.OSCILLATION:
            return "震荡（中性）"
        if advice.coeffs['btc'] == self._btc_reverse_coeff:
            return "反向（背离）"
        return "同向（确认）"

    def _cooldown_text(self) -> str:
        """冷却行文案：剩余时间与上次动作方向；无记录显示「无」"""
        last_time: Optional[datetime] = None
        last_dir: Optional[str] = None
        for direction, ts in self._cooldowns.items():
            if last_time is None or ts > last_time:
                last_time, last_dir = ts, direction
        if last_time is None or last_dir is None:
            return "- 冷却: 无"
        hours_elapsed = (datetime.now() - last_time).total_seconds() / 3600
        remain = max(Decimal('0'), Decimal(str(self._cooldown_hours)) - Decimal(str(hours_elapsed)))
        dir_text = {ACTION_ADD: '加码', ACTION_REDUCE: '减码', ACTION_CLEAR: '清仓'}[last_dir]
        return f"- 冷却: 剩余 {float(remain):.1f}h（上次{dir_text} {last_time.strftime('%H:%M')}）"

    def _section_title(self, advice: MarginAdvice) -> str:
        """板块标题：冷却拦截为「冷却中，暂不操作」，否则按动作文案"""
        if advice.cooldown_blocked:
            return '冷却中，暂不操作'
        action_text, _ = self._action_texts(advice.action)
        return action_text

    def _margin_lines(self, advice: MarginAdvice) -> list:
        """当前/建议保证金行；当前为 0 时提示按建议值入场"""
        lines = []
        current = advice.current_margin
        if current > Decimal('0'):
            pct = (advice.suggested_margin / current - TREND_UNITY) * Decimal('100')
            lines.append(f"- 当前保证金: {float(current):.0f} USDT")
            lines.append(f"- 建议保证金: {float(advice.suggested_margin):.0f} USDT（{float(pct):+.0f}%）")
        else:
            lines.append("- 当前保证金: 0 USDT")
            lines.append(f"- 建议保证金: {float(advice.suggested_margin):.0f} USDT")
            lines.append("- 当前无保证金，建议按建议值入场")
        return lines

    def _action_lines(self, advice: MarginAdvice) -> list:
        """调整幅度行：清仓提示立即清仓，加码/减码提示限幅"""
        lines = []
        if advice.action == ACTION_CLEAR:
            lines.append("- 调整幅度: 建议立即清仓")
        elif advice.action in (ACTION_ADD, ACTION_REDUCE):
            add_reduce = '增加' if advice.action == ACTION_ADD else '减少'
            limit_pct = float(self._max_step_ratio * Decimal('100'))
            lines.append(
                f"- 调整幅度: 建议{add_reduce} {float(advice.adjust_amount):.0f} USDT"
                f"（限幅{limit_pct:.0f}%）"
            )
        return lines

    def _tail_lines(self, advice: MarginAdvice) -> list:
        """尾部细节行：因子/资金费率/BTC一致性/冷却/背离/下单注意"""
        lines = []
        # 因子行（状态描述按 MarketState 枚举 + ADX 细分推导，与 _state_coeff 一致）
        c = advice.coeffs
        state_desc = self._state_desc(MarketState[c['state_name']], c['adx_1h'])
        lines.append(
            f"- 因子: 状态{self._fmt_coeff(c['state'])}"
            f"（{state_desc} ADX={float(c['adx_1h']):.1f}）"
            f" / 趋势{self._fmt_coeff(c['trend'])}"
            f" / 波动{self._fmt_coeff(c['volatility'])}"
            f" / BTC {self._fmt_coeff(c['btc'])} / 费率{self._fmt_coeff(c['funding'])}"
        )
        lines.append(self._funding_text(advice))
        lines.append(f"- BTC一致性: {self._btc_text(advice)}")
        lines.append(self._cooldown_text())
        if advice.divergence_line:
            lines.append(f"- ⚠️ 背离: {advice.divergence_line}")
        if advice.cooldown_blocked:
            verb = {ACTION_ADD: '加码', ACTION_REDUCE: '减码', ACTION_CLEAR: '清仓'}.get(
                advice.blocked_direction, '操作'
            )
            lines.append(
                f"- 说明: 距上次{verb}未满{float(self._cooldown_hours):.0f}h冷却期，"
                "本次仅提示不操作"
            )
        else:
            _, verb = self._action_texts(advice.action)
            lines.append(f"- 注意: {verb}后请确认每格下单张数≥1张")
        return lines

    def format_section(self, advice: MarginAdvice) -> str:
        """
        格式化保证金引导板块（纯文本，行序与需求推送模板一致）

        标题由动作决定（建议加码/建议减码/清仓/无需调整），
        冷却拦截时显示「冷却中，暂不操作」并附加说明行。
        依次为：标题、保证金、调整幅度、因子明细、尾部细节行。
        """
        title = self._section_title(advice)
        lines = [f"💰 保证金引导（{title}）"]
        lines += self._margin_lines(advice)
        lines += self._action_lines(advice)
        lines += self._tail_lines(advice)
        return "\n".join(lines)
