"""MTPCS 激进版（btc_eth_aggressive）回放适配器。

设计边界（Spec F5 / D4）：
- 决策链的每一步都委托 ``strategies/btc_eth_aggressive/core`` 的纯函数，本模块只负责
  「K 线缓冲 → 决策链编排 → OrderIntent」，**不复制实盘公式**、不读系统时钟、不写盘；
- 市场状态（``TREND`` / ``RANGING``）由 S6 的 regime 识别结果**注入**——本适配器
  **不做市场状态识别**，只接受构造期 ``market_state`` 或 ``PreparedContext.extras``
  传入的值；
- 仓位数量由外部注入的 ``quantity_for``（S6 的 sizing 逻辑）决定，适配器不发明数量
  公式、不硬编码数量；
- 所有阈值一律从 ``config``（与实盘配置同结构）读取，模块内不出现业务阈值字面量。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from strategies.btc_eth_aggressive.core import common
from strategies.btc_eth_aggressive.core import direction as direction_mod
from strategies.btc_eth_aggressive.core import exits, filters, scoring

from ..broker import PortfolioView
from ..models import ZERO, Bar, DataBlocked, OrderIntent
from .base import PreparedContext

__all__ = [
    "MARKET_STATE_RANGING",
    "MARKET_STATE_TREND",
    "MtpcsAggressiveAdapter",
]

#: 市场状态名（由 S6 regime 注入，本适配器只做分支）
MARKET_STATE_TREND = "TREND"
MARKET_STATE_RANGING = "RANGING"

#: 取价/取 ATR 的优先周期（与实盘 analyze() 一致取 1h；该周期不在 timeframes 时
#: 回退到配置中最后一个周期，不假设 1h 一定存在）
PRICE_TIMEFRAME = "1h"

#: 拒绝信号时的等级占位（与实盘 analyze() 的初始结果一致）
GRADE_REJECTED = "D"

#: K 线行中的数值字段：缓冲期以字符串保精度，读取时还原为 Decimal
_NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "quote_volume")


class MtpcsAggressiveAdapter:
    """MTPCS 激进版回放适配器（只做决策，不撮合、不读时钟）。"""

    strategy_id = "btc_eth_aggressive"

    #: MTPCS 不依赖外部时点快照（HRS 才需要）
    requires_external_snapshot = False

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        quantity_for: Callable[[str, Dict[str, Any]], Decimal],
        market_state: str = MARKET_STATE_TREND,
    ) -> None:
        """构造适配器。

        Args:
            config: 与实盘同结构的配置，``config["strategy"]`` 下含
                ``timeframes`` / ``scoring`` / ``symbol_config`` / ``risk``。
            quantity_for: 仓位数量注入函数 ``(symbol, decision) -> Decimal``，
                由 S6 的 sizing 逻辑提供；适配器自身不推导数量。
            market_state: ``"TREND"`` 或 ``"RANGING"``，由 S6 regime 注入。
                本 Spec 不实现市场状态识别，仅接受注入值；``extras`` 中的
                ``market_state`` 优先于该构造参数。
        """
        strategy_config = config.get("strategy", {})
        self._timeframes: List[str] = list(strategy_config.get("timeframes", []))
        self._scoring_config: Dict[str, Any] = strategy_config.get("scoring", {})
        self._symbol_config: Dict[str, Any] = strategy_config.get("symbol_config", {})
        self._risk_config: Dict[str, Any] = strategy_config.get("risk", {})
        self._ranging_config: Dict[str, Any] = self._risk_config.get("ranging_strategy", {})
        self._quantity_for = quantity_for
        self._market_state = market_state
        #: 成交量要求加成，由 S6 regime 通过 extras 注入（默认 0.0）
        self._vol_boost = 0.0
        self._bars: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._portfolio: Optional[PortfolioView] = None

    # ------------------------------------------------------------------
    # 协议实现
    # ------------------------------------------------------------------
    def prepare(self, portal: Any) -> PreparedContext:
        """校验 universe × timeframes 的 K 线覆盖度，缺任一组合即 DataBlocked。"""
        universe = portal.universe()
        config_hashes = portal.config_version()

        gaps: List[str] = []
        for symbol in universe.symbols:
            for interval in self._timeframes:
                if not portal.bars(symbol, interval):
                    gaps.append("kline_empty:{0}:{1}".format(symbol, interval))
        if gaps:
            raise DataBlocked(gaps)

        return PreparedContext(
            strategy_id=self.strategy_id,
            as_of=portal.as_of,
            universe=universe,
            config_hashes=config_hashes,
            portfolio=PortfolioView(
                positions=(), last_prices={}, realized_equity=Decimal("0")
            ),
            extras={
                "symbols": list(universe.symbols),
                "intervals": list(self._timeframes),
                "market_state": self._market_state,
            },
        )

    def on_bar(self, bar: Bar, ctx: PreparedContext) -> None:
        """把已收盘 K 线追加到内部缓冲，并刷新只读持仓视图。

        缓冲行字段与实盘 K 线行一致（open_time / close_time / open / high /
        low / close / volume / quote_volume / trades）；价格与量写成字符串以保精度，
        读取时再还原为 Decimal。
        """
        self._bars.setdefault((bar.symbol, bar.interval), []).append(
            {
                "open_time": bar.open_time,
                "close_time": bar.close_time,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "quote_volume": str(bar.quote_volume),
                "trades": int(bar.trades),
            }
        )
        self._portfolio = ctx.portfolio

    def on_snapshot(self, snap: Any, ctx: PreparedContext) -> None:
        """空实现：MTPCS 不依赖外部时点快照（成交/持仓/OI 等快照仅 HRS 使用）。"""
        return None

    def intents(self, ctx: PreparedContext) -> Tuple[OrderIntent, ...]:
        """按 symbol 排序产出市价开仓意图；已有未平仓持仓的 symbol 不加仓。"""
        symbols = ctx.extras.get("symbols") or list(ctx.universe.symbols)
        market_state_name = ctx.extras.get("market_state", self._market_state)
        vol_boost = ctx.extras.get("vol_boost")
        if vol_boost is not None:
            self._vol_boost = float(vol_boost)

        open_symbols = set(
            position.symbol for position in ctx.portfolio.positions if position.is_open()
        )

        intents: List[OrderIntent] = []
        seq = 0
        for symbol in sorted(symbols):
            if symbol in open_symbols:
                continue
            decision = self.decide(symbol, market_state_name)
            if decision is None or decision["direction"] is None:
                continue
            quantity = self._quantity_for(symbol, decision)
            if quantity is None:
                continue
            quantity_dec = Decimal(str(quantity))
            if quantity_dec <= ZERO:
                continue
            seq += 1
            intents.append(
                OrderIntent(
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    seq=seq,
                    side=decision["direction"],
                    order_type="MARKET",
                    quantity=quantity_dec,
                    reason="{0}级信号".format(decision["grade"]),
                    grade=decision["grade"],
                    reference_price=decision["entry_price"],
                )
            )
        return tuple(intents)

    def protective_prices(self, symbol: str) -> Optional[Dict[str, Decimal]]:
        """该 symbol 有未平仓持仓时，用核心退出公式算出止损/TP1/TP2 三个价格。"""
        if self._portfolio is None:
            return None
        for position in self._portfolio.positions:
            if position.symbol != symbol or not position.is_open():
                continue
            if position.entry_price is None or position.atr is None:
                return None
            direction = position.direction
            return {
                "stop_loss_price": exits.calculate_stop_price(
                    direction,
                    position.entry_price,
                    position.atr,
                    position.grade,
                    self._risk_config,
                ),
                "tp1_price": exits.calculate_tp_price(
                    position.entry_price,
                    position.atr,
                    direction,
                    1,
                    position.grade,
                    self._risk_config,
                ),
                "tp2_price": exits.calculate_tp_price(
                    position.entry_price,
                    position.atr,
                    direction,
                    2,
                    position.grade,
                    self._risk_config,
                ),
            }
        return None

    # ------------------------------------------------------------------
    # 纯决策（测试主入口）
    # ------------------------------------------------------------------
    def decide(self, symbol: str, market_state_name: str) -> Optional[Dict[str, Any]]:
        """产出该 symbol 的统一决策结果；K 线周期不全时返回 None。

        拒绝与通过都返回同一结构，便于逐字段断言：
        ``symbol`` / ``direction`` / ``grade`` / ``score`` / ``reason`` /
        ``market_state`` / ``entry_price`` / ``atr`` / ``stop_loss_price`` /
        ``tp1_price`` / ``tp2_price``。

        决策链顺序与实盘 ``analyze()`` 一致（见 Spec F5）：
        入场方向（趋势过滤 / 震荡投票+四护栏）→ 禁止入场第 1 项（K 线 spike）→
        综合评分与等级 → 震荡侧过热降级 → 成交量过滤 → 止损/止盈价格。
        """
        klines = self._klines_for(symbol)
        if klines is None:
            return None

        indicators = common.build_indicators(klines, self._risk_config)
        score = 0.0

        if market_state_name == MARKET_STATE_RANGING:
            direction, reason = self._ranging_direction(indicators, klines)
            if direction is None:
                return self._reject(symbol, market_state_name, reason, score)
        else:
            passed, trend_result = filters.check_trend_filter(
                indicators, klines, self._risk_config
            )
            if not passed:
                return self._reject(symbol, market_state_name, trend_result, score)
            direction = (
                str(trend_result)
                if trend_result
                else direction_mod.determine_direction(indicators, self._timeframes)
            )

        spike_reason = filters.check_kline_spike(
            klines, self._risk_config.get("prohibition", {})
        )
        if spike_reason is not None:
            return self._reject(symbol, market_state_name, spike_reason, score)

        score = float(
            scoring.calculate_score(
                indicators,
                klines,
                self._scoring_config,
                self._risk_config,
                market_state_name,
            )
        )
        grade = scoring.determine_grade(
            score, self._scoring_config, self._symbol_config, symbol
        )

        if market_state_name == MARKET_STATE_RANGING:
            downgraded = filters.apply_overheat_downgrade(
                grade, direction, indicators, klines, self._ranging_config
            )
            if downgraded is None:
                return self._reject(
                    symbol, market_state_name, "过热降级后低于最低允许等级，禁开", score
                )
            grade = downgraded

        vol_pass, vol_reason = filters.check_volume_filter(
            symbol,
            grade,
            klines,
            self._risk_config,
            self._symbol_config,
            self._vol_boost,
        )
        if not vol_pass:
            return self._reject(symbol, market_state_name, vol_reason, score)

        price_interval = (
            PRICE_TIMEFRAME if PRICE_TIMEFRAME in klines else self._timeframes[-1]
        )
        current_price = Decimal(str(klines[price_interval][-1]["close"]))
        raw_atr = indicators[price_interval]["ATR"].iloc[-1]
        # NaN 自比较不相等：指标缺失时 fail-closed，避免 Decimal('NaN') 静默污染价格
        if raw_atr is None or raw_atr != raw_atr:
            return self._reject(symbol, market_state_name, "ATR数据缺失", score)
        atr = Decimal(str(raw_atr))

        return {
            "symbol": symbol,
            "direction": direction,
            "grade": grade,
            "score": score,
            "reason": "",
            "market_state": market_state_name,
            "entry_price": current_price,
            "atr": atr,
            "stop_loss_price": exits.calculate_stop_price(
                direction, current_price, atr, grade, self._risk_config
            ),
            "tp1_price": exits.calculate_tp_price(
                current_price, atr, direction, 1, grade, self._risk_config
            ),
            "tp2_price": exits.calculate_tp_price(
                current_price, atr, direction, 2, grade, self._risk_config
            ),
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _klines_for(self, symbol: str) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """按 timeframes 取该 symbol 的多周期 K 线；缺任一周期或为空返回 None。"""
        klines: Dict[str, List[Dict[str, Any]]] = {}
        for interval in self._timeframes:
            rows = self._bars.get((symbol, interval))
            if not rows:
                return None
            klines[interval] = [_restore_numeric(row) for row in rows]
        if not klines:
            return None
        return klines

    def _ranging_direction(
        self, indicators: Dict, klines: Dict[str, List[Dict[str, Any]]]
    ) -> Tuple[Optional[str], str]:
        """震荡路径：三条件投票定方向，再依序过开仓侧四护栏（镜像实盘顺序）。"""
        if "4h" not in klines:
            return None, "4h数据缺失"
        entry_conditions = self._ranging_config.get("entry_conditions", {})
        direction, reason, _conditions = direction_mod.vote_ranging_direction(
            indicators, klines, entry_conditions
        )
        if direction is None:
            return None, reason

        entry_filters = self._ranging_config.get("entry_filters", {})
        if not entry_filters.get("enabled", True):
            # 开仓侧三机制总开关关闭：仅凭投票方向直接放行
            return direction, ""

        ok, reason, _ratio = filters.evaluate_volatility_regime(
            indicators, self._ranging_config
        )
        if not ok:
            return None, reason
        ok, reason = direction_mod.check_direction_alignment(
            direction, indicators, self._ranging_config
        )
        if not ok:
            return None, reason
        ok, reason = filters.check_volume_confirm(
            direction, indicators, klines, self._ranging_config
        )
        if not ok:
            return None, reason
        ok, reason = filters.check_overheat_ban(
            direction, indicators, klines, self._ranging_config
        )
        if not ok:
            return None, reason
        return direction, ""

    @staticmethod
    def _reject(
        symbol: str, market_state_name: str, reason: Any, score: float = 0.0
    ) -> Dict[str, Any]:
        """拒绝结果（与通过结果同构，方向为 None、等级为 D、价格全为 None）。"""
        return {
            "symbol": symbol,
            "direction": None,
            "grade": GRADE_REJECTED,
            "score": float(score),
            "reason": str(reason),
            "market_state": market_state_name,
            "entry_price": None,
            "atr": None,
            "stop_loss_price": None,
            "tp1_price": None,
            "tp2_price": None,
        }


def _restore_numeric(row: Dict[str, Any]) -> Dict[str, Any]:
    """把缓冲中的字符串数值还原为 Decimal（实盘 K 线行同样是 Decimal 值）。"""
    restored = dict(row)
    for field in _NUMERIC_FIELDS:
        restored[field] = Decimal(row[field])
    return restored
