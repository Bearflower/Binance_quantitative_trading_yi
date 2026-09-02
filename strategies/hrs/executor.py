"""
交易执行模块
负责 HRS 策略的订单执行：开仓、止损、止盈、平仓、开仓延迟处理
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional, List, Callable, Tuple

import pandas as pd
import structlog

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.indicators import TechnicalIndicators
from shared.notification import NotificationClient

from .position_manager import PositionManager


logger = structlog.get_logger()


@dataclass
class OpenResult:
    """开仓执行结果：分离「开仓成交」与「保护单完整性」两个独立信号（FR-01）"""

    order_filled: bool             # 开仓是否成交（决定是否记录持仓）
    protection_complete: bool      # 三类保护单是否完整创建
    failed_roles: List[str]        # 创建失败的保护单角色（"sl"/"tp1"/"tp2"）
    order: Optional[Dict[str, Any]] = None  # 开仓订单信息


@dataclass
class ReplenishResult:
    """补单执行结果：区分「已存在跳过」与「下单失败」（FR-02）"""

    success: bool                  # 整体是否成功（任一角色失败则为 False）
    failed_roles: List[str]        # 下单失败的角色
    placed: int = 0                # 成功下单数量
    note: str = ""                 # 特殊状态（沿用 "price_past_tp2" 语义）


class TradingExecutor:
    """
    交易执行器

    功能：
    - 做空/做多市价开仓
    - 开仓延迟处理（15分钟内未完全成交则取消并反向平仓）
    - 设置ATR止损、紧急止损、最小绝对止损
    - 分批止盈（第一目标30%、第二目标40%、移动止盈30%）
    - 时间止损平仓
    - 交易通知（支持事件开关）
    - 杠杆设置
    """

    def __init__(
        self,
        config: Dict[str, Any],
        binance_api: BinanceClient,
        db: DatabaseManager,
        notification: NotificationClient,
        position_manager: PositionManager,
        should_notify_callback: Optional[Callable] = None,
    ):
        """
        初始化交易执行器

        Args:
            config: 配置字典
            binance_api: 币安API客户端
            db: 数据库管理器
            notification: 通知客户端
            position_manager: 持仓管理器（用于跟踪条件单algoId）
            should_notify_callback: 可选的通知事件开关回调，签名为 should_notify(event_name) -> bool
        """
        self.config = config
        self.binance_api = binance_api
        self.db = db
        self.notification = notification
        self.position_manager = position_manager
        self._should_notify = should_notify_callback

        # 技术指标配置
        atr_config = config.get("atr", {})
        self.atr_period = atr_config.get("period", 14)

        trading_config = config.get("trading", {})

        # 杠杆
        self.leverage = trading_config.get("leverage", 2)

        # 止损配置
        stop_loss_config = trading_config.get("stop_loss", {})
        self.atr_multiplier = stop_loss_config.get("atr_multiplier", 2.5)
        self.emergency_percent = stop_loss_config.get("emergency_percent", 0.015)
        self.min_absolute_percent = stop_loss_config.get("min_absolute_percent", 0.05)

        # 分批止盈
        batch_config = trading_config.get("batch_take_profit", {})
        self.target1_atr_multiplier = batch_config.get("target1_atr_multiplier", 1.5)
        self.target1_close_percent = batch_config.get("target1_close_percent", 0.30)
        self.target2_atr_multiplier = batch_config.get("target2_atr_multiplier", 3.5)
        self.target2_close_percent = batch_config.get("target2_close_percent", 0.40)

        # 开仓超时
        timeout_config = trading_config.get("entry_timeout", {})
        self.entry_timeout_minutes = timeout_config.get("minutes", 15)
        # 开仓超时检查间隔（秒），从配置读取
        self.entry_timeout_check_interval_seconds = timeout_config.get("check_interval_seconds", 10)
        # 订单完全成交判定容差（已成交/委托 ≥ 该比例视为完全成交）
        self.fill_tolerance = timeout_config.get("fill_tolerance", 0.9999)

        # 订单类型配置（全部使用限价单）
        order_type_config = trading_config.get("order_type", {})
        self.order_type_entry = order_type_config.get("entry", "LIMIT")
        self.order_type_close = order_type_config.get("close", "LIMIT")
        self.order_type_stop_loss = order_type_config.get("stop_loss", "STOP")
        self.order_type_take_profit = order_type_config.get("take_profit", "TAKE_PROFIT")
        self.stop_loss_offset = order_type_config.get("stop_loss_offset", 0.002)
        self.take_profit_offset = order_type_config.get("take_profit_offset", 0.0015)

        # 移动止盈和时间止损配置（标准模式）
        trailing_config = trading_config.get("trailing", {})
        self.trailing_atr_multiplier = trailing_config.get("atr_multiplier", 1.5)
        time_stop_config = trading_config.get("time_stop", {})
        self.max_holding_hours = time_stop_config.get("max_holding_hours", 72)

        # V2.4: LV-RM 独立止损止盈配置
        lv_rm_config = config.get("lv_rm", {})
        lv_sl_config = lv_rm_config.get("stop_loss", {})
        lv_tp_config = lv_rm_config.get("take_profit", {})
        lv_time_config = lv_rm_config.get("time_stop", {})
        self.lv_rm_atr_multiplier = lv_sl_config.get("atr_multiplier", 1.5)
        self.lv_rm_emergency_percent = lv_sl_config.get("emergency_percent", 0.01)
        self.lv_rm_min_absolute_percent = lv_sl_config.get("min_absolute_percent", 0.03)
        self.lv_rm_target1_atr_multiplier = lv_tp_config.get("target1_atr_multiplier", 1.0)
        self.lv_rm_target1_close_percent = lv_tp_config.get("target1_close_percent", 0.30)
        self.lv_rm_target2_atr_multiplier = lv_tp_config.get("target2_atr_multiplier", 2.0)
        self.lv_rm_target2_close_percent = lv_tp_config.get("target2_close_percent", 0.40)
        self.lv_rm_trailing_atr_multiplier = lv_tp_config.get("trailing_stop_atr_multiplier", 1.0)
        self.lv_rm_max_holding_hours = lv_time_config.get("max_holding_hours", 48)

        logger.info(
            "交易执行器初始化完成",
            leverage=self.leverage,
            atr_multiplier=self.atr_multiplier,
            emergency_percent=self.emergency_percent,
            entry_timeout_minutes=self.entry_timeout_minutes,
            order_type_entry=self.order_type_entry,
            order_type_stop_loss=self.order_type_stop_loss,
            order_type_take_profit=self.order_type_take_profit,
        )

    async def calculate_atr(self, symbol: str, klines: List[Dict], period: Optional[int] = None) -> float:
        """
        计算 ATR

        Args:
            symbol: 交易对
            klines: K线数据
            period: ATR周期，默认从配置读取

        Returns:
            ATR值
        """
        if period is None:
            period = self.atr_period
        try:
            if not klines or len(klines) < period + 1:
                logger.warning("K线数据不足，无法计算ATR", symbol=symbol, count=len(klines) if klines else 0)
                return 0.0

            df = pd.DataFrame([
                {
                    "high": float(k.get("high", 0)),
                    "low": float(k.get("low", 0)),
                    "close": float(k.get("close", 0)),
                }
                for k in klines
            ])

            atr_series = TechnicalIndicators.calculate_atr(df, period=period)
            atr = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
            logger.info("ATR计算完成", symbol=symbol, atr=atr)
            return atr
        except Exception as e:
            logger.error("ATR计算失败", symbol=symbol, error=str(e))
            return 0.0

    def _get_lv_rm_params(self, entry_mode: str) -> Dict[str, Any]:
        """
        V2.4: 根据入场模式获取止损止盈参数

        LV-RM 使用独立参数（均值回归风格），其他模式使用标准参数。

        Args:
            entry_mode: 入场模式 ("standard", "emm", "semi_emm", "lv_rm")

        Returns:
            包含止损止盈参数的字典
        """
        if entry_mode == "lv_rm":
            return {
                "atr_multiplier": self.lv_rm_atr_multiplier,
                "emergency_percent": self.lv_rm_emergency_percent,
                "min_absolute_percent": self.lv_rm_min_absolute_percent,
                "target1_atr_multiplier": self.lv_rm_target1_atr_multiplier,
                "target1_close_percent": self.lv_rm_target1_close_percent,
                "target2_atr_multiplier": self.lv_rm_target2_atr_multiplier,
                "target2_close_percent": self.lv_rm_target2_close_percent,
                "trailing_atr_multiplier": self.lv_rm_trailing_atr_multiplier,
                "max_holding_hours": self.lv_rm_max_holding_hours,
            }
        return {
            "atr_multiplier": self.atr_multiplier,
            "emergency_percent": self.emergency_percent,
            "min_absolute_percent": self.min_absolute_percent,
            "target1_atr_multiplier": self.target1_atr_multiplier,
            "target1_close_percent": self.target1_close_percent,
            "target2_atr_multiplier": self.target2_atr_multiplier,
            "target2_close_percent": self.target2_close_percent,
            "trailing_atr_multiplier": self.trailing_atr_multiplier,
            "max_holding_hours": self.max_holding_hours,
        }

    async def execute_short(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        score: float,
        entry_mode: str = "standard",
    ) -> OpenResult:
        """
        执行做空交易

        V2.4: 新增 entry_mode 参数，LV-RM 模式使用独立止损止盈参数。
        FR-01: 返回 OpenResult，分离「开仓成交」与「保护单完整性」两个信号。

        Args:
            symbol: 交易对
            entry_price: 入场价格
            atr: ATR值
            quantity: 数量
            score: 评分
            entry_mode: 入场模式 ("standard", "emm", "semi_emm", "lv_rm")

        Returns:
            开仓执行结果（OpenResult）
        """
        try:
            logger.info(
                "准备做空",
                symbol=symbol,
                price=entry_price,
                quantity=float(quantity),
                entry_mode=entry_mode,
            )
            tick_size, step_size = await self._get_symbol_precision(symbol)
            quantity = self._format_quantity(quantity, step_size)

            await self._set_leverage(symbol)

            # 限价做空（使用当前价格作为最优限价）
            order = await self.binance_api.place_order(
                symbol=symbol,
                side="SELL",
                order_type=self.order_type_entry,
                quantity=quantity,
                price=Decimal(str(entry_price)),
                timeInForce="GTC",
            )

            if not order:
                logger.error("做空下单失败", symbol=symbol)
                return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

            # 开仓延迟检查：15分钟内未完全成交则取消并反向平仓
            order = await self._check_order_fill_with_timeout(
                symbol, order, "short", quantity, step_size
            )
            if not order:
                logger.warning("做空开仓超时，信号已放弃", symbol=symbol)
                return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

            # 根据入场模式获取止损止盈参数
            params = self._get_lv_rm_params(entry_mode)
            # 计算止损价格（做空：取三者最大值），用于通知与日志
            stop_loss_price = self._calc_sl_price("short", entry_price, atr, tick_size, params)

            # FR-01: 统一入口创建 SL/TP1/TP2 三类保护单，收集失败角色
            failed_roles = await self._create_protection_orders(
                symbol, "short", entry_price, atr, quantity, tick_size, step_size, params
            )

            # 发送通知
            await self._send_notification(symbol, "做空", entry_price, float(quantity), score, stop_loss_price)

            logger.info(
                "做空执行成功",
                symbol=symbol,
                entry_price=entry_price,
                quantity=float(quantity),
                stop_loss=stop_loss_price,
                failed_roles=failed_roles,
            )

            return OpenResult(
                order_filled=True,
                protection_complete=not failed_roles,
                failed_roles=failed_roles,
                order=order,
            )

        except Exception as e:
            logger.error("做空执行失败", symbol=symbol, error=str(e))
            return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

    async def execute_long(
        self,
        symbol: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        score: float,
        entry_mode: str = "standard",
    ) -> OpenResult:
        """
        执行做多交易

        V2.4: 新增 entry_mode 参数，LV-RM 模式使用独立止损止盈参数。
        FR-01: 返回 OpenResult，分离「开仓成交」与「保护单完整性」两个信号。

        Args:
            symbol: 交易对
            entry_price: 入场价格
            atr: ATR值
            quantity: 数量
            score: 评分
            entry_mode: 入场模式 ("standard", "emm", "semi_emm", "lv_rm")

        Returns:
            开仓执行结果（OpenResult）
        """
        try:
            logger.info(
                "准备做多",
                symbol=symbol,
                price=entry_price,
                quantity=float(quantity),
                entry_mode=entry_mode,
            )
            tick_size, step_size = await self._get_symbol_precision(symbol)
            quantity = self._format_quantity(quantity, step_size)

            await self._set_leverage(symbol)

            # 限价做多（使用当前价格作为最优限价）
            order = await self.binance_api.place_order(
                symbol=symbol,
                side="BUY",
                order_type=self.order_type_entry,
                quantity=quantity,
                price=Decimal(str(entry_price)),
                timeInForce="GTC",
            )

            if not order:
                logger.error("做多下单失败", symbol=symbol)
                return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

            # 开仓延迟检查：15分钟内未完全成交则取消并反向平仓
            order = await self._check_order_fill_with_timeout(
                symbol, order, "long", quantity, step_size
            )
            if not order:
                logger.warning("做多开仓超时，信号已放弃", symbol=symbol)
                return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

            # 根据入场模式获取止损止盈参数
            params = self._get_lv_rm_params(entry_mode)
            # 计算止损价格（做多：取三者最小值），用于通知与日志
            stop_loss_price = self._calc_sl_price("long", entry_price, atr, tick_size, params)

            # FR-01: 统一入口创建 SL/TP1/TP2 三类保护单，收集失败角色
            failed_roles = await self._create_protection_orders(
                symbol, "long", entry_price, atr, quantity, tick_size, step_size, params
            )

            # 发送通知
            await self._send_notification(symbol, "做多", entry_price, float(quantity), score, stop_loss_price)

            logger.info(
                "做多执行成功",
                symbol=symbol,
                entry_price=entry_price,
                quantity=float(quantity),
                stop_loss=stop_loss_price,
                failed_roles=failed_roles,
            )

            return OpenResult(
                order_filled=True,
                protection_complete=not failed_roles,
                failed_roles=failed_roles,
                order=order,
            )

        except Exception as e:
            logger.error("做多执行失败", symbol=symbol, error=str(e))
            return OpenResult(order_filled=False, protection_complete=False, failed_roles=[], order=None)

    def calculate_short_stop_loss(
        self,
        entry_price: float,
        atr: float,
        tick_size: Decimal,
        atr_multiplier: Optional[float] = None,
        emergency_percent: Optional[float] = None,
        min_absolute_percent: Optional[float] = None,
    ) -> float:
        """
        计算做空止损价

        取三者最大值（从配置读取参数）：
        1. ATR硬止损：开仓价 + atr_multiplier × ATR
        2. 紧急止损：开仓价 × (1 + emergency_percent)
        3. 最小绝对止损：开仓价 × (1 + min_absolute_percent)

        V2.4: 支持参数覆盖，用于 LV-RM 独立止损配置。

        Args:
            entry_price: 开仓价
            atr: ATR值
            tick_size: 价格精度
            atr_multiplier: ATR止损倍数，None则使用 self.atr_multiplier
            emergency_percent: 紧急止损比例，None则使用 self.emergency_percent
            min_absolute_percent: 最小绝对止损比例，None则使用 self.min_absolute_percent

        Returns:
            止损价
        """
        atr_mul = atr_multiplier if atr_multiplier is not None else self.atr_multiplier
        emerg_pct = emergency_percent if emergency_percent is not None else self.emergency_percent
        min_abs_pct = min_absolute_percent if min_absolute_percent is not None else self.min_absolute_percent

        atr_stop = entry_price + atr_mul * atr
        emergency_stop = entry_price * (1 + emerg_pct)
        min_absolute_stop = entry_price * (1 + min_abs_pct)

        stop_loss = max(atr_stop, emergency_stop, min_absolute_stop)

        logger.info(
            "做空止损计算",
            entry_price=entry_price,
            atr_stop=atr_stop,
            emergency_stop=emergency_stop,
            min_absolute_stop=min_absolute_stop,
            final=stop_loss,
        )

        return float(self._format_price(Decimal(str(stop_loss)), tick_size))

    def calculate_long_stop_loss(
        self,
        entry_price: float,
        atr: float,
        tick_size: Decimal,
        atr_multiplier: Optional[float] = None,
        emergency_percent: Optional[float] = None,
        min_absolute_percent: Optional[float] = None,
    ) -> float:
        """
        计算做多止损价

        取三者最小值（从配置读取参数）：
        1. ATR硬止损：开仓价 - atr_multiplier × ATR
        2. 紧急止损：开仓价 × (1 - emergency_percent)
        3. 最小绝对止损：开仓价 × (1 - min_absolute_percent)

        V2.4: 支持参数覆盖，用于 LV-RM 独立止损配置。

        Args:
            entry_price: 开仓价
            atr: ATR值
            tick_size: 价格精度
            atr_multiplier: ATR止损倍数，None则使用 self.atr_multiplier
            emergency_percent: 紧急止损比例，None则使用 self.emergency_percent
            min_absolute_percent: 最小绝对止损比例，None则使用 self.min_absolute_percent

        Returns:
            止损价
        """
        atr_mul = atr_multiplier if atr_multiplier is not None else self.atr_multiplier
        emerg_pct = emergency_percent if emergency_percent is not None else self.emergency_percent
        min_abs_pct = min_absolute_percent if min_absolute_percent is not None else self.min_absolute_percent

        atr_stop = entry_price - atr_mul * atr
        emergency_stop = entry_price * (1 - emerg_pct)
        min_absolute_stop = entry_price * (1 - min_abs_pct)

        stop_loss = min(atr_stop, emergency_stop, min_absolute_stop)

        logger.info(
            "做多止损计算",
            entry_price=entry_price,
            atr_stop=atr_stop,
            emergency_stop=emergency_stop,
            min_absolute_stop=min_absolute_stop,
            final=stop_loss,
        )

        return float(self._format_price(Decimal(str(stop_loss)), tick_size))

    def _calc_sl_price(
        self,
        direction: str,
        entry_price: float,
        atr: float,
        tick_size: Decimal,
        params: Dict[str, Any],
    ) -> float:
        """
        计算止损价格（纯函数，供开仓/补单/加仓共用，避免重复逻辑）

        Args:
            direction: 方向 ('short'/'long')
            entry_price: 开仓价
            atr: ATR值
            tick_size: 价格精度
            params: 止损止盈参数（含 atr_multiplier/emergency_percent/min_absolute_percent）

        Returns:
            止损价
        """
        if direction == "short":
            return self.calculate_short_stop_loss(
                entry_price, atr, tick_size,
                atr_multiplier=params["atr_multiplier"],
                emergency_percent=params["emergency_percent"],
                min_absolute_percent=params["min_absolute_percent"],
            )
        return self.calculate_long_stop_loss(
            entry_price, atr, tick_size,
            atr_multiplier=params["atr_multiplier"],
            emergency_percent=params["emergency_percent"],
            min_absolute_percent=params["min_absolute_percent"],
        )

    def _calc_tp_price(
        self,
        direction: str,
        target: str,
        entry_price: float,
        atr: float,
        tick_size: Decimal,
        params: Dict[str, Any],
    ) -> Decimal:
        """
        计算止盈触发价（纯函数）

        Args:
            direction: 方向 ('short'/'long')
            target: 止盈目标 ("tp1"/"tp2")
            entry_price: 开仓价
            atr: ATR值
            tick_size: 价格精度
            params: 止损止盈参数（含 target1/2_atr_multiplier）

        Returns:
            止盈触发价
        """
        key = {"tp1": "target1_atr_multiplier", "tp2": "target2_atr_multiplier"}[target]
        multiplier = Decimal(str(params[key]))
        if direction == "short":
            return self._format_price(Decimal(str(entry_price)) - (Decimal(str(atr)) * multiplier), tick_size)
        return self._format_price(Decimal(str(entry_price)) + (Decimal(str(atr)) * multiplier), tick_size)

    def _calc_tp_qty(self, quantity: Decimal, close_percent: float, step_size: Decimal) -> Decimal:
        """
        计算止盈数量（纯函数）

        Args:
            quantity: 当前持仓数量
            close_percent: 止盈平仓比例
            step_size: 数量精度

        Returns:
            止盈数量
        """
        return self._format_quantity(quantity * Decimal(str(close_percent)), step_size)

    @staticmethod
    def _get_close_side(direction: str) -> str:
        """
        获取平仓方向（纯函数）

        做空需买入回补，做多需卖出平仓。

        Args:
            direction: 持仓方向 ('short'/'long')

        Returns:
            平仓方向 ('BUY'/'SELL')
        """
        return "BUY" if direction == "short" else "SELL"

    def _calc_limit_price(self, direction: str, trigger_price: float, offset: float) -> Decimal:
        """
        计算限价偏移价（纯函数）

        做空限价高于触发价（1 + 偏移），做多限价低于触发价（1 - 偏移）。

        Args:
            direction: 持仓方向 ('short'/'long')
            trigger_price: 触发价（止损价/止盈价）
            offset: 偏移比例（如 stop_loss_offset / take_profit_offset）

        Returns:
            限价
        """
        factor = 1 + offset if direction == "short" else 1 - offset
        return Decimal(str(trigger_price * factor))

    async def _place_protection_order(
        self,
        symbol: str,
        role: str,
        side: str,
        order_type: str,
        stop_price: Decimal,
        limit_price: Decimal,
        quantity: Decimal,
        close_position: bool = False,
    ) -> Dict[str, Any]:
        """
        创建单个保护单（幂等入口，FR-05）

        - 数量 <= 0 时视为「跳过」（非失败）
        - 下单成功且返回 algoId → 写入本地记录
        - 下单失败 → 返回 error，不抛异常（由调用方收集 failed_roles）

        Returns:
            {"success": bool, "skipped": bool, "algo_id": int|None, "error": str|None}
        """
        if quantity is not None and float(quantity) <= 0:
            logger.info("保护单数量为零，跳过", symbol=symbol, role=role)
            return {"success": True, "skipped": True, "algo_id": None, "error": None}

        try:
            order, algo_id = await self._submit_conditional_order(
                symbol=symbol, side=side, order_type=order_type,
                stop_price=stop_price, limit_price=limit_price,
                quantity=quantity, close_position=close_position,
            )
        except Exception as e:
            logger.error("保护单下单失败", symbol=symbol, role=role, error=str(e))
            return {"success": False, "skipped": False, "algo_id": None, "error": str(e)}

        if algo_id is None:
            # 下单成功但未返回 algoId → 视为失败（FR-05）
            reason = "下单成功但未返回algoId" if order else "下单失败"
            logger.error("保护单下单异常", symbol=symbol, role=role, reason=reason)
            return {"success": False, "skipped": False, "algo_id": None, "error": reason}

        self.position_manager.add_algo_id(symbol, role, algo_id)
        return {"success": True, "skipped": False, "algo_id": algo_id, "error": None}

    async def _submit_conditional_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        stop_price: Decimal,
        limit_price: Decimal,
        quantity: Decimal,
        close_position: bool,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        """
        提交条件单并提取 algoId（子函数，供 _place_protection_order 调用）

        Args:
            symbol: 交易对
            side: 买卖方向 (BUY/SELL)
            order_type: 订单类型 (STOP/TAKE_PROFIT)
            stop_price: 触发价
            limit_price: 限价
            quantity: 数量
            close_position: 是否平仓单（仅止损单）

        Returns:
            (订单信息, algoId)；无 algoId 时 algoId 为 None
        """
        kwargs = {"closePosition": True} if close_position else {}
        order = await self.binance_api.place_conditional_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            stop_price=stop_price,
            price=limit_price,
            quantity=quantity,
            **kwargs,
        )
        return order, (order.get("algoId") if order else None)

    async def _create_protection_orders(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        tick_size: Decimal,
        step_size: Decimal,
        params: Dict[str, Any],
    ) -> List[str]:
        """
        创建 SL/TP1/TP2 三类保护单并收集结果（FR-01 收敛入口）

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 开仓价
            atr: ATR值
            quantity: 开仓数量
            tick_size: 价格精度
            step_size: 数量精度
            params: 止损止盈参数

        Returns:
            失败角色列表（空列表 = 保护单完整）
        """
        failed_roles: List[str] = []

        # 1. 止损单（closePosition 平仓单）
        if not await self._place_sl_order(symbol, direction, entry_price, atr, quantity, tick_size, params):
            failed_roles.append("sl")

        # 2/3. 止盈单（TP1/TP2 共用子函数，消除重复）
        for target in ("tp1", "tp2"):
            if not await self._place_tp_order(
                symbol, direction, target, entry_price, atr,
                quantity, tick_size, step_size, params,
            ):
                failed_roles.append(target)

        if failed_roles:
            logger.warning(
                "保护单创建不完整",
                symbol=symbol,
                direction=direction,
                failed_roles=failed_roles,
            )
        return failed_roles

    async def _place_sl_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        tick_size: Decimal,
        params: Dict[str, Any],
    ) -> bool:
        """
        创建止损保护单（closePosition 平仓单）

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 开仓价
            atr: ATR值
            quantity: 持仓数量
            tick_size: 价格精度
            params: 止损止盈参数

        Returns:
            是否下单成功
        """
        sl_price = self._calc_sl_price(direction, entry_price, atr, tick_size, params)
        sl_limit = self._calc_limit_price(direction, sl_price, self.stop_loss_offset)
        result = await self._place_protection_order(
            symbol=symbol, role="sl", side=self._get_close_side(direction),
            order_type=self.order_type_stop_loss,
            stop_price=Decimal(str(sl_price)), limit_price=sl_limit,
            quantity=quantity, close_position=True,
        )
        return result["success"]

    async def _place_tp_order(
        self,
        symbol: str,
        direction: str,
        target: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        tick_size: Decimal,
        step_size: Decimal,
        params: Dict[str, Any],
    ) -> bool:
        """
        创建单个止盈保护单（TP1/TP2 共用，消除重复代码）

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            target: 止盈目标 ("tp1"/"tp2")
            entry_price: 开仓价
            atr: ATR值
            quantity: 持仓数量
            tick_size: 价格精度
            step_size: 数量精度
            params: 止损止盈参数（含 target{n}_close_percent）

        Returns:
            是否下单成功
        """
        tp_price = self._calc_tp_price(direction, target, entry_price, atr, tick_size, params)
        tp_qty = self._calc_tp_qty(
            quantity, params[f"target{target[-1]}_close_percent"], step_size
        )
        tp_limit = self._calc_limit_price(direction, float(tp_price), self.take_profit_offset)
        result = await self._place_protection_order(
            symbol=symbol, role=target, side=self._get_close_side(direction),
            order_type=self.order_type_take_profit,
            stop_price=tp_price, limit_price=tp_limit, quantity=tp_qty,
        )
        return result["success"]

    async def close_position(
        self,
        symbol: str,
        direction: str,
        close_percent: float = 1.0,
        reason: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        平仓

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            close_percent: 平仓比例
            reason: 平仓原因

        Returns:
            成功返回订单结果字典（含 orderId），失败返回 None
        """
        try:
            positions = await self.binance_api.get_position(symbol)
            position = None
            for pos in positions:
                pos_amt = float(pos.get("positionAmt", 0))
                if direction == "short" and pos_amt < 0:
                    position = pos
                    break
                elif direction == "long" and pos_amt > 0:
                    position = pos
                    break

            if not position:
                logger.warning("未找到持仓", symbol=symbol, direction=direction)
                return None

            position_amt = abs(float(position.get("positionAmt", 0)))
            close_quantity = position_amt * close_percent

            _, step_size = await self._get_symbol_precision(symbol)
            close_quantity_decimal = self._format_quantity(Decimal(str(close_quantity)), step_size)

            side = self._get_close_side(direction)
            # 获取当前价格用于限价平仓
            ticker = await self.binance_api.get_ticker(symbol)
            close_price = float(ticker.get("lastPrice", 0))
            if close_price <= 0:
                logger.warning("无法获取当前价格，使用市价平仓", symbol=symbol)
                order = await self.binance_api.place_order(
                    symbol=symbol,
                    side=side,
                    order_type="MARKET",
                    quantity=close_quantity_decimal,
                )
            else:
                order = await self.binance_api.place_order(
                    symbol=symbol,
                    side=side,
                    order_type=self.order_type_close,
                    quantity=close_quantity_decimal,
                    price=Decimal(str(close_price)),
                    timeInForce="GTC",
                )

            if order:
                logger.info(
                    "平仓成功",
                    symbol=symbol,
                    direction=direction,
                    reason=reason,
                    quantity=float(close_quantity_decimal),
                )
                return order

            return None

        except Exception as e:
            logger.error("平仓失败", symbol=symbol, error=str(e))
            return None

    async def replenish_position_orders(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        target1_reached: bool = False,
        target2_reached: bool = False,
        best_price: float = 0,
    ) -> ReplenishResult:
        """
        补充持仓缺失的止损止盈条件单

        用于策略重启后恢复保护，以及TP1成交后确保TP2存在。
        FR-02/FR-09: 入口先批量取消清场，随后 SL/TP1/TP2 全量重建；下单失败不再静默忽略。

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            entry_price: 开仓均价
            entry_quantity: 初始开仓数量
            atr: ATR值
            target1_reached: 第一目标是否已成交
            target2_reached: 第二目标是否已成交
            best_price: 最佳价格（用于TP2成交后计算移动止盈价）

        Returns:
            补单执行结果（ReplenishResult）
        """
        if atr <= 0:
            logger.warning("ATR无效，跳过补单", symbol=symbol, atr=atr)
            return ReplenishResult(success=False, failed_roles=[], note="invalid_atr")

        tick_size, step_size = await self._get_symbol_precision(symbol)
        params = self._get_lv_rm_params("standard")

        # FR-07/FR-09: 补单前先批量取消清场，随后全量重建
        await self._cancel_old_orders_for_replenish(symbol)

        # 依次补充 SL/TP1/TP2 并汇总结果
        return await self._replenish_all_roles(
            symbol, direction, entry_price, entry_quantity, atr,
            tick_size, step_size, params, target1_reached, target2_reached,
            best_price,
        )

    async def _cancel_old_orders_for_replenish(self, symbol: str) -> None:
        """
        补单前批量取消旧条件单（子函数，供 replenish_position_orders 调用）

        FR-07/FR-09: 取消失败仅告警不阻断，等待下一轮补单兜底恢复。
        """
        cancel_result = await self.position_manager.cancel_all_orders(symbol)
        if cancel_result["failed"] > 0:
            logger.warning(
                "补单前取消旧条件单部分失败，仍继续重建",
                symbol=symbol,
                result=cancel_result,
            )

    async def _replenish_all_roles(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        tick_size: Decimal,
        step_size: Decimal,
        params: Dict[str, Any],
        target1_reached: bool,
        target2_reached: bool,
        best_price: float = 0,
    ) -> ReplenishResult:
        """
        依次补充 SL/TP1/TP2 并汇总结果（子函数，供 replenish_position_orders 调用）

        Returns:
            补单执行结果（ReplenishResult）
        """
        placed = 0
        failed_roles: List[str] = []
        note = ""

        # 止损单（closePosition 平仓单）
        sl_placed, sl_failed = await self._replenish_sl_role(
            symbol, direction, entry_price, entry_quantity, atr, tick_size,
            params, target1_reached, target2_reached,
        )
        placed += sl_placed
        failed_roles.extend(sl_failed)

        # 止盈单 TP1/TP2 共用子函数循环补充（消除重复）
        for target, reached, check_past in (
            ("tp1", target1_reached, False),
            ("tp2", target2_reached, True),
        ):
            tp_placed, tp_failed, tp_note = await self._replenish_tp_role(
                symbol, direction, target, entry_price, entry_quantity, atr,
                tick_size, step_size, params, reached, check_price_past=check_past,
                best_price=best_price,
            )
            placed += tp_placed
            failed_roles.extend(tp_failed)
            if tp_note == "price_past_tp2":
                note = "price_past_tp2"

        return await self._summarize_replenish(
            symbol, direction, placed, failed_roles, note
        )

    async def _summarize_replenish(
        self,
        symbol: str,
        direction: str,
        placed: int,
        failed_roles: List[str],
        note: str,
    ) -> ReplenishResult:
        """
        汇总补单结果并返回（子函数，供 _replenish_all_roles 调用）

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            placed: 成功下单数量
            failed_roles: 下单失败的角色列表
            note: 特殊状态（沿用 "price_past_tp2" 语义）

        Returns:
            补单执行结果（ReplenishResult）
        """
        logger.info(
            "持仓保护订单补充完成",
            symbol=symbol,
            direction=direction,
            placed=placed,
            failed_roles=failed_roles,
            note=note,
        )
        return ReplenishResult(
            success=not failed_roles,
            failed_roles=failed_roles,
            placed=placed,
            note=note,
        )

    async def _replenish_sl_role(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        tick_size: Decimal,
        params: Dict[str, Any],
        target1_reached: bool,
        target2_reached: bool,
    ) -> Tuple[int, List[str]]:
        """
        补充止损单（FR-02），返回 (成功下单数, 失败角色列表)

        两个止盈均已成交时不再补充止损单。
        """
        if target1_reached and target2_reached:
            return 0, []

        sl_price = self._calc_sl_price(direction, entry_price, atr, tick_size, params)
        sl_limit = self._calc_limit_price(direction, sl_price, self.stop_loss_offset)

        result = await self._place_protection_order(
            symbol=symbol, role="sl", side=self._get_close_side(direction),
            order_type=self.order_type_stop_loss,
            stop_price=Decimal(str(sl_price)), limit_price=sl_limit,
            quantity=Decimal(str(entry_quantity)), close_position=True,
        )
        if result["success"]:
            if result["skipped"]:
                return 0, []
            logger.info("止损单已补充", symbol=symbol, stop_price=sl_price)
            return 1, []
        logger.error("止损单补充失败", symbol=symbol, error=result["error"])
        return 0, ["sl"]

    async def _replenish_tp_role(
        self,
        symbol: str,
        direction: str,
        target: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        tick_size: Decimal,
        step_size: Decimal,
        params: Dict[str, Any],
        target_reached: bool,
        check_price_past: bool,
        best_price: float = 0,
    ) -> Tuple[int, List[str], Optional[str]]:
        """
        补充单个止盈单（FR-02），返回 (成功下单数, 失败角色列表, note)

        - target_reached 为 True 时跳过
        - TP2 需检查当前价格是否已过目标价（price_past_tp2）
        - V2.7: TP2 已成交时（target_reached=True & target="tp2"），使用 best_price 下移动止盈单
        """
        # V2.7: TP2 已成交 → 下移动止盈 STOP_LOSS 单保护剩余仓位
        if target_reached and target == "tp2" and best_price > 0 and atr > 0:
            return await self._place_trailing_stop_order(
                symbol, direction, target, entry_price, entry_quantity,
                atr, tick_size, params, best_price,
            )

        if target_reached:
            return 0, [], None

        tp_price = self._calc_tp_price(direction, target, entry_price, atr, tick_size, params)
        tp_qty = self._calc_tp_qty(
            Decimal(str(entry_quantity)), params[f"target{target[-1]}_close_percent"], step_size
        )
        if float(tp_qty) <= 0:
            return 0, [], None

        # 当前价格已过 TP2 目标价 → 激活移动止盈（沿用 price_past_tp2 语义）
        if check_price_past and target == "tp2" and await self._check_tp_price_past(symbol, direction, tp_price):
            logger.info(
                "当前价格已过TP2目标，激活移动止盈",
                symbol=symbol,
                target2_price=float(tp_price),
                direction=direction,
            )
            # V2.7: 价格已过 TP2 也要下移动止盈 STOP_LOSS 单保护剩余仓位
            if best_price > 0 and atr > 0:
                return await self._place_trailing_stop_order(
                    symbol, direction, target, entry_price, entry_quantity,
                    atr, tick_size, params, best_price,
                )
            return 0, [], "price_past_tp2"

        tp_limit = self._calc_limit_price(direction, float(tp_price), self.take_profit_offset)

        return await self._submit_tp_role_order(
            symbol, direction, target, tp_price, tp_limit, tp_qty
        )

    async def _place_trailing_stop_order(
        self,
        symbol: str,
        direction: str,
        target: str,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        tick_size: Decimal,
        params: Dict[str, Any],
        best_price: float,
    ) -> Tuple[int, List[str], Optional[str]]:
        """
        V2.7: TP2已成交后，使用 best_price 计算移动止盈价并下 STOP_LOSS 保护单

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            target: 角色 ("tp2")
            entry_price: 开仓价
            entry_quantity: 开仓数量
            atr: ATR值
            tick_size: 价格精度
            params: 参数（含 trailing_atr_multiplier）
            best_price: 最佳价格（做多=最高价，做空=最低价）

        Returns:
            (成功下单数, 失败角色列表, note)
        """
        trailing_multiplier = params.get("trailing_atr_multiplier", 1.0)
        if direction == "short":
            trailing_stop_price = best_price + atr * trailing_multiplier
        else:
            trailing_stop_price = best_price - atr * trailing_multiplier

        trailing_stop_price = max(trailing_stop_price, 0.01)  # 防止负值
        trailing_stop_price = float(self._format_price(Decimal(str(trailing_stop_price)), tick_size))

        trailing_limit = self._calc_limit_price(direction, trailing_stop_price, self.stop_loss_offset)

        logger.info(
            "TP2已成交，下移动止盈保护单",
            symbol=symbol, role=target, direction=direction,
            best_price=best_price, atr=atr,
            trailing_stop_price=trailing_stop_price,
        )

        result = await self._place_protection_order(
            symbol=symbol, role=f"{target}_trailing", side=self._get_close_side(direction),
            order_type=self.order_type_stop_loss,
            stop_price=Decimal(str(trailing_stop_price)), limit_price=trailing_limit,
            quantity=Decimal(str(entry_quantity)), close_position=True,
        )

        if result["success"]:
            if result["skipped"]:
                return 0, [], None
            logger.info(
                "移动止盈保护单已补充",
                symbol=symbol, role=target,
                stop_price=trailing_stop_price,
            )
            return 1, [], None
        logger.error("移动止盈保护单补充失败", symbol=symbol, role=target, error=result["error"])
        return 0, [f"{target}_trailing"], None

    async def _submit_tp_role_order(
        self,
        symbol: str,
        direction: str,
        target: str,
        tp_price: Decimal,
        tp_limit: Decimal,
        tp_qty: Decimal,
    ) -> Tuple[int, List[str], Optional[str]]:
        """
        提交止盈补单单并处理结果，返回 (成功下单数, 失败角色列表, note)

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            target: 止盈目标 ("tp1"/"tp2")
            tp_price: 止盈触发价
            tp_limit: 止盈限价
            tp_qty: 止盈数量

        Returns:
            (成功下单数, 失败角色列表, note)
        """
        result = await self._place_protection_order(
            symbol=symbol, role=target, side=self._get_close_side(direction),
            order_type=self.order_type_take_profit,
            stop_price=tp_price, limit_price=tp_limit, quantity=tp_qty,
        )
        if result["success"]:
            if result["skipped"]:
                return 0, [], None
            logger.info(
                "止盈单已补充", symbol=symbol, role=target,
                price=float(tp_price), qty=float(tp_qty),
            )
            return 1, [], None
        logger.error("止盈单补充失败", symbol=symbol, role=target, error=result["error"])
        return 0, [target], None

    async def _check_tp_price_past(self, symbol: str, direction: str, tp_price: Decimal) -> bool:
        """
        检查当前价格是否已过止盈目标价（子函数，供补单复用）

        Args:
            symbol: 交易对
            direction: 方向 ('short'/'long')
            tp_price: 止盈目标价

        Returns:
            True: 当前价格已过目标价
        """
        ticker = await self.binance_api.get_ticker(symbol)
        current_price = float(ticker.get("lastPrice", 0))
        return (
            current_price < float(tp_price) if direction == "short"
            else current_price > float(tp_price)
        )

    async def add_to_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr: float,
        quantity: Decimal,
        score: float,
    ) -> Optional[Dict[str, Any]]:
        """
        P2-3: 加仓

        在已有持仓基础上追加仓位，并重新计算止损止盈。

        Args:
            symbol: 交易对
            direction: 方向 ('short' 或 'long')
            entry_price: 当前入场价格
            atr: ATR值
            quantity: 加仓数量
            score: 评分

        Returns:
            订单信息，失败返回 None
        """
        try:
            logger.info(
                "准备加仓",
                symbol=symbol,
                direction=direction,
                price=entry_price,
                add_quantity=float(quantity),
            )
            tick_size, step_size = await self._get_symbol_precision(symbol)
            quantity = self._format_quantity(quantity, step_size)

            if float(quantity) <= 0:
                logger.warning("加仓数量为零，跳过", symbol=symbol)
                return None

            # FR-08: 获取加仓所需参数与止损价（用于通知与日志）
            params = self._get_lv_rm_params("standard")
            stop_loss_price = self._calc_sl_price(direction, entry_price, atr, tick_size, params)

            # FR-08: 先批量取消旧条件单，取消失败则阻断加仓（保持旧仓位保护单不变）
            cancel_result = await self.position_manager.cancel_all_orders(symbol)
            if cancel_result["failed"] > 0:
                logger.error(
                    "加仓取消旧条件单失败，中止加仓",
                    symbol=symbol,
                    result=cancel_result,
                )
                return None

            # 限价加仓（旧单已取消，加仓成交后保护单基于新总量重建）
            side = "SELL" if direction == "short" else "BUY"
            order = await self.binance_api.place_order(
                symbol=symbol,
                side=side,
                order_type=self.order_type_entry,
                quantity=quantity,
                price=Decimal(str(entry_price)),
                timeInForce="GTC",
            )

            if not order:
                logger.error("加仓下单失败（旧条件单已取消，等待补单兜底）", symbol=symbol)
                return None

            # 获取交易所最新持仓总数量（保护单基于加仓后总量计算）
            positions = await self.binance_api.get_position(symbol)
            total_qty = Decimal("0")
            for pos in positions:
                pos_amt = abs(float(pos.get("positionAmt", 0)))
                if pos_amt > 0:
                    total_qty = Decimal(str(pos_amt))
                    break
            if total_qty <= 0:
                total_qty = quantity

            # FR-08: 复用统一入口重下 SL/TP1/TP2（quantity 用交易所最新持仓总量）
            failed_roles = await self._create_protection_orders(
                symbol, direction, entry_price, atr, total_qty, tick_size, step_size, params
            )
            if failed_roles:
                logger.warning(
                    "加仓保护单重建不完整",
                    symbol=symbol,
                    direction=direction,
                    failed_roles=failed_roles,
                )

            # 发送加仓通知
            await self._send_add_position_notification(
                symbol, direction, entry_price, float(quantity), score, stop_loss_price
            )

            logger.info(
                "加仓执行成功",
                symbol=symbol,
                direction=direction,
                add_quantity=float(quantity),
                total_qty=float(total_qty),
                new_stop_loss=stop_loss_price,
            )

            return order

        except Exception as e:
            logger.error("加仓执行失败", symbol=symbol, error=str(e))
            return None

    async def _send_add_position_notification(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        score: float,
        stop_loss: float,
    ) -> None:
        """P2-3: 发送加仓通知"""
        # 检查通知事件开关
        if self._should_notify and not self._should_notify("add_position"):
            return
        try:
            message = (
                f"【HRS策略加仓通知】\n"
                f"交易对: {symbol}\n"
                f"方向: {direction}\n"
                f"加仓数量: {quantity}\n"
                f"加仓价: {entry_price}\n"
                f"新止损价: {stop_loss}\n"
                f"评分: {score:.2f}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self.notification.send(message=message, level="info", project="hrs")
        except Exception as e:
            logger.warning("发送加仓通知失败", error=str(e))

    # ==================== 开仓延迟处理 ====================

    async def _check_order_fill_with_timeout(
        self,
        symbol: str,
        order: Dict[str, Any],
        direction: str,
        original_quantity: Decimal,
        step_size: Decimal,
    ) -> Optional[Dict[str, Any]]:
        """
        检查市价订单是否在超时时间内完全成交

        需求 4.3 节放弃条件 2：
        - 发出市价开仓订单后，15分钟内订单未能完全成交
        - 必须取消未成交部分，若有部分成交则反向平仓
        - 放弃本次信号

        Args:
            symbol: 交易对
            order: 订单信息
            direction: 方向 ('short' 或 'long')
            original_quantity: 原始下单数量
            step_size: 数量精度

        Returns:
            订单信息（完全成交时），超时返回 None
        """
        order_id = order.get("orderId")
        if not order_id:
            logger.warning("订单无ID，无法检查成交状态", symbol=symbol)
            return order  # 无法检查时，信任订单结果

        # 检查初始成交状态
        executed_qty = float(order.get("executedQty", 0))
        orig_qty = float(order.get("origQty", original_quantity))
        if orig_qty <= 0:
            orig_qty = float(original_quantity)

        if executed_qty >= orig_qty * self.fill_tolerance:
            # 已完全成交，直接返回
            logger.info("订单已完全成交", symbol=symbol, executed_qty=executed_qty, orig_qty=orig_qty)
            return order

        logger.warning(
            "订单未完全成交，开始监控超时",
            symbol=symbol,
            executed_qty=executed_qty,
            orig_qty=orig_qty,
            timeout_minutes=self.entry_timeout_minutes,
        )

        # 监控订单，等待完全成交或超时
        deadline = datetime.now(timezone.utc) + timedelta(minutes=self.entry_timeout_minutes)
        while datetime.now(timezone.utc) < deadline:
            await asyncio.sleep(self.entry_timeout_check_interval_seconds)

            try:
                # 查询订单状态
                order_status = await self.binance_api.get_order(symbol, order_id)
                if order_status:
                    executed_qty = float(order_status.get("executedQty", 0))
                    status = order_status.get("status", "")

                    if status == "FILLED" or executed_qty >= orig_qty * self.fill_tolerance:
                        logger.info("订单在超时前完全成交", symbol=symbol, executed_qty=executed_qty)
                        return order_status

                    if status in ("CANCELED", "EXPIRED", "REJECTED"):
                        logger.warning("订单已被取消/过期/拒绝", symbol=symbol, status=status)
                        break

                    logger.debug(
                        "订单成交监控中",
                        symbol=symbol,
                        executed_qty=executed_qty,
                        orig_qty=orig_qty,
                        remaining_seconds=(deadline - datetime.now(timezone.utc)).total_seconds(),
                    )
            except Exception as e:
                logger.warning("查询订单状态失败", symbol=symbol, order_id=order_id, error=str(e))

        # 超时或订单异常，处理取消和反向平仓
        await self._handle_entry_timeout(symbol, order_id, direction, executed_qty, step_size)
        return None

    async def _handle_entry_timeout(
        self,
        symbol: str,
        order_id: int,
        direction: str,
        executed_qty: float,
        step_size: Decimal,
    ) -> None:
        """
        处理开仓超时：取消未成交部分，若有部分成交则反向平仓

        需求 4.3 节放弃条件 2：必须取消未成交部分，并将部分成交反向平仓恢复零敞口。

        Args:
            symbol: 交易对
            order_id: 订单ID
            direction: 方向
            executed_qty: 已成交数量
            step_size: 数量精度
        """
        logger.warning(
            "开仓超时，执行取消和反向平仓",
            symbol=symbol,
            order_id=order_id,
            direction=direction,
            executed_qty=executed_qty,
            timeout_minutes=self.entry_timeout_minutes,
        )

        # 1. 取消未成交部分
        try:
            cancel_result = await self.binance_api.cancel_order(symbol, order_id)
            if cancel_result:
                logger.info("未成交订单已取消", symbol=symbol, order_id=order_id)
        except Exception as e:
            # 订单可能已经成交或不存在，忽略错误
            logger.debug("取消订单失败（可能已成交）", symbol=symbol, order_id=order_id, error=str(e))

        # 2. 若有部分成交，立即反向平仓
        if executed_qty > 0:
            # 使用更大的容差阈值，确保足够覆盖滑点导致的价格变动
            close_qty = self._format_quantity(Decimal(str(executed_qty)), step_size)
            if float(close_qty) <= 0:
                logger.warning(
                    "部分成交数量过小，无法反向平仓",
                    symbol=symbol,
                    executed_qty=executed_qty,
                )
                return

            close_side = self._get_close_side(direction)
            try:
                # 获取当前价格用于限价反向平仓
                ticker = await self.binance_api.get_ticker(symbol)
                reverse_price = float(ticker.get("lastPrice", 0))
                if reverse_price <= 0:
                    close_order = await self.binance_api.place_order(
                        symbol=symbol,
                        side=close_side,
                        order_type="MARKET",
                        quantity=close_qty,
                    )
                else:
                    close_order = await self.binance_api.place_order(
                        symbol=symbol,
                        side=close_side,
                        order_type=self.order_type_close,
                        quantity=close_qty,
                        price=Decimal(str(reverse_price)),
                        timeInForce="GTC",
                    )
                if close_order:
                    logger.warning(
                        "开仓超时，部分成交已反向平仓",
                        symbol=symbol,
                        direction=direction,
                        executed_qty=executed_qty,
                        close_qty=float(close_qty),
                    )
                else:
                    logger.error(
                        "反向平仓下单失败，存在未平仓风险",
                        symbol=symbol,
                        direction=direction,
                        executed_qty=executed_qty,
                    )
            except Exception as e:
                logger.error(
                    "反向平仓异常，存在未平仓风险",
                    symbol=symbol,
                    direction=direction,
                    executed_qty=executed_qty,
                    error=str(e),
                )
        else:
            logger.info("无部分成交，无需反向平仓", symbol=symbol)

    async def _get_symbol_precision(self, symbol: str) -> tuple:
        """获取交易对精度"""
        try:
            info = await self.binance_api.get_symbol_info(symbol)
            tick_size = Decimal(str(info.get("tickSize", "0.01")))
            step_size = Decimal(info.get("stepSize", "0.001"))
            return tick_size, step_size
        except Exception as e:
            logger.warning("获取交易对精度失败，使用默认精度", symbol=symbol, error=str(e))
            return Decimal("0.01"), Decimal("0.001")

    def _format_quantity(self, quantity: Decimal, step_size: Decimal) -> Decimal:
        """格式化数量"""
        if step_size <= 0:
            return quantity
        return (quantity // step_size) * step_size

    def _format_price(self, price: Decimal, tick_size: Decimal) -> Decimal:
        """格式化价格，精度和舍入模式从配置读取"""
        if tick_size <= 0:
            return price
        trading_config = self.config.get("trading", {})
        price_config = trading_config.get("price", {})
        quantize_precision = Decimal(str(price_config.get("quantize_precision", 1)))
        quantize_rounding = price_config.get("quantize_rounding", "ROUND_HALF_UP")
        return (price / tick_size).quantize(quantize_precision, rounding=quantize_rounding) * tick_size

    async def _set_leverage(self, symbol: str) -> None:
        """设置杠杆"""
        try:
            await self.binance_api.set_leverage(symbol, self.leverage)
        except Exception as e:
            logger.warning("设置杠杆失败", symbol=symbol, error=str(e))

    async def _send_notification(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        score: float,
        stop_loss: float,
    ) -> None:
        """发送交易通知"""
        # 检查通知事件开关
        if self._should_notify and not self._should_notify("open_position"):
            return
        try:
            message = (
                f"【HRS策略交易通知】\n"
                f"交易对: {symbol}\n"
                f"方向: {direction}\n"
                f"数量: {quantity}\n"
                f"开仓价: {entry_price}\n"
                f"止损价: {stop_loss}\n"
                f"评分: {score:.2f}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self.notification.send(message=message, level="info", project="hrs")
        except Exception as e:
            logger.warning("发送通知失败", error=str(e))