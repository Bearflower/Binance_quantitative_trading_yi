"""
交易执行模块
执行做空交易、设置止损止盈
"""
from typing import Dict, Any, Optional, List, Tuple
import asyncio
import os
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import structlog

from shared.binance_api import BinanceClient, BinanceAPIError
from shared.database import DatabaseManager
from shared.notification import NotificationClient
from shared.kline_service import KLineService, KLineServiceError
from shared.condition_orders import record_condition_order
# R06 统一订单终态助手（替代内联等待实现，杜绝三策略重复代码）
from shared.order_fill_waiter import (
    OrderFillResult,
    wait_order_final_state,
)
# R03 统一减仓平仓助手（剩余量重算 + reduceOnly + -2022 前置对账 + 撤单后读最终量）
from shared.reduce_only_close import (
    CLOSE_ACCEPTED,
    CLOSE_FAILED,
    CLOSE_FILLED,
    CLOSE_PARTIAL,
    CloseOutcome,
    close_remaining,
)
# R07 开仓占用互斥（锁内原子占位 / 释放 / 过期清理）
from shared.position_ownership import (
    cleanup_expired_claims,
    load_ownership_config,
    release_claim,
    try_claim_symbol,
)
from shared.dynamic_trailing import (
    calculate_dynamic_trailing_stop,
    get_volatility_adjustment,
    TrailingStopResult,
)
from shared.capital_manager import CapitalManager
from shared.position_baseline import (
    build_contract_size_map,
    calc_occupied_margin,
    calc_position_margin,
)
from shared.utils import to_aware_utc


logger = structlog.get_logger()


# 补全条件单的准备结果（P0-3：Phase A 只读准备阶段的三态）
_REPLENISH_READY = 'ready'            # 计划就绪，可进入撤单/挂单
_REPLENISH_NO_POSITION = 'no_position'  # 无空头持仓，视同成功（不撤不挂）
_REPLENISH_FAILED = 'failed'          # 准备失败，禁止撤单

# 保护条件单元信息（消除 SL/TP1/TP2 挂单路径的重复代码）：
# algo_key -> (下单类型, 记录类型, 已存在提示, 成功提示)
_CONDITION_ORDER_META: Dict[str, tuple] = {
    'sl': ('STOP', 'STOP_LOSS', '止损条件单已存在，跳过', '补全止损条件单成功'),
    'tp1': ('TAKE_PROFIT', 'TAKE_PROFIT', 'TP1 止盈条件单已存在，跳过', '补全 TP1 止盈条件单成功'),
    'tp2': ('TAKE_PROFIT', 'TAKE_PROFIT', 'TP2 止盈条件单已存在，跳过', '补全 TP2 止盈条件单成功'),
}

# 缺失保护单类型（类型级）：find_missing_protection 与增量补挂共用，杜绝魔法字符串。
# 说明（方案甲已知局限）：判定为「类型级」而非「档位级」——只要存在任一条 OPEN 的
# TAKE_PROFIT 即视为「止盈单不缺」。若「仅 TP1 丢失、TP2 仍在」则不会补挂 TP1。
_MISSING_SL = "止损单"
_MISSING_TP = "止盈单"


class TradingExecutor:
    """交易执行器

    功能：
    - 执行做空订单
    - 设置止损止盈
    - 记录交易日志
    - 发送通知
    """

    # 订单未找到错误码（用于幂等取消操作）
    _ORDER_NOT_FOUND_CODE = -2011
    # 交易对精度缓存，减少频繁调用 exchangeInfo 的开销
    _precision_cache: Dict[str, tuple] = {}

    def __init__(
        self,
        binance_api: BinanceClient,
        db: DatabaseManager,
        notification: NotificationClient,
        config: Dict[str, Any],
        kline_service: Optional[KLineService] = None,
        config_path: Optional[str] = None,
    ):
        """
        初始化交易执行器

        Args:
            binance_api: Binance API 客户端
            db: 数据库管理器
            notification: 通知客户端
            config: 配置字典
            kline_service: K线服务（用于ATR计算）
            config_path: 配置文件路径（用于资金分配限制）
        """
        self.binance_api = binance_api
        self.db = db
        self.notification = notification
        self.config = config
        self.kline_service = kline_service

        # R07 开仓占用互斥配置（record_name/competing_record_names/enabled/TTL/清理周期等）
        self._ownership = load_ownership_config(config)
        # 过期占用清理的时间戳（单调时钟，避免系统时间回拨影响节流判定）
        self._last_claim_cleanup_at = 0.0

        # 资金分配管理器（方案 D：DB 主来源 + config 兜底，保证金口径）
        resolved_config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )
        self.capital_mgr = CapitalManager(
            resolved_config_path,
            db=db,
            strategy_id=config.get('strategy', {}).get('name', 'new_coin'),
        )

        # 交易配置
        trading_config = config.get('trading', {})
        # 杠杆严格从配置读取（R3）：缺失或 ≤ 0 视为配置错误，禁止用 1 或 2 猜测默认值
        self.leverage = self._resolve_leverage(trading_config.get('leverage'))
        self.single_position_margin = Decimal(str(trading_config.get('single_position_margin', 50)))
        self.stop_loss_percent = Decimal(str(trading_config.get('stop_loss_percent', 0.05)))
        self.take_profit_percent = Decimal(str(trading_config.get('take_profit_percent', 0.10)))
        
        # 分批止盈配置
        batch_config = trading_config.get('batch_take_profit', {})
        self.batch_take_profit_enabled = batch_config.get('enabled', True)
        self.target1_atr_multiplier = Decimal(str(batch_config.get('target1_atr_multiplier', 1.5)))
        self.target1_close_percent = Decimal(str(batch_config.get('target1_close_percent', 0.30)))
        self.target2_atr_multiplier = Decimal(str(batch_config.get('target2_atr_multiplier', 3.5)))
        self.target2_close_percent = Decimal(str(batch_config.get('target2_close_percent', 0.40)))
        self.trailing_stop_atr_multiplier = Decimal(str(batch_config.get('trailing_stop_atr_multiplier', 1.5)))
        
        # 时间止损配置
        time_stop_config = trading_config.get('time_stop', {})
        self.time_stop_enabled = time_stop_config.get('enabled', True)
        self.max_holding_hours = time_stop_config.get('max_holding_hours', 72)

        # 时间止损前置复核配置（多因素综合评分）
        review_config = trading_config.get('time_stop_review', {})
        self.time_stop_review_enabled = review_config.get('enabled', False)
        self.review_hold_threshold = float(review_config.get('hold_threshold', 5.0))
        self.review_exempt_progress = float(review_config.get('exempt_progress', 0.7))
        self.review_bias_hold = review_config.get('bias_hold', True)
        review_weights = review_config.get('weights', {})
        self.review_weights = {
            'trend': float(review_weights.get('trend', 0.40)),
            'price_action': float(review_weights.get('price_action', 0.30)),
            'volume': float(review_weights.get('volume', 0.20)),
            'sentiment': float(review_weights.get('sentiment', 0.10)),
        }
        self.review_drop_reference = float(review_config.get('trend', {}).get('drop_reference', 0.10))
        self.review_rebound_ratio = float(review_config.get('reversal', {}).get('rebound_ratio', 0.01))
        self.review_breakout_ratio = float(review_config.get('reversal', {}).get('breakout_ratio', 0.01))
        self.review_reversal_lookback = int(review_config.get('reversal', {}).get('lookback', 5))
        self.review_volume_lookback = int(review_config.get('volume', {}).get('lookback', 5))
        self.review_volume_surge = float(review_config.get('volume', {}).get('volume_surge', 1.5))
        
        # 紧急止损配置
        self.emergency_stop_enabled = trading_config.get('emergency_stop', {}).get('enabled', True)
        self.emergency_stop_check_minutes = trading_config.get('emergency_stop', {}).get('check_minutes', 15)
        self.emergency_stop_trigger_percent = Decimal(str(trading_config.get('emergency_stop', {}).get('trigger_percent', 0.015)))
        
        # ATR止损配置
        self.atr_stop_multiplier = Decimal(str(trading_config.get('atr_stop', {}).get('multiplier', 2.5)))

        # 止盈成交检测配置（持仓数量对比，检测 TP1/TP2 条件单成交）
        pd_config = trading_config.get('position_detection', {})
        self.position_detection_enabled = pd_config.get('enabled', True)
        self.qty_tolerance_ratio = float(pd_config.get('qty_tolerance_ratio', 0.01))
        self.qty_tolerance_absolute = float(pd_config.get('qty_tolerance_absolute', 0.0001))
        self.zero_qty_threshold = float(pd_config.get('zero_qty_threshold', 0.0001))
        
        # 限价单滑点参数（限价相对于触发价的偏移量，默认 0.1%）
        self.limit_order_slippage = Decimal(str(trading_config.get("limit_order_slippage", 0.001)))

        # 市价单评分阈值：评分 ≥ 此值时直接用市价单抢单，低于此值走优化限价单
        self.market_order_score_threshold = float(trading_config.get('market_order_score_threshold', 7.0))

        # 默认精度（API获取失败时的兜底值）
        default_precision = trading_config.get('default_precision', {})
        self.default_tick_size = Decimal(str(default_precision.get('tick_size', '0.01')))
        self.default_step_size = Decimal(str(default_precision.get('step_size', '0.001')))

        # 平仓比例（全仓平仓时使用）
        close_pos_config = trading_config.get('close_position', {})
        self.close_percent = Decimal(str(close_pos_config.get('close_percent', 1.0)))

        # 最小名义价值（USDT）：低于此值的条件单会被币安拒绝（-4164），从配置读取
        self.min_notional = Decimal(str(trading_config.get('min_notional', 5)))
        # 条件单补全配置（P0-3）：先算后撤开关 / 幂等忽略错误码 / MCTPS 托管跳过清单
        replenish_config = trading_config.get('replenish', {})
        self.replenish_cancel_after_ready = bool(replenish_config.get('cancel_after_ready', True))
        raw_ignore = replenish_config.get('ignore_error_codes', []) or [
            '-4164', '-2011', '-2021', '-4136', '-4507'
        ]
        self.replenish_ignore_error_codes = [str(code).strip() for code in raw_ignore]
        raw_skip = replenish_config.get('skip_symbols', []) or []
        self.replenish_skip_symbols = {str(s).strip().upper() for s in raw_skip if str(s).strip()}
        # 保护缺口/ATR 不可用告警的同 (kind,symbol) 降频窗口（秒），从配置读取
        self.alert_throttle_seconds = float(
            replenish_config.get('alert_throttle_seconds', 3600)
        )
        # 守卫核验保护单时是否要求存在止盈单（P0-A-AC3），从配置读取
        self.replenish_require_take_profit = bool(
            replenish_config.get('require_take_profit', True)
        )
        # K 线注册态保障配置（P0-1）：ensure-active 开关 / 重试次数 / 间隔 / 采集周期
        kline_config = config.get('kline', {})
        self.kline_interval = kline_config.get('interval', '1h')
        self.ensure_active_before_use = bool(kline_config.get('ensure_active_before_use', True))
        self.ensure_active_retries = int(kline_config.get('ensure_active_retries', 2))
        self.ensure_active_retry_interval = float(kline_config.get('ensure_active_retry_interval', 2))
        # 本执行器已确保 active 的币种集合（避免重复注册；服务端注册为幂等）
        self._registered_symbols: set = set()

        # 最小开仓保证金（缩仓后低于此值则拒开，决策 D3；从配置读取，禁止硬编码）
        min_margin = trading_config.get('min_position_margin')
        self.min_position_margin = float(min_margin) if min_margin is not None else None

        # 同币种重复开仓通知降频窗口（秒，避免刷屏）
        self.duplicate_notify_window_seconds = int(
            trading_config.get('duplicate_symbol_notify_window_seconds', 3600)
        )

        # 通用告警节流表：{(kind, symbol, ...): 上次放行时间戳（monotonic）}
        # 供「同币种重复开仓」「持仓保护缺口」「ATR 不可用」等复用（禁重复降频逻辑）
        self._notify_ts: Dict[tuple, float] = {}

        # 持仓跟踪（用于移动止盈和时间止损）
        self.position_tracking: Dict[str, Dict[str, Any]] = {}

        # 持仓基线就绪标志（R4）：策略启动时先置 False，基线重建完成后置 True，
        # 未就绪期间禁止开新仓（仅允许减仓），避免用不完整基线做限额判断
        self.baseline_ready: bool = False

        # contractSize 缓存（{symbol: 合约面值}，用于统一保证金口径）
        self._contract_size_cache: Optional[Dict[str, float]] = None

        # 已补全TP2的币种集合（避免重复补全）
        self._replenished_symbols: set = set()

        # 波动率计算缓存（用于动态利润保护）
        self._volatility_cache: Dict[str, Any] = {}

        # 上次跟踪的持仓数量（用于止盈成交检测，对比交易所实际数量变化）
        self._last_tracked_qty: Dict[str, float] = {}

        logger.info(
            "交易执行器初始化完成",
            leverage=self.leverage,
            single_position_margin=float(self.single_position_margin),
            batch_take_profit_enabled=self.batch_take_profit_enabled,
            time_stop_enabled=self.time_stop_enabled
        )

    def _resolve_leverage(self, raw_leverage: Any) -> Decimal:
        """
        严格解析杠杆配置（R3：缺失或 ≤ 0 视为配置错误，禁止默认猜测）

        Args:
            raw_leverage: config.trading.leverage 原始值（可能缺失 / 非数值）

        Returns:
            Decimal: 合法杠杆；非法时返回 Decimal('0') 并把 self._leverage_valid 置 False，
                     由 execute_short 的开仓守卫拦截，杜绝「占用算 0 就放行」的新漏洞
        """
        try:
            leverage = Decimal(str(raw_leverage))
        except (InvalidOperation, TypeError, ValueError):
            leverage = Decimal('0')
        self._leverage_valid = leverage > 0
        if not self._leverage_valid:
            logger.error(
                "杠杆配置缺失或非法（必须为正数），禁止开仓",
                raw_leverage=raw_leverage,
                config_path=self.capital_mgr.config_path,
            )
            return Decimal('0')
        return leverage

    async def execute_short(
        self,
        symbol: str,
        score_result: Dict[str, Any],
        current_price: float
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        执行做空交易

        Args:
            symbol: 交易对
            score_result: 评分结果字典
            current_price: 当前价格

        Returns:
            (订单信息字典, 失败原因) 元组：
            - 成功：返回 (order, "")，order 为成交后的订单信息
            - 失败：返回 (None, "<具体失败原因>")，
              失败原因包含账户余额不足/仓位大小计算失败/总仓位超限/总持仓保证金超限/
              开空仓下单失败/市价单未成交/限价单超时未成交/执行异常等
        """
        claimed = False  # R07：是否已成功占位（异常/失败时需释放）
        try:
            logger.info(
                f"准备执行做空: {symbol}",
                score=score_result.get('total_score'),
                price=current_price
            )

            # 1. 获取账户余额
            balance = await self._get_account_balance()

            if balance <= 0:
                logger.error("账户余额不足")
                return None, "账户余额不足"

            # 2. 基线就绪校验（R4：基线重建完成前禁止开新仓，仅允许减仓）
            if not self.baseline_ready:
                logger.warning("持仓基线尚未重建完成，禁止开新仓", symbol=symbol)
                return None, "持仓基线未就绪，暂禁止开仓"

            # 杠杆配置校验（R3：缺失或 ≤ 0 属配置错误，禁止开仓并告警，不得猜测默认值）
            if not self._leverage_valid:
                logger.error("杠杆配置缺失或非法，禁止开仓", symbol=symbol)
                return None, "杠杆配置缺失或非法，禁止开仓"

            # 3. 同币种重复开仓判重（R6：交易所真实持仓 ∪ 本地记录 并集判定）
            if await self._is_symbol_occupied(symbol):
                return None, "该币种已有持仓，禁止重复开仓"

            # 3.5 R07 开仓占用互斥：锁内原子占位（外部请求须在锁外，故先占位再下单）
            claim = await self._claim_symbol(symbol)
            if not claim.get("claimed"):
                await self._notify_claim_conflict(symbol, claim.get("owner"))
                return None, "该币种已被其他策略持有，跳过开仓"
            claimed = True

            # 4. 额度核算：占用统计 → 限额读取 → 缩仓 → 门槛判定 → 限额校验（决策 D3）
            position_size, reject_reason = await self._resolve_open_margin_budget(
                symbol, current_price
            )
            if position_size is None:
                await self._release_claim_quietly(symbol, "open_failed")
                return None, reject_reason

            # 5. 获取交易对精度
            tick_size, step_size = await self._get_symbol_precision(symbol)

            # 6. 格式化数量
            quantity = self._format_quantity(position_size, step_size)

            # 7. 设置杠杆
            await self._set_leverage(symbol, self.leverage)

            # 8. 开空仓（根据评分决定下单方式）
            #    评分 ≥ market_order_score_threshold：用市价单抢单，确保成交
            #    评分 < 阈值：用实时市价+滑点偏移的限价单，提高成交率
            score = float(score_result.get('total_score', 0) or 0)
            use_market_order = score >= self.market_order_score_threshold
            logger.info(
                f"{symbol} 开仓方式判定",
                score=score,
                threshold=self.market_order_score_threshold,
                use_market_order=use_market_order,
            )
            order = await self._place_short_order(symbol, quantity, Decimal(str(current_price)), use_market_order=use_market_order)

            if not order:
                logger.error("开空仓失败")
                await self._release_claim_quietly(symbol, "open_failed")
                return None, "开空仓下单失败"

            # 9. R06-F5：统一等待订单终态（识别部分成交、撤单竞态、PM 可见延迟）
            entry_timeout = self.config.get('trading', {}).get('entry_order_timeout_seconds', 60)
            entry_order_id = order.get('orderId')
            fill = await self._wait_for_order_fill(
                symbol, entry_order_id, timeout_seconds=entry_timeout,
                client_order_id=order.get('clientOrderId'),
            )
            if fill.is_filled:
                order = fill.raw or order
            elif fill.has_fill:
                # 部分成交：按实际成交量建仓并挂保护（R06-AC1/AC2）
                actual_qty = self._format_quantity(fill.executed_qty, step_size)
                if actual_qty <= 0:
                    # D3：精度截断后低于最小下单量 → 减仓清零，失败则告警
                    await self._zero_micro_entry(symbol, fill.executed_qty, step_size)
                    await self._release_claim_quietly(symbol, "open_failed")
                    return None, "部分成交量低于最小下单量，已尝试减仓清零"
                quantity = actual_qty
                order = fill.raw or order
                logger.warning("入场部分成交，按实际成交量建仓", symbol=symbol,
                               executed_qty=str(actual_qty))
            else:
                # 无成交/结果未知：取消可能仍挂着的订单，避免后续价格到达时成交
                await self._cancel_entry_order_quietly(symbol, entry_order_id)
                await self._release_claim_quietly(symbol, "open_failed")
                fail_reason = "市价单未成交" if use_market_order else "限价单超时未成交"
                return None, fail_reason

            # 10. 保存订单到数据库
            await self._save_order(order, score_result)

            # 11. 计算ATR（用于分批止盈）
            atr = await self._calculate_atr(symbol)

            # 12. 初始化持仓跟踪（必须在创建条件单之前，确保 record_condition_order 能正常执行）
            #     经唯一工厂构建，确保字段集与重启恢复/补单路径完全一致
            self.position_tracking[symbol] = self._build_tracking_entry(
                entry_price=current_price,
                entry_quantity=quantity,
                atr=atr,
            )

            # 记录初始持仓数量（用于止盈成交检测）
            self._last_tracked_qty[symbol] = float(quantity)

            # 13. 写入 short_positions 表（B1 修复：确保重启恢复时能识别自己的持仓）
            await self._insert_short_position(
                symbol=symbol,
                quantity=quantity,
                entry_price=Decimal(str(current_price)),
            )

            # 14. 设置止损止盈（根据配置选择策略）
            if self.batch_take_profit_enabled and atr > 0:
                # 使用分批止盈策略
                await self._set_batch_take_profit(
                    symbol=symbol,
                    total_quantity=quantity,
                    entry_price=Decimal(str(current_price)),
                    atr=atr,
                    tick_size=tick_size,
                    step_size=step_size
                )
            else:
                # 使用传统固定止盈止损
                await self._set_stop_loss_take_profit(
                    symbol,
                    quantity,
                    Decimal(str(current_price)),
                    tick_size
                )

            # 15. 发送通知
            await self._send_notification(symbol, order, current_price, score_result)

            logger.info(
                f"做空执行成功: {symbol}",
                order_id=str(order.get('orderId')),
                quantity=quantity,
                atr=float(atr)
            )

            return order, ""

        except Exception as e:
            logger.error(
                f"执行做空失败: {symbol}",
                error=str(e),
                exc_info=True
            )
            if claimed:
                await self._release_claim_quietly(symbol, "open_exception")
            return None, f"执行异常: {str(e)}"

    async def _resolve_open_margin_budget(
        self,
        symbol: str,
        current_price: float,
    ) -> Tuple[Optional[Decimal], str]:
        """
        开仓额度核算：占用统计 → 限额读取 → 缩仓 → 门槛判定 → 限额校验（决策 D3）

        严格保持原 execute_short 的判定顺序与行为：
        ① 统计当前总占用保证金；② 读取生效限额并算可用额度；③ 按额度缩仓；
        ④ 缩仓后低于最小保证金门槛则拒开；⑤ 统一限额校验（唯一入口）。

        Args:
            symbol: 交易对
            current_price: 当前价格

        Returns:
            (position_size, reject_reason) 元组：
            - 通过：返回 (仓位数量, "")
            - 拒绝：返回 (None, "<具体失败原因>")
        """
        # ① 统计当前总占用保证金；② 读取生效限额并计算可用额度（统一保证金口径）
        occupied_margin, limit, limit_source, avail = await self._read_available_budget(symbol)

        # ③ 计算仓位大小（剩余额度不足一笔标准仓位时按可用额度缩仓，决策 D3）
        position_size, new_margin = await self._shrink_position_and_margin(
            symbol, current_price, avail
        )

        # ④ 缩仓后保证金门槛判定（额度耗尽时 new_margin=0，给出「可用额度不足」而非含糊的计算失败）
        reject = self._reject_if_below_min_margin(
            symbol, new_margin, occupied_margin, limit, limit_source, avail
        )
        if reject:
            return None, reject
        if position_size <= 0:
            logger.error("仓位大小计算失败", symbol=symbol, price=current_price)
            return None, "仓位大小计算失败"

        # ⑤ 统一限额校验（唯一入口，保证金口径；替代原两处口径混用检查）
        reject_reason = await self._enforce_limit_check(
            symbol, occupied_margin, new_margin, limit, limit_source
        )
        if reject_reason:
            return None, reject_reason

        return position_size, ""

    async def _read_available_budget(
        self,
        symbol: str,
    ) -> Tuple[float, Optional[float], str, Optional[float]]:
        """
        步骤①②：统计当前占用保证金 → 读取生效限额 → 计算剩余可用额度

        Args:
            symbol: 交易对（仅用于日志上下文）

        Returns:
            (占用保证金, 生效限额, 限额来源, 可用额度)；
            限额为 None 表示三级来源均不可用（fail-open），此时可用额度也为 None
        """
        occupied_margin = await self.calc_current_occupied_margin()
        limit, limit_source = await self.capital_mgr.get_effective_margin_limit()
        avail = None if limit is None else max(limit - occupied_margin, 0.0)
        logger.info(
            f"{symbol} 开仓额度核算",
            occupied_margin=round(occupied_margin, 4),
            limit=limit,
            limit_source=limit_source,
            available=avail,
        )
        return occupied_margin, limit, limit_source, avail

    async def _shrink_position_and_margin(
        self,
        symbol: str,
        current_price: float,
        avail: Optional[float],
    ) -> Tuple[Decimal, float]:
        """
        步骤③：按剩余可用额度缩仓，并计算缩仓后实际占用保证金

        缩仓后保证金按统一口径计算：数量 × 价格 × contractSize / 杠杆。

        Args:
            symbol: 交易对
            current_price: 当前价格
            avail: 剩余可用额度（USDT）；None 表示 fail-open 不缩仓

        Returns:
            (缩仓后仓位数量, 缩仓后占用保证金)
        """
        position_size = self._calculate_position_size(current_price, max_margin=avail)
        contract_sizes = await self.get_contract_size_map()
        new_margin = calc_position_margin(
            position_size, current_price, contract_sizes.get(symbol), self.leverage
        )
        return position_size, new_margin

    def _reject_if_below_min_margin(
        self,
        symbol: str,
        new_margin: float,
        occupied_margin: float,
        limit: Optional[float],
        limit_source: str,
        avail: Optional[float],
    ) -> Optional[str]:
        """
        步骤④：缩仓后保证金低于最小开仓门槛则拒开（低于 trading.min_position_margin）

        Args:
            symbol: 交易对
            new_margin: 缩仓后占用保证金
            occupied_margin: 当前总占用保证金
            limit: 生效限额（None 表示 fail-open）
            limit_source: 限额来源标签
            avail: 剩余可用额度

        Returns:
            Optional[str]: 拒绝原因；None 表示通过门槛判定
        """
        if self.min_position_margin is None or new_margin >= self.min_position_margin:
            return None
        logger.warning(
            "缩仓后保证金低于最小开仓门槛，拒绝开仓",
            strategy_id=self.capital_mgr.strategy_id,
            symbol=symbol,
            occupied_margin=round(occupied_margin, 4),
            new_margin=round(new_margin, 4),
            limit=limit,
            limit_source=limit_source,
            min_position_margin=self.min_position_margin,
            available=avail,
        )
        return f"可用额度不足(缩仓后保证金{new_margin:.2f} < 门槛{self.min_position_margin:.2f})"

    async def _enforce_limit_check(
        self,
        symbol: str,
        occupied_margin: float,
        new_margin: float,
        limit: Optional[float],
        limit_source: str,
    ) -> Optional[str]:
        """
        步骤⑤：统一限额校验（保证金口径唯一入口）

        Args:
            symbol: 交易对
            occupied_margin: 当前总占用保证金
            new_margin: 缩仓后新增保证金
            limit: 生效限额（None 表示 fail-open 放行）
            limit_source: 限额来源标签

        Returns:
            Optional[str]: 拒绝原因；None 表示校验通过
        """
        allowed, reject_reason = await self.capital_mgr.can_open_within_limit(
            occupied_margin, new_margin
        )
        if allowed:
            return None
        logger.warning(
            "开仓被限额拦截",
            strategy_id=self.capital_mgr.strategy_id,
            symbol=symbol,
            occupied_margin=round(occupied_margin, 4),
            new_margin=round(new_margin, 4),
            limit=limit,
            limit_source=limit_source,
            reason=reject_reason,
        )
        return reject_reason

    async def _get_account_balance(self) -> Decimal:
        """获取账户可用余额"""
        try:
            balance = await self.binance_api.get_account_balance()
            usdt_balance = balance.get('USDT', Decimal('0'))
            logger.info(f"账户余额: {usdt_balance} USDT")
            return usdt_balance
        except Exception as e:
            logger.error(f"获取账户余额失败: {e}")
            return Decimal('0')

    def _calculate_position_size(
        self,
        current_price: float,
        max_margin: Optional[float] = None,
    ) -> Decimal:
        """
        计算仓位大小（剩余额度不足一笔标准仓位时按额度缩仓，决策 D3）

        Args:
            current_price: 当前价格
            max_margin: 本次开仓允许占用的最大保证金（USDT）；
                        None 表示不做缩仓限制，使用配置的单笔保证金

        Returns:
            仓位大小（数量）；价格非法返回 0
        """
        # 使用配置的单笔保证金；max_margin 更小时按额度缩仓
        margin = self.single_position_margin
        if max_margin is not None:
            margin = min(margin, Decimal(str(max_margin)))

        # 考虑杠杆
        position_value = margin * self.leverage

        # 计算数量
        if current_price > 0:
            quantity = position_value / Decimal(str(current_price))
            logger.debug(
                "计算仓位大小",
                margin=float(margin),
                leverage=self.leverage,
                position_value=float(position_value),
                quantity=float(quantity),
                shrunk=max_margin is not None and margin < self.single_position_margin,
            )
            return quantity

        return Decimal('0')

    async def calc_current_occupied_margin(self, own_symbols: Optional[set] = None) -> float:
        """
        计算策略自身当前总占用保证金（R3/R7 共用，统一保证金口径）

        数据源优先级：
        1. 交易所 positionRisk（数量取真实值），仅统计策略自有币种；
        2. 交易所不可用时降级为本地 position_tracking 估算（并告警）。

        Args:
            own_symbols: 策略自有币种集合；None 时取 position_tracking 的 key 集合

        Returns:
            float: 总占用保证金（USDT）
        """
        symbols = set(own_symbols) if own_symbols is not None else set(self.position_tracking.keys())
        if not symbols:
            return 0.0

        sizes = await self.get_contract_size_map()
        exchange_positions = await self._fetch_short_position_risk()

        if exchange_positions is None:
            # 交易所不可用：降级为本地跟踪估算（保守，不丢已有持仓）
            logger.warning("交易所持仓不可用，占用保证金改用本地跟踪估算", symbols=list(symbols))
            local_records = self._build_local_position_records(symbols, sizes)
            return calc_occupied_margin(local_records, self.leverage)

        owned = [
            self._with_contract_size(pos, sizes)
            for pos in exchange_positions
            if isinstance(pos, dict) and pos.get("symbol") in symbols
        ]
        return calc_occupied_margin(owned, self.leverage)

    async def _is_symbol_occupied(self, symbol: str) -> bool:
        """
        同币种重复开仓判重（R6：交易所真实持仓 ∪ 本地记录 并集）

        命中任一来源即视为「已持仓」：
        - 交易所 positionRisk 中该币种 positionAmt < 0
        - 本地 position_tracking 含该币种
        - DB new_coin.short_positions 中存在 open 记录

        边界：交易所接口失败且本地也无记录时按保守策略拒绝（宁可漏开不可重开）。

        Args:
            symbol: 待开仓币种

        Returns:
            bool: True 表示已持仓，应拒开
        """
        sources, exchange_qty, exchange_available = await self._collect_occupancy_sources(symbol)

        if not sources:
            if exchange_available:
                return False
            # 交易所不可用 + 本地无记录：保守拒绝
            logger.warning(
                "交易所持仓不可用且本地无记录，保守拒绝该币种开仓",
                symbol=symbol,
            )
            await self._notify_duplicate_symbol(symbol, None, ["exchange_unavailable"])
            return True

        logger.warning(
            "该币种已有持仓，禁止重复开仓",
            symbol=symbol,
            exchange_quantity=exchange_qty,
            sources=sources,
        )
        await self._notify_duplicate_symbol(symbol, exchange_qty, sources)
        return True

    async def _claim_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        开仓前对该 symbol 原子占位（R07 预占互斥）

        占用参数全部取自 ownership 配置（归属名/对家名单/有效期/advisory lock 超时/开关），
        禁止硬编码；intent_id 缺省交由 db 层生成。冲突/异常由 try_claim_symbol 内部
        兜底为 claimed=False，绝不抛异常，交由调用方跳过开仓。

        Args:
            symbol: 交易对

        Returns:
            try_claim_symbol 的结果字典 {'claimed', 'owner', 'claim_id'}
        """
        return await try_claim_symbol(
            self.db,
            symbol,
            self._ownership['my_record_name'],
            competing_record_names=self._ownership['competing_record_names'],
            ttl_minutes=self._ownership['claim_ttl_minutes'],
            enabled=self._ownership['enabled'],
            lock_timeout_seconds=self._ownership['lock_timeout_seconds'],
        )

    async def _release_claim_quietly(self, symbol: str, reason: str) -> None:
        """
        静默释放本策略对该 symbol 的占用（开仓失败/异常路径调用，R07-F4）

        调用点全在失败/异常分支上，故此处绝不能再抛出异常：释放失败仅记 warning。

        Args:
            symbol: 交易对
            reason: 释放原因（如 'open_failed'/'open_exception'）
        """
        try:
            await release_claim(
                self.db, symbol, self._ownership['my_record_name'], reason=reason
            )
        except Exception as e:
            logger.warning(
                "释放开仓占用失败",
                symbol=symbol,
                reason=reason,
                error=str(e),
            )

    async def _notify_claim_conflict(self, symbol: str, owner: Optional[str]) -> None:
        """
        发送开仓占用冲突告警（该币种已被其他策略占用而跳过开仓时）

        复用本文件既有告警写法（notification.project 兜底 new_coin），异常仅记日志。

        Args:
            symbol: 交易对
            owner: 占用方策略名（不可用时为 None）
        """
        project = self.config.get('notification', {}).get('project', 'new_coin')
        owner_desc = owner if owner else "未知策略"
        message = (
            f"【新币做空策略】开仓占用冲突已跳过\n"
            f"币种: {symbol}\n"
            f"占用方: {owner_desc}\n"
            f"原因: 该币种已被其他策略持有，跳过开仓"
        )
        try:
            await self.notification.send(message=message, level="warning", project=project)
        except Exception as e:
            logger.warning("发送开仓占用冲突通知失败", symbol=symbol, error=str(e))

    async def maybe_cleanup_expired_claims(self) -> None:
        """
        按 claim_cleanup_interval_minutes 定时清理过期开仓占用（R07-F7）

        内部自节流：以单调时钟 time.monotonic() 与上次清理时间戳比较，未满一个
        清理周期则直接返回，保证每轮调用开销恒定；关闭 ownership.enabled 时不动作。
        使用单调时钟可避免系统时间回拨导致节流失效。
        """
        if not self._ownership['enabled']:
            return
        now = time.monotonic()
        # 清理周期（分钟）转秒，下限 1 分钟，防止配置为 0/负值时失控高频清理
        interval_seconds = max(self._ownership['claim_cleanup_interval_minutes'], 1) * 60
        if now - self._last_claim_cleanup_at < interval_seconds:
            return
        self._last_claim_cleanup_at = now
        try:
            cleared = await cleanup_expired_claims(self.db)
            if cleared:
                logger.info("过期开仓占用清理完成", cleared=cleared)
        except Exception as e:
            logger.warning("清理过期开仓占用失败", error=str(e))

    async def _collect_occupancy_sources(
        self,
        symbol: str,
    ) -> Tuple[List[str], Optional[float], bool]:
        """
        收集该币种的持仓命中来源（R6：交易所 ∪ 本地跟踪 ∪ DB 记录 并集）

        Args:
            symbol: 待开仓币种

        Returns:
            (命中来源列表, 交易所持仓数量, 交易所是否可用)
        """
        sources: List[str] = []
        if symbol in self.position_tracking:
            sources.append("position_tracking")

        exchange_qty: Optional[float] = None
        exchange_available = True
        positions = await self._fetch_short_position_risk()
        if positions is None:
            exchange_available = False
        else:
            exchange_qty = self._match_exchange_short_qty(symbol, positions)
            if exchange_qty is not None:
                sources.append("exchange")

        if await self._has_open_short_position(symbol):
            sources.append("short_positions")

        return sources, exchange_qty, exchange_available

    @staticmethod
    def _match_exchange_short_qty(
        symbol: str,
        positions: List[Dict[str, Any]],
    ) -> Optional[float]:
        """
        从交易所持仓记录中提取该币种的空头数量（positionAmt < 0 视为空头）

        Args:
            symbol: 待开仓币种
            positions: positionRisk 返回的持仓列表

        Returns:
            Optional[float]: 空头数量绝对值；未命中返回 None
        """
        for pos in positions:
            if not isinstance(pos, dict) or pos.get("symbol") != symbol:
                continue
            amt = float(pos.get("positionAmt", 0) or 0)
            if amt < 0:
                return abs(amt)
        return None

    async def _has_open_short_position(self, symbol: str, *, default_on_error: bool = False) -> bool:
        """
        查询 DB new_coin.short_positions 是否存在该币种的未平仓记录

        Args:
            symbol: 交易对
            default_on_error: 查询异常时的返回值（默认 False 不阻断主流程；
                P0-1 的「有持仓不注销」守卫传 True，保守视为有持仓）

        Returns:
            bool: True 表示存在 open 记录
        """
        try:
            row = await self.db.fetch_one(
                """
                SELECT 1 FROM new_coin.short_positions
                WHERE symbol = $1 AND status = 'open'
                LIMIT 1
                """,
                symbol,
            )
            return row is not None
        except Exception as e:
            logger.warning("查询 short_positions 未平仓记录失败", symbol=symbol, error=str(e))
            return default_on_error

    async def get_open_short_symbols(self) -> Optional[set]:
        """
        查询 DB new_coin.short_positions 中本策略未平仓的币种集合

        PM 账户 positionRisk 返回账户内全部空头（含其他策略持仓），无法区分归属，
        故以 short_positions 表作为「本策略自有币种」的权威来源。

        Returns:
            Optional[set]: 自有未平仓币种集合；查询异常返回 None（调用方需降级处理）
        """
        try:
            rows = await self.db.fetch_all(
                """
                SELECT DISTINCT symbol FROM new_coin.short_positions
                WHERE status = 'open'
                """
            )
            return {row['symbol'] for row in rows if row.get('symbol')}
        except Exception as e:
            logger.warning("查询 short_positions 未平仓币种集合失败", error=str(e))
            return None

    def should_notify(self, key: tuple, window_seconds: float) -> bool:
        """通用告警节流判定：同一 key 在窗口内只放行一次（P0-C-AC6）。

        用「是否已记录」判定，而非与 0.0 比较：monotonic 基准在容器/机器重启后归零，
        若以 0.0 为哨兵会把「首次告警」误判为窗口内而静默丢弃。

        Args:
            key: 告警标识（如 (kind, symbol)、(kind, symbol, context)）
            window_seconds: 降频窗口（秒）；<=0 表示不降频（每次都放行）

        Returns:
            bool: True 表示应发送；False 表示窗口内已发送过、应跳过
        """
        now = time.monotonic()
        last_ts = self._notify_ts.get(key)
        if last_ts is not None and window_seconds > 0 and now - last_ts < window_seconds:
            return False
        self._notify_ts[key] = now
        return True

    async def find_missing_protection(
        self, symbol: str, *, require_take_profit: Optional[bool] = None,
        strict: bool = False,
    ) -> Optional[List[str]]:
        """核验标的缺失哪些 OPEN 保护条件单（P0-A-AC3 / P0-D-AC7）。

        Args:
            symbol: 交易对
            require_take_profit: 是否要求止盈单；None 时读取配置
                trading.replenish.require_take_profit（默认 true）
            strict: 查询异常时的收敛策略（区分「确认缺失」与「不可判定」）：
                False（默认）保守返回「全部应存在类型」（fail-closed，用于
                「是否触发守卫」）；True 返回 None 表示「不可判定」（用于
                「挂什么」——判定不确定一律不补挂，防重复止损单）。

        Returns:
            Optional[List[str]]: 缺失的中文类型列表（如 [_MISSING_SL]）；无缺口
            返回 []；strict=True 且查询异常时返回 None（不可判定）。

        Note:
            strict=False 查询异常时保守返回「全部应存在类型」（fail-closed），
            避免因 DB 抖动误判为「已保护」而放过真实缺口；由守卫触发补挂。
        """
        if require_take_profit is None:
            require_take_profit = self.replenish_require_take_profit
        missing = [_MISSING_SL]
        if require_take_profit:
            missing.append(_MISSING_TP)
        try:
            rows = await self.db.fetch_all(
                """
                SELECT DISTINCT order_type FROM condition_orders
                WHERE strategy_name = $1 AND symbol = $2 AND status = 'OPEN'
                """,
                "new_coin",
                symbol,
            )
        except Exception as e:
            logger.warning(
                "查询条件单保护缺口失败（保守返回全部应存在类型）",
                symbol=symbol,
                strict=strict,
                error=str(e),
            )
            # strict：不可判定返回 None（调用方据此零补挂，防重复止损单）
            return None if strict else missing
        open_types = {str(r.get('order_type', '')).upper() for r in (rows or [])}
        result: List[str] = []
        if "STOP_LOSS" not in open_types:
            result.append(_MISSING_SL)
        if require_take_profit and "TAKE_PROFIT" not in open_types:
            result.append(_MISSING_TP)
        return result

    def reset_replenish_flag(self, symbol: str) -> None:
        """清除补全完成标记，允许后续失去保护后自愈（同轮内防重仍由 _should_skip 保证）"""
        self._replenished_symbols.discard(symbol)

    async def _notify_duplicate_symbol(
        self,
        symbol: str,
        exchange_qty: Optional[float],
        sources: List[str],
    ) -> None:
        """
        发送同币种重复开仓告警（按配置窗口降频，避免刷屏）

        Args:
            symbol: 交易对
            exchange_qty: 交易所持仓数量（不可用时为 None）
            sources: 命中来源列表
        """
        if not self.should_notify(("duplicate_symbol", symbol), self.duplicate_notify_window_seconds):
            return

        project = self.config.get('notification', {}).get('project', 'new_coin')
        qty_desc = f"{exchange_qty:.6f}" if exchange_qty is not None else "未知"
        message = (
            f"【新币做空策略】同币种重复开仓已拦截\n"
            f"币种: {symbol}\n"
            f"交易所持仓数量: {qty_desc}\n"
            f"命中来源: {', '.join(sources)}\n"
            f"原因: 该币种已有持仓，禁止重复开仓"
        )
        try:
            await self.notification.send(message=message, level="warning", project=project)
        except Exception as e:
            logger.warning("发送同币种重复开仓通知失败", symbol=symbol, error=str(e))

    async def get_contract_size_map(self) -> Dict[str, float]:
        """
        获取 {symbol: contractSize} 映射（带进程内缓存）

        对外公开：供策略侧上报占用（R7）与基线重建复用同一份口径数据源。

        Returns:
            Dict[str, float]: 合约面值映射；接口异常返回空字典（后续按 1 估算）
        """
        if self._contract_size_cache is not None:
            return self._contract_size_cache
        try:
            exchange_info = await self.binance_api.get_exchange_info()
            self._contract_size_cache = build_contract_size_map(exchange_info)
        except Exception as e:
            logger.warning("获取 exchangeInfo 失败，contractSize 将按 1 估算", error=str(e))
            self._contract_size_cache = {}
        return self._contract_size_cache

    async def build_occupancy_report(
        self,
        symbols: Optional[set] = None,
    ) -> Optional[Tuple[Dict[str, float], Dict[str, float]]]:
        """
        构建统一口径的持仓占用上报数据（R7：看板口径与限额口径一致）

        口径：margin = |positionAmt| × markPrice × contractSize / leverage，quantity = |positionAmt|。

        Args:
            symbols: 需要上报的币种集合；None 时取 position_tracking 的 key 集合

        Returns:
            (margin_dict, qty_dict)；
            None 表示交易所持仓不可用，调用方应保留上一次上报值（禁止用固定单笔保证金填充，避免占用被低估）
        """
        own = set(symbols) if symbols is not None else set(self.position_tracking.keys())
        if not own:
            return {}, {}

        sizes = await self.get_contract_size_map()
        positions = await self._fetch_short_position_risk()
        if positions is None:
            return None

        margin_dict: Dict[str, float] = {}
        qty_dict: Dict[str, float] = {}
        for pos in positions:
            if not isinstance(pos, dict) or pos.get("symbol") not in own:
                continue
            symbol = pos["symbol"]
            amt = abs(float(pos.get("positionAmt", 0) or 0))
            if amt <= 0:
                continue
            margin = calc_position_margin(
                amt, pos.get("markPrice"), sizes.get(symbol), self.leverage
            )
            if margin <= 0:
                # 保证金算不出（标记价缺失等）：跳过该币种，避免上报 0 导致占用被低估
                logger.warning(
                    "持仓保证金计算为 0，跳过上报以免低估占用",
                    symbol=symbol,
                    mark_price=pos.get("markPrice"),
                )
                continue
            qty_dict[symbol] = amt
            margin_dict[symbol] = margin
        return margin_dict, qty_dict

    async def _fetch_short_position_risk(self) -> Optional[List[Dict[str, Any]]]:
        """
        查询交易所持仓（positionRisk）

        Returns:
            list: 持仓列表（PM 账户零持仓返回空列表）；
                  None 表示接口失败，调用方需降级处理
        """
        try:
            return await self.binance_api.get_position()
        except Exception as e:
            logger.warning("查询交易所持仓失败", error=str(e))
            return None

    @staticmethod
    def _with_contract_size(position: Dict[str, Any], sizes: Dict[str, float]) -> Dict[str, Any]:
        """
        为持仓记录补充 contractSize 字段（供统一口径函数使用）

        Args:
            position: 交易所持仓记录
            sizes: {symbol: contractSize} 映射

        Returns:
            Dict: 补充 contractSize 后的持仓记录（不修改原对象）
        """
        item = dict(position)
        item["contractSize"] = sizes.get(position.get("symbol"))
        return item

    def _build_local_position_records(
        self,
        symbols: set,
        sizes: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """
        依据本地 position_tracking 构建统一口径所需的持仓记录

        Args:
            symbols: 需要统计的币种集合
            sizes: {symbol: contractSize} 映射

        Returns:
            List[Dict]: 持仓记录列表（positionAmt 为负表示做空）
        """
        records: List[Dict[str, Any]] = []
        for symbol in symbols:
            track = self.position_tracking.get(symbol, {}) or {}
            qty = float(track.get("entry_quantity", 0) or 0)
            price = float(track.get("entry_price", 0) or 0)
            records.append({
                "symbol": symbol,
                "positionAmt": -qty,
                "markPrice": price,
                "contractSize": sizes.get(symbol),
            })
        return records

    async def _get_symbol_precision(self, symbol: str) -> tuple:
        """
        获取交易对精度（带缓存）

        Args:
            symbol: 交易对

        Returns:
            (价格精度, 数量精度)
        """
        # 缓存命中直接返回
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]

        try:
            # 获取交易对信息
            exchange_info = await self.binance_api._request(
                "GET",
                "/fapi/v1/exchangeInfo",
                signed=False
            )

            for s in exchange_info.get('symbols', []):
                if s['symbol'] == symbol:
                    # 提取价格精度
                    tick_size = self.default_tick_size
                    for f in s.get('filters', []):
                        if f['filterType'] == 'PRICE_FILTER':
                            tick_size = Decimal(f['tickSize'])
                            break

                    # 提取数量精度
                    step_size = self.default_step_size
                    for f in s.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            step_size = Decimal(f['stepSize'])
                            break

                    logger.debug(
                        f"获取精度: {symbol}",
                        tick_size=float(tick_size),
                        step_size=float(step_size)
                    )

                    # 写入缓存
                    self._precision_cache[symbol] = (tick_size, step_size)
                    return tick_size, step_size

            logger.warning(f"未找到交易对精度: {symbol}")
            return self.default_tick_size, self.default_step_size

        except Exception as e:
            logger.error(f"获取交易对精度失败: {e}")
            # 异常时清空缓存，下次重试会重新获取
            self._precision_cache.pop(symbol, None)
            return self.default_tick_size, self.default_step_size

    def _format_quantity(
        self,
        quantity: Decimal,
        step_size: Decimal
    ) -> Decimal:
        """
        格式化数量（按 step_size 取整，确保为 step_size 的整数倍）

        Args:
            quantity: 原始数量
            step_size: 数量精度

        Returns:
            格式化后的数量
        """
        # step_size 可能带多余尾随零（如 Decimal('0.01000')），normalize 后取实际精度
        normalized = step_size.normalize()
        formatted = (quantity / normalized).quantize(Decimal('1'), rounding='ROUND_DOWN') * normalized
        logger.debug(
            "格式化数量",
            original=float(quantity),
            formatted=float(formatted),
            step_size=float(step_size)
        )

        return formatted

    async def _set_leverage(self, symbol: str, leverage: int):
        """设置杠杆"""
        try:
            await self.binance_api._request(
                "POST",
                "/fapi/v1/leverage",
                params={
                    'symbol': symbol,
                    'leverage': leverage
                },
                signed=True
            )
            logger.info(f"设置杠杆: {symbol} x{leverage}")
        except Exception as e:
            logger.error(f"设置杠杆失败: {e}")

    async def _place_short_order(
        self,
        symbol: str,
        quantity: Decimal,
        current_price: Decimal,
        use_market_order: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        开空仓（支持市价单或优化限价单）

        Args:
            symbol: 交易对
            quantity: 数量
            current_price: 参考价格（K线收盘价，用于日志）
            use_market_order: 是否用市价单（高分抢单）

        Returns:
            订单信息
        """
        try:
            if use_market_order:
                # 高分：直接用市价单，确保成交
                order = await self.binance_api.place_order(
                    symbol=symbol,
                    side='SELL',
                    quantity=quantity,
                    order_type='MARKET'
                )
                logger.info(
                    f"开空仓成功(市价单): {symbol}",
                    order_id=order.get('orderId'),
                    quantity=float(quantity),
                    order_type='MARKET'
                )
                return order

            # 低分：用实时市价 + 滑点偏移作为限价，提高成交率
            # 做空开仓是 SELL，限价略高于实时市价，等待轻微反弹即可成交
            market_price = await self.binance_api.get_ticker_price(symbol)
            limit_price = market_price * (Decimal('1') + self.limit_order_slippage)
            order = await self.binance_api.place_order(
                symbol=symbol,
                side='SELL',
                order_type='LIMIT',
                quantity=quantity,
                price=limit_price,
                timeInForce='GTC'
            )

            logger.info(
                f"开空仓成功(限价单): {symbol}",
                order_id=order.get('orderId'),
                quantity=float(quantity),
                price=float(limit_price),
                market_price=float(market_price),
                order_type='LIMIT'
            )

            return order

        except Exception as e:
            logger.error(f"开空仓失败: {e}")
            return None

    async def _save_order(
        self,
        order: Dict[str, Any],
        score_result: Dict[str, Any]
    ):
        """保存订单到数据库"""
        try:
            await self.db.execute(
                """
                INSERT INTO orders (
                    order_id, symbol, strategy, side, type, quantity,
                    price, status, score, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                str(order.get('orderId')),
                order.get('symbol'),
                'new_coin',
                'SELL',
                'LIMIT',
                order.get('origQty'),
                order.get('avgPrice', 0),
                order.get('status'),
                score_result.get('total_score'),
                datetime.now()
            )

            logger.info(f"订单已保存: {order.get('orderId')}")

        except Exception as e:
            logger.error(f"保存订单失败: {e}")

    async def _insert_short_position(
        self,
        symbol: str,
        quantity: Decimal,
        entry_price: Decimal,
        position_id: Optional[str] = None,
    ) -> None:
        """
        开仓成功后，往 new_coin.short_positions 表插入一条记录。

        这是 B1 修复的核心：确保 new_coin 策略自己的持仓有明确的数据库记录，
        重启恢复时优先从数据库恢复自己的持仓，而非扫全账户 positionRisk 把其他
        策略的仓位也当成自己的。

        Args:
            symbol: 交易对
            quantity: 开仓数量（正数）
            entry_price: 入场价格
            position_id: 可选手工指定的 position_id，默认使用 symbol + 时间戳生成
        """
        try:
            # opened_at 列为 TIMESTAMP（无时区），必须绑定 naive datetime
            # 容器时区为 UTC，datetime.now() 即为 naive UTC（与项目其他落库代码一致）
            now = datetime.now()
            pid = position_id or f"{symbol}_{int(now.timestamp())}"
            await self.db.execute(
                """
                INSERT INTO new_coin.short_positions (
                    symbol, position_id, quantity, entry_price, status, opened_at
                ) VALUES ($1, $2, $3, $4, 'open', $5)
                ON CONFLICT (position_id) DO NOTHING
                """,
                symbol,
                pid,
                str(quantity),
                str(entry_price),
                now,
            )
            logger.info(
                "short_positions 持仓记录已写入",
                symbol=symbol,
                quantity=float(quantity),
                entry_price=float(entry_price),
                position_id=pid,
            )
        except Exception as e:
            logger.warning(
                "short_positions 持仓记录写入失败（不影响交易主流程）",
                symbol=symbol,
                error=str(e),
            )

    async def _update_short_position_closed(
        self,
        symbol: str,
        close_type: str = 'closed',
    ) -> None:
        """
        平仓成功后，将 new_coin.short_positions 表中该 symbol 的记录标记为已关闭。

        Args:
            symbol: 交易对
            close_type: 关闭类型，'closed'（正常平仓）或 'liquidated'（强平）
        """
        try:
            if close_type not in ('closed', 'liquidated'):
                close_type = 'closed'
            await self.db.execute(
                """
                UPDATE new_coin.short_positions
                SET status = $1, closed_at = $2
                WHERE symbol = $3 AND status = 'open'
                """,
                close_type,
                # closed_at 列为 TIMESTAMP（无时区），必须绑定 naive datetime
                datetime.now(),
                symbol,
            )
            logger.info(
                "short_positions 持仓记录已更新为关闭",
                symbol=symbol,
                close_type=close_type,
            )
        except Exception as e:
            logger.warning(
                "short_positions 持仓记录更新失败（不影响交易主流程）",
                symbol=symbol,
                error=str(e),
            )

    async def _set_stop_loss_take_profit(
        self,
        symbol: str,
        quantity: Decimal,
        entry_price: Decimal,
        tick_size: Decimal
    ):
        """
        设置止损止盈（使用限价条件单）

        Args:
            symbol: 交易对
            quantity: 数量
            entry_price: 入场价格
            tick_size: 价格精度
        """
        try:
            # 计算止损价格 = MAX(紧急止损, 最小绝对止损)
            min_stop_price = entry_price * (Decimal('1') + self.stop_loss_percent)
            emergency_stop_price = entry_price * (Decimal('1') + self.emergency_stop_trigger_percent)
            stop_loss_price = max(min_stop_price, emergency_stop_price)
            stop_loss_price = self._format_price(stop_loss_price, tick_size)

            # 计算止盈价格（向下）
            take_profit_price = entry_price * (Decimal('1') - self.take_profit_percent)
            take_profit_price = self._format_price(take_profit_price, tick_size)

            # 计算止损限价（限价略高于止损价，确保触发后立即成交）
            # 做空方向止损是买入（BUY），限价应略高于触发价
            slippage = self.limit_order_slippage
            stop_limit_price = self._format_price(stop_loss_price * (Decimal('1') + slippage), tick_size)

            # 计算止盈限价（限价略高于止盈价，确保触发后立即成交）
            # 做空方向止盈是买入（BUY），限价应略高于触发价
            tp_limit_price = self._format_price(take_profit_price * (Decimal('1') + slippage), tick_size)

            # 设置止损单（限价条件单，reduce_only 全仓止损，传入 quantity 替代 closePosition 以兼容 PM 账户）
            sl_result = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side='BUY',
                order_type='STOP',
                stop_price=stop_loss_price,
                price=stop_limit_price,
                quantity=quantity,
                reduce_only=True
            )

            # 保存止损单 algoId
            if sl_result and 'algoId' in sl_result and symbol in self.position_tracking:
                self.position_tracking[symbol]['algo_ids']['sl'] = sl_result['algoId']
                # 记录止损条件单到数据库（用于孤儿单清理）
                await record_condition_order(
                    self.db, "new_coin", symbol,
                    algo_id=sl_result['algoId'],
                    order_type="STOP_LOSS"
                )

            logger.info(
                f"设置止损: {symbol}",
                stop_loss=float(stop_loss_price),
                stop_limit=float(stop_limit_price),
                stop_loss_percent=float(self.stop_loss_percent),
                order_type='STOP',
                algo_id=sl_result.get('algoId', 'N/A')
            )

            # 设置止盈单（限价条件单，closePosition 全仓止盈，无需 quantity）
            tp_result = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side='BUY',
                order_type='TAKE_PROFIT',
                stop_price=take_profit_price,
                price=tp_limit_price,
                closePosition=True
            )

            # 保存止盈单 algoId
            if tp_result and 'algoId' in tp_result and symbol in self.position_tracking:
                self.position_tracking[symbol]['algo_ids']['tp'] = tp_result['algoId']
                # 记录止盈条件单到数据库（用于孤儿单清理）
                await record_condition_order(
                    self.db, "new_coin", symbol,
                    algo_id=tp_result['algoId'],
                    order_type="TAKE_PROFIT"
                )

            logger.info(
                f"设置止盈: {symbol}",
                take_profit=float(take_profit_price),
                tp_limit=float(tp_limit_price),
                take_profit_percent=float(self.take_profit_percent),
                order_type='TAKE_PROFIT',
                algo_id=tp_result.get('algoId', 'N/A')
            )

        except Exception as e:
            logger.error(f"设置止损止盈失败: {e}")

    def _format_price(
        self,
        price: Decimal,
        tick_size: Decimal
    ) -> Decimal:
        """格式化价格（四舍五入到tickSize的整数倍）"""
        # tick_size 可能带多余尾随零（如 Decimal('0.01000')），normalize 后取实际精度
        normalized = tick_size.normalize()
        return (price / normalized).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * normalized

    async def _wait_for_order_fill(
        self,
        symbol: str,
        order_id: Optional[int],
        timeout_seconds: int = 60,
        check_interval: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderFillResult:
        """
        等待入场订单至终态（R06 薄封装：统一走 shared.order_fill_waiter）

        保留本方法名以兼容既有调用点，内部委托 ``wait_order_final_state``，
        结构化返回 OrderFillResult（识别部分成交/撤单竞态/PM 可见延迟）。

        Args:
            symbol: 交易对
            order_id: 交易所订单号（与 client_order_id 至少提供其一）
            timeout_seconds: 超时秒数（取策略配置 entry_order_timeout_seconds）
            check_interval: 轮询间隔（None 则取 order_fill 共享配置）
            client_order_id: 客户端订单号（order_id 缺失时按此查单）

        Returns:
            OrderFillResult（.is_filled 完全成交 / .has_fill 含部分成交）
        """
        return await wait_order_final_state(
            self.binance_api,
            symbol,
            order_id=order_id,
            client_order_id=client_order_id,
            timeout_seconds=timeout_seconds,
            check_interval=check_interval,
        )

    async def _cancel_entry_order_quietly(self, symbol: str, order_id: Optional[int]) -> None:
        """
        静默取消入场订单（-2011/-2013 视为已不存在，属正常竞态）

        Args:
            symbol: 交易对
            order_id: 交易所订单号
        """
        if order_id is None:
            return
        try:
            await self.binance_api.cancel_order(symbol, str(order_id))
        except BinanceAPIError as e:
            if e.code in (-2011, -2013):
                logger.info("入场订单已不存在（边界成交/已撤销），无需取消",
                            symbol=symbol, order_id=order_id, error_code=e.code)
            else:
                logger.warning("取消入场订单失败", symbol=symbol, error=str(e))
        except Exception as e:
            logger.warning("取消入场订单异常", symbol=symbol, error=str(e))

    async def _zero_micro_entry(
        self, symbol: str, executed_qty: Decimal, step_size: Decimal
    ) -> None:
        """
        D3：入场部分成交量经精度截断为 0（低于最小下单量）→ 减仓清零微仓

        清零失败仅告警（不静默丢弃），由人工复核。

        Args:
            symbol: 交易对
            executed_qty: 实际已成成交量（绝对值）
            step_size: 该交易对数量精度（由开仓流程已获取的精度传入）
        """
        target = abs(Decimal(str(executed_qty)))
        if target <= 0:
            return
        # 平仓口径与 _close_position 一致（trading.close_position），避免重复定义阈值
        cfg = self.config.get('trading', {}).get('close_position', {})
        outcome = await self._close_with_reduce_only(
            symbol, target, order_type="MARKET", step_size=step_size, cfg=cfg
        )
        if not outcome.success:
            logger.warning("微仓减仓清零失败，需人工复核", symbol=symbol, reason=outcome.reason)
            message = (
                f"【新币做空微仓清零告警】\n"
                f"交易对: {symbol}\n"
                f"目标数量: {target}\n"
                f"说明: {outcome.reason}\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await self._notify_close_failure(symbol, "微仓清零失败", message)
        else:
            logger.info("微仓减仓清零成功", symbol=symbol)

    async def _send_notification(
        self,
        symbol: str,
        order: Dict[str, Any],
        current_price: float,
        score_result: Dict[str, Any]
    ):
        """发送交易通知"""
        try:
            message = f"""
【新币做空交易通知】
交易对: {symbol}
方向: 做空
数量: {order.get('origQty')}
价格: {current_price}
订单ID: {order.get('orderId')}
评分: {score_result.get('total_score'):.2f}
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            await self.notification.send(
                message=message,
                level="info",
                project="new_coin"
            )

            logger.info(f"交易通知已发送: {symbol}")

        except Exception as e:
            logger.error(f"发送通知失败: {e}")
    
    async def _calculate_atr(self, symbol: str, period: Optional[int] = None) -> Decimal:
        """
        计算ATR（Average True Range，平均真实波动范围）
        
        ATR用于衡量市场波动性，是设置止盈止损的重要参考指标
        
        Args:
            symbol: 交易对
            period: ATR周期（默认从配置读取 atr_period，如未配置则为14）
            
        Returns:
            ATR值
        """
        try:
            if period is None:
                period = self.config.get('kline', {}).get('atr_period', 14)
            
            if not self.kline_service:
                logger.warning("K线服务未设置，无法计算ATR")
                return Decimal('0')
            
            # 获取K线配置
            kline_config = self.config.get('kline', {})
            interval = kline_config.get('interval', '15m')
            limit = period + 1  # 需要多一根K线来计算TR
            
            # 获取K线数据
            klines = await self.kline_service.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            if not klines or len(klines) < period + 1:
                logger.warning(f"K线数据不足，无法计算ATR: {symbol}")
                return Decimal('0')
            
            # 计算True Range (TR)
            tr_list = []
            for i in range(1, len(klines)):
                high = Decimal(str(klines[i].get('high', 0)))
                low = Decimal(str(klines[i].get('low', 0)))
                prev_close = Decimal(str(klines[i-1].get('close', 0)))
                
                # TR = max(High - Low, |High - PrevClose|, |Low - PrevClose|)
                tr1 = high - low
                tr2 = abs(high - prev_close)
                tr3 = abs(low - prev_close)
                
                tr = max(tr1, tr2, tr3)
                tr_list.append(tr)
            
            # 计算ATR（取最近period个TR的平均值）
            if len(tr_list) >= period:
                atr = sum(tr_list[-period:]) / period
                logger.debug(f"计算ATR: {symbol} = {atr}")
                return atr
            else:
                logger.warning(f"TR数据不足，无法计算ATR: {symbol}")
                return Decimal('0')

        except KLineServiceError as e:
            # C-3：区分「服务拒绝」与「数据不足」——K 线服务非 200/异常抛 KLineServiceError，
            # 属应采集却不可用，记 ERROR 并降频告警（200 空 list 走上方 warning 分支，不告警）
            logger.error(
                f"K线服务拒绝，ATR 不可用: {symbol}",
                status_code=getattr(e, "status_code", None),
                error=str(e),
            )
            await self._notify_atr_unavailable(symbol)
            return Decimal('0')
        except Exception as e:
            logger.error(f"计算ATR失败: {symbol}, 错误: {e}")
            return Decimal('0')

    async def _notify_atr_unavailable(self, symbol: str) -> None:
        """ATR 不可用（K线服务拒绝）降频告警（ERROR 级，飞书；P0-C-AC3/AC5）"""
        if not self.should_notify(("atr_unavailable", symbol), self.alert_throttle_seconds):
            return
        project = self.config.get('notification', {}).get('project', 'new_coin')
        message = (
            f"【新币做空策略】ATR 不可用（K线服务拒绝）\n"
            f"币种: {symbol}\n"
            f"影响: 相关止盈止损阈值无法计算，已跳过本轮检查\n"
            f"处理: 请检查 K 线服务与该标的采集状态"
        )
        try:
            await self.notification.send(message=message, level="error", project=project)
        except Exception as e:
            logger.warning("发送 ATR 不可用告警失败", symbol=symbol, error=str(e))

    async def _set_batch_take_profit(
        self,
        symbol: str,
        total_quantity: Decimal,
        entry_price: Decimal,
        atr: Decimal,
        tick_size: Decimal,
        step_size: Decimal
    ):
        """
        设置分批止盈止损
        
        策略：
        1. 第一目标：开仓价 - 1.5×ATR，平仓30%
        2. 第二目标：开仓价 - 3.5×ATR，平仓40%
        3. 剩余30%使用移动止盈
        4. 止损：MAX(ATR止损, 紧急止损, 最小绝对止损)（综合止损）
        
        Args:
            symbol: 交易对
            total_quantity: 总数量
            entry_price: 入场价格
            atr: ATR值
            tick_size: 价格精度
            step_size: 数量精度
        """
        try:
            logger.info(
                f"设置分批止盈止损: {symbol}",
                entry_price=float(entry_price),
                atr=float(atr)
            )
            
            # 计算限价单滑点
            slippage = self.limit_order_slippage

            # 1. 计算最终止损价 = MAX(ATR止损, 紧急止损, 最小绝对止损)
            min_stop_price = entry_price * (Decimal('1') + self.stop_loss_percent)
            emergency_stop_price = entry_price * (Decimal('1') + self.emergency_stop_trigger_percent)
            atr_stop_price = entry_price + (atr * self.atr_stop_multiplier)

            final_stop_price = max(min_stop_price, emergency_stop_price, atr_stop_price)
            stop_loss_price = self._format_price(final_stop_price, tick_size)

            # 计算止损限价（限价略高于止损价，确保触发后立即成交）
            stop_limit_price = self._format_price(stop_loss_price * (Decimal('1') + slippage), tick_size)

            sl_result = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side='BUY',
                order_type='STOP',
                stop_price=stop_loss_price,
                price=stop_limit_price,
                quantity=total_quantity,
                reduce_only=True
            )

            # 保存止损单 algoId
            if sl_result and 'algoId' in sl_result and symbol in self.position_tracking:
                self.position_tracking[symbol]['algo_ids']['sl'] = sl_result['algoId']
                # 记录止损条件单到数据库（用于孤儿单清理）
                await record_condition_order(
                    self.db, "new_coin", symbol,
                    algo_id=sl_result['algoId'],
                    order_type="STOP_LOSS"
                )

            logger.info(
                f"设置综合止损（ATR+紧急+最小绝对值取最大值）: {symbol}",
                stop_loss=float(stop_loss_price),
                stop_limit=float(stop_limit_price),
                atr_stop=float(atr_stop_price),
                emergency_stop=float(emergency_stop_price),
                min_stop=float(min_stop_price),
                final_stop=float(final_stop_price),
                order_type='STOP',
                algo_id=sl_result.get('algoId', 'N/A')
            )

            # 2. 设置第一目标止盈（开仓价 - 1.5×ATR，平仓30%）
            target1_price = entry_price - (atr * self.target1_atr_multiplier)
            target1_price = self._format_price(target1_price, tick_size)
            target1_quantity = total_quantity * self.target1_close_percent
            target1_quantity = self._format_quantity(target1_quantity, step_size)

            # 计算 TP1 限价（限价略高于止盈价，确保触发后立即成交）
            tp1_limit_price = self._format_price(target1_price * (Decimal('1') + slippage), tick_size)

            # 检查最小名义价值，避免 -4164 错误
            tp1_notional = target1_quantity * target1_price
            if target1_quantity > 0 and target1_price > 0 and tp1_notional < self.min_notional:
                logger.info(
                    f"TP1 名义价值 {float(tp1_notional)} USDT < 最小值 {float(self.min_notional)} USDT，跳过设置",
                    symbol=symbol
                )
            else:
                tp1_result = await self.binance_api.place_conditional_order(
                    symbol=symbol,
                    side='BUY',
                    order_type='TAKE_PROFIT',
                    stop_price=target1_price,
                    price=tp1_limit_price,
                    quantity=target1_quantity,
                    reduce_only=True
                )

                # 保存 TP1 止盈单 algoId
                if tp1_result and 'algoId' in tp1_result and symbol in self.position_tracking:
                    self.position_tracking[symbol]['algo_ids']['tp1'] = tp1_result['algoId']
                    # 记录 TP1 止盈条件单到数据库（用于孤儿单清理）
                    await record_condition_order(
                        self.db, "new_coin", symbol,
                        algo_id=tp1_result['algoId'],
                        order_type="TAKE_PROFIT"
                    )

                logger.info(
                    f"设置第一目标止盈: {symbol}",
                    target_price=float(target1_price),
                    tp_limit=float(tp1_limit_price),
                    quantity=float(target1_quantity),
                    close_percent=float(self.target1_close_percent),
                    order_type='TAKE_PROFIT',
                    algo_id=tp1_result.get('algoId', 'N/A')
                )

            # 3. 设置第二目标止盈（开仓价 - 3.5×ATR，平仓40%）
            target2_price = entry_price - (atr * self.target2_atr_multiplier)
            
            # 验证 TP2 价格有效性：必须大于 0，且未低于当前价
            if target2_price <= 0:
                logger.warning(
                    f"第二目标止盈价格无效（<=0），跳过设置",
                    symbol=symbol,
                    target_price=float(target2_price),
                    entry_price=float(entry_price),
                    atr=float(atr),
                    atr_multiplier=float(self.target2_atr_multiplier)
                )
            elif entry_price <= target2_price:
                # 正常情况 entry_price > target2_price（做空目标价在下方）
                # 如果 entry_price <= target2_price 说明计算异常，跳过
                logger.warning(
                    f"第二目标止盈价格异常（>=入场价），跳过设置",
                    symbol=symbol,
                    target_price=float(target2_price),
                    entry_price=float(entry_price)
                )
            else:
                target2_price = self._format_price(target2_price, tick_size)
                target2_quantity = total_quantity * self.target2_close_percent
                target2_quantity = self._format_quantity(target2_quantity, step_size)

                # 计算 TP2 限价（限价略高于止盈价，确保触发后立即成交）
                tp2_limit_price = self._format_price(target2_price * (Decimal('1') + slippage), tick_size)

                # 检查最小名义价值，避免 -4164 错误
                tp2_notional = target2_quantity * target2_price
                if target2_quantity > 0 and target2_price > 0 and tp2_notional < self.min_notional:
                    logger.info(
                        f"TP2 名义价值 {float(tp2_notional)} USDT < 最小值 {float(self.min_notional)} USDT，跳过设置",
                        symbol=symbol
                    )
                else:
                    tp2_result = await self.binance_api.place_conditional_order(
                        symbol=symbol,
                        side='BUY',
                        order_type='TAKE_PROFIT',
                        stop_price=target2_price,
                        price=tp2_limit_price,
                        quantity=target2_quantity,
                        reduce_only=True
                    )

                    # 保存 TP2 止盈单 algoId
                    if tp2_result and 'algoId' in tp2_result and symbol in self.position_tracking:
                        self.position_tracking[symbol]['algo_ids']['tp2'] = tp2_result['algoId']
                        # 记录 TP2 止盈条件单到数据库（用于孤儿单清理）
                        await record_condition_order(
                            self.db, "new_coin", symbol,
                            algo_id=tp2_result['algoId'],
                            order_type="TAKE_PROFIT"
                        )
                    
                    logger.info(
                        f"设置第二目标止盈: {symbol}",
                        target_price=float(target2_price),
                        tp_limit=float(tp2_limit_price),
                        quantity=float(target2_quantity),
                        close_percent=float(self.target2_close_percent),
                        order_type='TAKE_PROFIT',
                        algo_id=tp2_result.get('algoId', 'N/A')
                    )
            
            # 4. 剩余30%使用移动止盈（在监控中实现）
            # 记录目标价格到持仓跟踪
            if symbol in self.position_tracking:
                self.position_tracking[symbol]['target1_price'] = float(target1_price)
                self.position_tracking[symbol]['target2_price'] = float(target2_price)
                self.position_tracking[symbol]['atr'] = float(atr)
            
            logger.info(f"分批止盈止损设置完成: {symbol}")
            
        except Exception as e:
            logger.error(f"设置分批止盈止损失败: {e}")
    
    async def check_position_management(self, symbol: str) -> None:
        """
        检查持仓管理（移动止盈、时间止损、动态利润保护）

        此方法应由策略主循环定期调用

        Args:
            symbol: 交易对
        """
        try:
            # 检查是否有该币种的持仓跟踪
            if symbol not in self.position_tracking:
                return

            tracking = self.position_tracking[symbol]
            entry_time = tracking.get('entry_time')

            # 0. 检测止盈目标成交（持仓数量对比，激活移动止损）
            exchange_qty = await self._get_exchange_position_qty(symbol)
            if exchange_qty is not None:
                fill_level = self.detect_take_profit_fills(symbol, exchange_qty)
                if fill_level == 0:
                    # 全部平仓：交由 _monitor_positions 完成清理
                    return

            # 1. 检查时间止损
            if self.time_stop_enabled:
                await self._check_time_stop(symbol, entry_time)

            # 1.5 检查紧急止损
            if self.emergency_stop_enabled:
                await self._check_emergency_stop(symbol, entry_time)

            # 2. 检查动态利润保护（仅在 TP2 到达后激活）
            # 获取当前价格一次，同时用于动态利润保护和最高价更新
            if tracking.get('target2_reached'):
                ticker = await self.binance_api._request(
                    "GET", "/fapi/v1/ticker/price",
                    params={'symbol': symbol}, signed=False
                )
                current_price = Decimal(str(ticker.get('price', 0)))

                # 更新最高价（做空时追踪反弹）
                if current_price > Decimal(str(tracking.get('highest_price', 0))):
                    tracking['highest_price'] = float(current_price)

                # 先检查动态利润保护（价格从最高价回落触发）
                await self._check_dynamic_trailing(symbol, current_price)

                # 再检查移动止盈（价格从最低价反弹触发）
                await self._check_trailing_stop(symbol)

        except Exception as e:
            logger.error(f"检查持仓管理失败: {symbol}, 错误: {e}")
    
    def _normalize_entry_time(
        self, symbol: str, entry_time: Any, check_name: str
    ) -> Optional[datetime]:
        """
        规范化入场时间并做判空告警（供各止损检查方法复用）

        入场时间来源不一：持仓跟踪表存 aware datetime，而重启后的持仓重建流程可能传入
        ISO 字符串；若直接参与 datetime 相减会抛 TypeError，导致止损检查失效。此处统一
        调用 to_aware_utc 规范化，转换失败时记录告警并返回 None。

        Args:
            symbol: 交易对，用于日志排查
            entry_time: 原始入场时间（ISO 字符串 / naive datetime / aware datetime 等）
            check_name: 检查名称（如 "紧急止损"），用于拼接告警文案

        Returns:
            规范化后的 aware UTC datetime；返回 None 表示无法解析，调用方应跳过检查
        """
        normalized = to_aware_utc(entry_time)
        if normalized is None:
            logger.warning(
                f"入场时间无法解析，跳过{check_name}检查",
                symbol=symbol,
                entry_time=entry_time,
            )
        return normalized

    async def _check_emergency_stop(self, symbol: str, entry_time: datetime) -> None:
        """
        检查紧急止损
        
        逻辑：
        - 开仓后15分钟内，价格反向（上涨）超过1.5%，立即平仓
        - 15分钟后此检查失效
        
        Args:
            symbol: 交易对
            entry_time: 入场时间
        """
        try:
            # 防御性规范化：entry_time 可能来自 ISO 字符串（如重启后的持仓重建流程），
            # 统一转为 aware datetime 后再相减，避免 TypeError 导致止损检查失效
            normalized_entry_time = self._normalize_entry_time(symbol, entry_time, "紧急止损")
            if normalized_entry_time is None:
                return

            # 计算持仓时长（分钟）
            holding_minutes = (datetime.now(timezone.utc) - normalized_entry_time).total_seconds() / 60
            
            # 超过检查时间则不触发
            if holding_minutes > self.emergency_stop_check_minutes:
                return
            
            # 获取当前价格
            ticker = await self.binance_api._request(
                "GET",
                "/fapi/v1/ticker/price",
                params={'symbol': symbol},
                signed=False
            )
            current_price = float(ticker.get('price', 0))
            
            if current_price <= 0:
                return
            
            entry_price = self.position_tracking.get(symbol, {}).get('entry_price', 0)
            if entry_price <= 0:
                return
            
            # 计算价格涨幅（做空方向，价格上涨为不利方向）
            price_change = (current_price - entry_price) / entry_price
            
            if price_change >= float(self.emergency_stop_trigger_percent):
                logger.warning(
                    f"触发紧急止损: {symbol}",
                    entry_price=entry_price,
                    current_price=current_price,
                    price_change=f"{price_change:.2%}",
                    holding_minutes=f"{holding_minutes:.1f}"
                )
                
                # 立即平仓
                await self._close_position(symbol, self.close_percent, "紧急止损")
                
                # 取消该合约剩余条件单
                await self.cancel_all_algo_orders(symbol)
                
                # 清理持仓跟踪（幂等）
                self.clear_position_tracking(symbol)
                
                # 发送通知
                await self.notification.send(
                    message=f"【紧急止损触发】\n交易对: {symbol}\n入场价: {entry_price}\n当前价: {current_price}\n涨幅: {price_change:.2%}\n持仓时长: {holding_minutes:.1f}分钟",
                    level="warning",
                    project="new_coin"
                )
                
        except Exception as e:
            logger.error(f"检查紧急止损失败: {symbol}, 错误: {e}")
    
    async def _check_time_stop(self, symbol: str, entry_time: datetime) -> None:
        """
        检查时间止损

        逻辑：
        - 持仓超过 max_holding_hours（默认72小时）且未达第一目标
        - 若启用前置复核（time_stop_review.enabled）：先做多因素综合评分判断空头逻辑是否仍成立；
          成立则继续持有，不成立才平仓100%
        - 未启用复核则保持原逻辑：直接平仓100%

        Args:
            symbol: 交易对
            entry_time: 入场时间
        """
        try:
            # 防御性规范化：entry_time 可能来自 ISO 字符串（如重启后的持仓重建流程），
            # 统一转为 aware datetime 后再相减，避免 TypeError 导致止损检查失效
            normalized_entry_time = self._normalize_entry_time(symbol, entry_time, "时间止损")
            if normalized_entry_time is None:
                return

            # 计算持仓时长
            holding_hours = (datetime.now(timezone.utc) - normalized_entry_time).total_seconds() / 3600
            
            if holding_hours >= self.max_holding_hours:
                # 检查是否达到第一目标
                tracking = self.position_tracking.get(symbol, {})
                if not tracking.get('target1_reached'):
                    # 前置复核：先判断空头逻辑是否仍成立，再决定是否止损
                    should_stop, reason = True, ""
                    if self.time_stop_review_enabled:
                        should_stop, reason = await self._time_stop_review(symbol, tracking)

                    if not should_stop:
                        logger.info(
                            f"时间止损复核：空头逻辑仍成立，继续持有", 
                            symbol=symbol,
                            holding_hours=holding_hours,
                            reason=reason
                        )
                        return

                    logger.warning(
                        f"触发时间止损: {symbol}",
                        holding_hours=holding_hours,
                        max_holding_hours=self.max_holding_hours,
                        review_reason=reason
                    )
                    
                    # 平仓100%
                    await self._close_position(symbol, self.close_percent, "时间止损")
                    
                    # 清除持仓跟踪（幂等）
                    self.clear_position_tracking(symbol)
                    
                    # 发送通知
                    await self.notification.send(
                        message=f"【时间止损触发】\n交易对: {symbol}\n持仓时长: {holding_hours:.1f}小时\n已平仓100%\n复核原因: {reason}",
                        level="warning",
                        project="new_coin"
                    )
            
        except Exception as e:
            logger.error(f"检查时间止损失败: {symbol}, 错误: {e}")
    
    async def _time_stop_review(self, symbol: str, tracking: Dict[str, Any]) -> Tuple[bool, str]:
        """
        时间止损前置复核（多因素综合评分）

        判断"空头逻辑是否仍成立"：
        - 距第一目标较近（豁免规则）→ 判定继续持有
        - 综合 趋势 + 反转形态 + 量能 + 情绪 四维评分，总分 ≥ hold_threshold 继续持有，< 则止损
        - 数据异常/无法确定时按 bias_hold 偏向继续持有

        Args:
            symbol: 交易对
            tracking: 持仓跟踪数据（含 entry_price/atr 等）

        Returns:
            (should_stop, reason): 是否应立即止损平仓，及判定原因
        """
        try:
            entry_price = float(tracking.get('entry_price', 0))
            atr = float(tracking.get('atr', 0) or 0)

            if entry_price <= 0:
                return (not self.review_bias_hold), "入场价异常（按bias_hold配置决策）"

            # 获取当前价格
            ticker = await self.binance_api._request(
                "GET", "/fapi/v1/ticker/price",
                params={'symbol': symbol}, signed=False
            )
            current_price = float(ticker.get('price', 0))
            if current_price <= 0:
                return (not self.review_bias_hold), "当前价格异常（按bias_hold配置决策）"

            # 豁免规则：距第一目标较近则继续持有到目标
            if atr > 0:
                target1_price = entry_price - float(self.target1_atr_multiplier) * atr
                total_drop = entry_price - target1_price
                if total_drop > 0:
                    progress = (entry_price - current_price) / total_drop
                    if progress >= self.review_exempt_progress:
                        return False, f"距第一目标较近(达成{progress*100:.0f}%)，豁免时间止损"

            # 获取K线
            klines = await self.kline_service.get_klines(symbol, "1h", limit=30)
            if not klines:
                return (not self.review_bias_hold), "K线数据缺失（按bias_hold配置决策）"

            # 计算四维评分
            score = self._compute_review_score(entry_price, current_price, atr, klines)

            should_stop = score < self.review_hold_threshold
            if not should_stop:
                return False, f"综合评分{score:.1f}(≥{self.review_hold_threshold})，空头逻辑仍成立，继续持有"
            return True, f"综合评分{score:.1f}(<{self.review_hold_threshold})，空头逻辑被破坏，止损"

        except Exception as e:
            logger.error(f"时间止损复核失败: {symbol}, 错误: {e}")
            # 复核异常时按 bias_hold 决策，避免因数据问题误平仓
            return (not self.review_bias_hold, "复核异常（按bias_hold配置决策）")

    def _compute_review_score(
        self,
        entry_price: float,
        current_price: float,
        atr: float,
        klines: List[Dict[str, Any]]
    ) -> float:
        """
        计算多因素综合评分（0~10，越高表示空头逻辑越成立）

        四维：
        - trend：空头趋势（现价较开仓价回撤深度）
        - price_action：反转形态（近期是否出现反弹/破位，削弱空头）
        - volume：量能（反弹是否放量上攻）
        - sentiment：情绪（资金费率是否仍利于空头）

        Args:
            entry_price: 开仓价
            current_price: 当前价
            atr: ATR值
            klines: 1小时K线列表

        Returns:
            float: 综合评分
        """
        # 1. 趋势维度（0~10）
        drop_ratio = (entry_price - current_price) / entry_price if entry_price else 0
        trend_score = min(10.0, max(0.0, drop_ratio / self.review_drop_reference) * 10.0)

        # 2. 反转形态维度（0~10）
        price_action_score = self._score_price_action(klines, current_price)

        # 3. 量能维度（0~10）
        volume_score = self._score_volume(klines)

        # 4. 情绪维度（0~10）
        # 当前保持中性分（5.0），避免增加实时资金费率API依赖；如需接入可扩展 _score_sentiment
        sentiment_score = self._score_sentiment()

        w = self.review_weights
        total = (
            trend_score * w['trend']
            + price_action_score * w['price_action']
            + volume_score * w['volume']
            + sentiment_score * w['sentiment']
        )
        return total

    def _score_price_action(self, klines: List[Dict[str, Any]], current_price: float) -> float:
        """
        反转形态维度评分：越高表示空头越成立（无反转），越低表示出现反转削弱空头

        判定依据：
        - 现价较近期低点反弹达到阈值 → 反弹信号（削弱空头）
        - 现价已回到/突破近期高点 → 破位信号（削弱空头）
        - 现价突破前期高点 → 强反转信号（大幅削弱空头）
        """
        if not klines:
            return 5.0
        recent = klines[-self.review_reversal_lookback:]
        prior = klines[:-self.review_reversal_lookback] if len(klines) > self.review_reversal_lookback else []

        recent_high = max(float(k.get('high', 0)) for k in recent)
        recent_low = min(float(k.get('low', 0)) for k in recent)

        # 反弹信号：现价较近期低点反弹超过阈值
        rebound_ratio = (current_price - recent_low) / recent_low if recent_low else 0.0
        # 破位信号：现价突破前期高点
        prior_high = max(float(k.get('high', 0)) for k in prior) if prior else recent_high
        breakout_ratio = (current_price - prior_high) / prior_high if prior_high else 0.0

        penalty = 0.0
        if rebound_ratio >= self.review_rebound_ratio:
            penalty += 4.0
        if current_price >= recent_high:
            penalty += 3.0
        if breakout_ratio >= self.review_breakout_ratio:
            penalty += 3.0

        return max(0.0, 10.0 - penalty)

    def _score_volume(self, klines: List[Dict[str, Any]]) -> float:
        """
        量能维度评分：越高表示空头越成立（反弹缩量/无放量上攻）
        """
        if not klines or len(klines) < 2 * self.review_volume_lookback:
            return 5.0
        recent = klines[-self.review_volume_lookback:]
        baseline = klines[-2 * self.review_volume_lookback:-self.review_volume_lookback]

        recent_vol = sum(float(k.get('volume', 0)) for k in recent) / len(recent)
        base_vol = sum(float(k.get('volume', 0)) for k in baseline) / len(baseline)

        if base_vol <= 0:
            return 5.0

        vol_ratio = recent_vol / base_vol
        # 放量上攻 = 空头受威胁，扣分
        recent_up = sum(1 for k in recent if float(k.get('close', 0)) >= float(k.get('open', 0)))
        up_ratio = recent_up / len(recent)

        if vol_ratio >= self.review_volume_surge and up_ratio >= 0.6:
            return 2.0  # 放量上攻，削弱空头
        return 8.0  # 未放量上攻，空头仍健康

    def _score_sentiment(self) -> float:
        """
        情绪维度评分：当前保持中性分（5.0），避免增加实时资金费率API依赖。

        （如需接入资金费率，可在后续迭代中重写此方法，从 binance_api 读取当前费率）
        """
        return 5.0
    
    def _warn_invalid_atr(self, symbol: str, context: str, atr) -> bool:
        """
        ATR 无效（<=0）时记中文告警并返回 True，调用方据此跳过本轮检查

        atr 参数不加类型注解：两处调用分别传入 float（移动止盈读原始条目）与
        Decimal（动态利润保护先经 Decimal(str(...)) 归一），二者均支持与 0 比较
        及 float() 转换，避免额外引入 Union 或强转。

        Args:
            symbol: 交易对
            context: 触发场景的中文名（如"移动止盈"/"动态利润保护"），拼接进告警文案
            atr: 当前 ATR 值（float 或 Decimal）

        Returns:
            bool: True 表示 ATR 无效、调用方应跳过本轮；False 表示有效可继续
        """
        if atr <= 0:
            logger.warning(
                f"{symbol} {context}缺少有效 ATR，跳过本轮检查（避免误触发平仓）",
                atr=float(atr),
            )
            return True
        return False

    async def _check_trailing_stop(self, symbol: str) -> None:
        """
        检查移动止盈
        
        逻辑：
        - 记录持仓期间的最低价
        - 从最低价反弹1.5×ATR时平仓剩余仓位
        
        Args:
            symbol: 交易对
        """
        try:
            tracking = self.position_tracking.get(symbol, {})
            if not tracking:
                return
            
            # 获取当前价格
            ticker = await self.binance_api._request(
                "GET",
                "/fapi/v1/ticker/price",
                params={'symbol': symbol},
                signed=False
            )
            
            current_price = float(ticker.get('price', 0))
            lowest_price = tracking.get('lowest_price', current_price)
            atr = tracking.get('atr', 0)
            
            # 更新最低价
            if current_price < lowest_price:
                tracking['lowest_price'] = current_price
                logger.debug(f"更新最低价: {symbol} = {current_price}")
                return
            
            # atr 防护：重启恢复路径可能未回填 atr（视为 0）。此时阈值 = 0×倍数 = 0，
            # 而进入本分支必有 current_price >= lowest_price（见上方最低价早退块），故
            # price_bounce >= 0 恒为真 → 重启后第一次检查即误触发移动止盈平仓（资金风险）。
            # 故 atr<=0 时记中文告警并跳过本轮阈值比较与平仓；此处最低价已维护完毕，
            # 不会因跳过而漏更新最低价（方向保守）。本方法返回契约为 None，直接 return
            # 与上方最低价分支的既有返回一致，不破坏调用方 check_position_management。
            if self._warn_invalid_atr(symbol, "移动止盈", atr):
                # C-5：ATR=0 已跳过本轮，补发降频告警（既跳过又可观测）
                await self._notify_atr_unavailable(symbol)
                return

            # 计算反弹幅度
            price_bounce = current_price - lowest_price
            trailing_stop_threshold = atr * float(self.trailing_stop_atr_multiplier)
            
            # 检查是否触发移动止盈
            if price_bounce >= trailing_stop_threshold:
                logger.warning(
                    f"触发移动止盈: {symbol}",
                    lowest_price=lowest_price,
                    current_price=current_price,
                    price_bounce=price_bounce,
                    trailing_stop_threshold=trailing_stop_threshold
                )
                
                # 平仓剩余仓位
                if self._should_close_remaining(symbol, tracking):
                    await self._close_position(symbol, self.close_percent, "移动止盈")
                
                # 清除持仓跟踪（幂等）
                self.clear_position_tracking(symbol)
                
                # 发送通知
                await self.notification.send(
                    message=f"【移动止盈触发】\n交易对: {symbol}\n最低价: {lowest_price}\n当前价: {current_price}\n反弹: {price_bounce:.4f}\n已平仓剩余仓位",
                    level="info",
                    project="new_coin"
                )
            
        except Exception as e:
            logger.error(f"检查移动止盈失败: {symbol}, 错误: {e}")

    async def _cancel_trailing_stop_order(self, symbol: str) -> None:
        """
        取消移动止损条件单（平仓触发时调用）

        Args:
            symbol: 交易对
        """
        tracking = self.position_tracking.get(symbol, {})
        algo_ids = tracking.get('algo_ids', {})
        dt_config = self.config.get('trading', {}).get('dynamic_trailing', {})
        silent_error_codes = set(dt_config.get('cleanup_silent_error_codes', [-2022, -2011]))

        old_id = algo_ids.get('trailing_stop')
        if old_id is not None:
            try:
                await self.binance_api.cancel_algo_order(symbol, old_id)
            except BinanceAPIError as e:
                if e.code not in silent_error_codes:
                    logger.warning(
                        f"{symbol} 取消移动止损条件单失败",
                        algo_id=old_id, error_code=e.code
                    )
            except Exception as e:
                logger.warning(
                    f"{symbol} 取消移动止损条件单异常",
                    algo_id=old_id, error=str(e)
                )
            algo_ids['trailing_stop'] = None

    async def _sync_trailing_stop_order(
        self,
        symbol: str,
        trailing_stop: Decimal
    ) -> None:
        """
        将动态止损价同步到交易所条件单

        取消旧条件单，创建新条件单，让交易所自动触发止损。
        首次激活时同时取消原有硬止损单（algo_ids['sl']）。

        Args:
            symbol: 交易对
            trailing_stop: 计算出的动态止损价
        """
        tracking = self.position_tracking.get(symbol, {})
        algo_ids = tracking.get('algo_ids', {})
        trading_config = self.config.get('trading', {})
        dt_config = trading_config.get('dynamic_trailing', {})

        stop_side = 'BUY'  # 做空止损方向为买入
        stop_offset_pct = Decimal(str(dt_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        silent_error_codes = set(dt_config.get('cleanup_silent_error_codes', [-2022, -2011]))

        # 1. 取消旧移动止损条件单
        old_trailing_id = algo_ids.get('trailing_stop')
        if old_trailing_id is not None:
            try:
                await self.binance_api.cancel_algo_order(symbol, old_trailing_id)
                logger.info(
                    f"{symbol} 旧移动止损条件单已取消",
                    algo_id=old_trailing_id
                )
            except BinanceAPIError as e:
                if e.code in silent_error_codes:
                    logger.debug(
                        f"{symbol} 旧移动止损条件单取消失败（可能已成交）",
                        algo_id=old_trailing_id, error_code=e.code
                    )
                else:
                    logger.warning(
                        f"{symbol} 取消旧移动止损条件单异常",
                        algo_id=old_trailing_id, error_code=e.code
                    )
            except Exception as e:
                logger.warning(
                    f"{symbol} 取消旧移动止损条件单异常",
                    algo_id=old_trailing_id, error=str(e)
                )
            algo_ids['trailing_stop'] = None

        # 2. 首次激活时，取消原有硬止损单（已被动态止损替代）
        old_sl_id = algo_ids.get('sl')
        if old_sl_id is not None:
            try:
                await self.binance_api.cancel_algo_order(symbol, old_sl_id)
                logger.info(
                    f"{symbol} 硬止损单已取消（由动态止损替代）",
                    algo_id=old_sl_id
                )
            except BinanceAPIError as e:
                if e.code in silent_error_codes:
                    logger.debug(
                        f"{symbol} 硬止损单取消失败（可能已成交）",
                        algo_id=old_sl_id, error_code=e.code
                    )
                else:
                    logger.warning(
                        f"{symbol} 取消硬止损单异常",
                        algo_id=old_sl_id, error_code=e.code
                    )
            except Exception as e:
                logger.warning(
                    f"{symbol} 取消硬止损单异常",
                    algo_id=old_sl_id, error=str(e)
                )
            algo_ids['sl'] = None

        # 3. 计算止损限价（做空：限价 = 止损价 * (1 + offset_pct)，向不利方向偏移）
        stop_limit_price = trailing_stop * (Decimal('1') + stop_offset_pct)

        # 4. 精度调整（new_coin 返回 tuple）
        try:
            tick_size, step_size = await self._get_symbol_precision(symbol)
        except Exception:
            tick_size = self.default_tick_size
            step_size = self.default_step_size

        stop_limit_price = self._format_price(stop_limit_price, tick_size)
        close_qty = Decimal(str(tracking.get('remaining_quantity', 0)))
        close_quantity = self._format_quantity(close_qty, step_size)

        # 5. 下新止损条件单
        logger.info(
            f"{symbol} 下移动止损条件单",
            stop_side=stop_side,
            stop_price=float(trailing_stop),
            limit_price=float(stop_limit_price),
            quantity=float(close_quantity)
        )

        try:
            new_order = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side=stop_side,
                stop_price=trailing_stop,
                price=stop_limit_price,
                quantity=close_quantity,
                order_type="STOP",
                reduce_only=True
            )

            new_order_id = new_order.get('algoId') or new_order.get('orderId')
            algo_ids['trailing_stop'] = new_order_id

            logger.info(
                f"{symbol} 移动止损条件单已创建",
                order_id=new_order_id,
                trailing_stop=float(trailing_stop)
            )

            # 记录条件单到数据库（用于孤儿单清理追踪）
            if new_order_id and self.db and new_order.get('algoId'):
                await record_condition_order(
                    self.db, "new_coin", symbol,
                    algo_id=new_order['algoId'],
                    order_type="STOP_LOSS"
                )
        except Exception as e:
            logger.error(
                f"{symbol} 创建移动止损条件单失败",
                error=str(e),
                exc_info=True
            )

    async def _check_dynamic_trailing(
        self,
        symbol: str,
        current_price: Decimal
    ) -> None:
        """
        检查并执行动态利润保护

        调用 shared 层计算函数，判断是否触发平仓或需要更新交易所条件单。

        Args:
            symbol: 交易对
            current_price: 当前价格（Decimal）
        """
        try:
            tracking = self.position_tracking.get(symbol)
            if not tracking:
                return

            # 读取配置
            trading_config = self.config.get('trading', {})
            dt_config = trading_config.get('dynamic_trailing', {})
            if not dt_config.get('enabled', True):
                return

            # 读取动态利润保护所需字段
            entry_price = Decimal(str(tracking.get('entry_price', 0)))
            atr = Decimal(str(tracking.get('atr', 0)))
            highest_price = tracking.get('highest_price')
            lowest_price = tracking.get('lowest_price')

            # atr 防护：重启恢复路径可能未回填 atr（视为 0）。此时硬止损价会退化为
            # entry_price（entry + 0×倍数），"最终止损价"被错误压低到入场价附近，
            # 价格一回到入场价即判定 triggered → 重启后立刻误平仓（资金风险）。
            # 故 atr<=0 时记中文告警并跳过本轮。本方法返回契约为 None，调用方
            # check_position_management 以裸 await 调用并忽略返回值，故直接 return
            # 与"未激活"分支的既有返回一致，不会破坏调用方逻辑。
            if self._warn_invalid_atr(symbol, "动态利润保护", atr):
                # C-5：ATR=0 已跳过本轮，补发降频告警（既跳过又可观测）
                await self._notify_atr_unavailable(symbol)
                return

            # 获取波动率调节因子（如果配置启用）
            vol_adj = 1.0
            vol_config = dt_config.get('volatility_adjustment', {})
            if vol_config.get('enabled', True) and self.kline_service:
                vol_adj = await get_volatility_adjustment(
                    symbol=symbol,
                    entry_price=entry_price,
                    atr=atr,
                    kline_service=self.kline_service,
                    config=vol_config,
                    cache=self._volatility_cache,
                )

            # 获取硬止损 ATR 倍数
            atr_stop_mult = Decimal(str(trading_config.get('atr_stop', {}).get('multiplier', 2.5)))

            # 调用 shared 层纯计算函数
            result = calculate_dynamic_trailing_stop(
                direction='SHORT',
                entry_price=entry_price,
                current_price=current_price,
                highest_price=Decimal(str(highest_price)) if highest_price else None,
                lowest_price=Decimal(str(lowest_price)) if lowest_price else None,
                trailing_activated=tracking.get('trailing_activated', False),
                tp1_hit=tracking.get('target1_reached', False),
                tp2_hit=tracking.get('target2_reached', False),
                pending_profit_pct=tracking.get('pending_profit_pct'),
                current_tier_index=tracking.get('current_tier_index', -1),
                current_trailing_stop_price=Decimal(str(tracking['trailing_stop_price'])) if tracking.get('trailing_stop_price') is not None else None,
                config=dt_config,
                atr=atr,
                stop_loss_atr_multiplier=atr_stop_mult,
                volatility_adj=vol_adj,
            )

            if result is None:
                # 未激活，更新状态后返回
                tracking['trailing_activated'] = False
                return

            # 更新 position_tracking 状态
            old_trailing_stop = tracking.get('trailing_stop_price')
            tracking['trailing_activated'] = result.trailing_activated
            tracking['pending_profit_pct'] = result.pending_profit_pct
            tracking['current_tier_index'] = result.current_tier_index
            tracking['trailing_stop_price'] = float(result.trailing_stop_price)

            # 更新最高价（做空时追踪反弹价格）
            current_price_float = float(current_price)
            if current_price_float > tracking.get('highest_price', 0):
                tracking['highest_price'] = current_price_float

            # 情况1：触发平仓
            if result.triggered:
                # 平仓前取消交易所上的移动止损条件单
                await self._cancel_trailing_stop_order(symbol)

                logger.info(
                    f"{symbol} 触发动态利润保护止损",
                    current_price=float(current_price),
                    trailing_stop=float(result.trailing_stop_price),
                    pending_profit_pct=result.pending_profit_pct,
                    close_quantity=tracking.get('remaining_quantity', 0)
                )

                await self._close_position(
                    symbol=symbol,
                    close_percent=self.close_percent,
                    reason="动态利润保护"
                )
                return

            # 情况2：止损价未改善，无需更新交易所条件单
            new_trailing_stop = float(result.trailing_stop_price)
            if old_trailing_stop is not None and new_trailing_stop == old_trailing_stop:
                return

            # 情况3：止损价改善 → 同步到交易所条件单
            await self._sync_trailing_stop_order(symbol, result.trailing_stop_price)

        except Exception as e:
            logger.error(f"{symbol} 检查动态利润保护失败", error=str(e), exc_info=True)

    async def _close_position(
        self,
        symbol: str,
        close_percent: Decimal,
        reason: str
    ) -> bool:
        """
        平仓（R03：委托统一减仓助手，先限价、未成功再市价兜底）

        分两阶段：阶段一以订单簿买一价（回退最新价）限价平仓；阶段一未达标时
        进入阶段二按市价兜底。两阶段均通过 shared.close_remaining 提交，强制带
        减仓约束（reduceOnly）并按剩余待平量重算，防止反向开仓。

        Args:
            symbol: 交易对
            close_percent: 平仓比例（0-1）
            reason: 平仓原因

        Returns:
            是否成功（True 仅当剩余待平量=0 且持仓对账通过，R03-F5）
        """
        try:
            # 1. 读取交易所真实空头持仓，计算目标平仓量（以真实持仓为上限，防反向）
            target_qty, step_size, tick_size = await self._read_short_target(symbol, close_percent)
            if target_qty <= 0:
                # 已无空头持仓：幂等视为已平仓
                logger.info("已无空头持仓，视为已平仓", symbol=symbol, reason=reason)
                await self._update_short_position_closed(symbol=symbol)
                return True

            cfg = self.config.get('trading', {}).get('close_position', {})
            # 2. 阶段一：限价平仓（订单簿买一价优先，取不到回退最新价）
            limit_price = await self._resolve_close_limit_price(symbol, tick_size)
            outcome: Optional[CloseOutcome] = None
            if limit_price > 0:
                outcome = await self._close_with_reduce_only(
                    symbol, target_qty, order_type="LIMIT",
                    step_size=step_size, cfg=cfg, price=limit_price,
                )

            # 3. 阶段二：限价未成功 → 市价兜底（同样带 reduceOnly）
            if outcome is None or not outcome.success:
                outcome = await self._close_with_reduce_only(
                    symbol, target_qty, order_type="MARKET",
                    step_size=step_size, cfg=cfg,
                )

            # 4. 结果判定：任一阶段达标即视为平仓成功
            if outcome.success:
                logger.info(
                    "平仓成功", symbol=symbol, reason=reason,
                    status=outcome.status, closed_qty=str(outcome.closed_qty),
                )
                await self._update_short_position_closed(symbol=symbol)
                return True

            await self._alert_close_failure(symbol, reason, outcome)
            return False
        except Exception as e:
            logger.error("平仓失败", symbol=symbol, reason=reason, error=str(e), exc_info=True)
            await self._notify_close_failure(symbol, reason, f"平仓异常：{e}")
            return False

    async def _read_short_target(
        self,
        symbol: str,
        close_percent: Decimal,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """读取交易所真实空头持仓，返回（目标平仓量, 数量精度, 价格精度）"""
        positions = await self.binance_api.get_position(symbol)
        position_amt = Decimal("0")
        for pos in positions or []:
            amt = Decimal(str(pos.get('positionAmt', 0)))
            if amt < 0:
                position_amt = amt
                break
        tick_size, step_size = await self._get_symbol_precision(symbol)
        target_qty = abs(position_amt) * Decimal(str(close_percent))
        return target_qty, step_size, tick_size

    async def _resolve_close_limit_price(self, symbol: str, tick_size: Decimal) -> Decimal:
        """确定限价平仓价格：订单簿买一价优先，取不到回退最新价，最后按 tick_size 取整"""
        price = Decimal("0")
        try:
            orderbook = await self.binance_api.get_orderbook(symbol, limit=5)
            bids = orderbook.get('bids') or []
            if bids:
                price = Decimal(str(bids[0][0]))
        except Exception as e:
            logger.warning("获取订单簿失败，回退最新价", symbol=symbol, error=str(e))
        if price <= 0:
            try:
                ticker = await self.binance_api.get_ticker(symbol)
                price = Decimal(str(ticker.get('lastPrice', 0)))
            except Exception as e:
                logger.warning("获取最新价失败", symbol=symbol, error=str(e))
                return Decimal("0")
        if price <= 0:
            return Decimal("0")
        return self._format_price(price, tick_size)

    async def _close_with_reduce_only(
        self,
        symbol: str,
        target_qty: Decimal,
        *,
        order_type: str,
        step_size: Decimal,
        cfg: Dict[str, Any],
        price: Optional[Decimal] = None,
    ) -> CloseOutcome:
        """单阶段减仓：展开配置后委托 shared.close_remaining（限价/市价共用）"""
        return await close_remaining(
            self.binance_api,
            symbol,
            target_qty,
            side="BUY",  # 新币做空策略固定平空
            order_type=order_type,
            price=price,
            reduce_only=bool(cfg.get('reduce_only', True)),
            sync_before_reduce_only=bool(cfg.get('sync_before_reduce_only', True)),
            step_size=str(step_size),
            max_retries=int(cfg.get('max_retries', 3)),
            retry_interval=float(cfg.get('retry_interval', 2)),
            poll_interval=float(cfg.get('poll_interval', 2)),
            timeout_seconds=float(cfg.get('timeout', 10)),
            position_confirm_retries=int(cfg.get('position_confirm_retries', 2)),
            position_confirm_interval=float(cfg.get('position_confirm_interval', 1)),
        )

    async def _alert_close_failure(
        self, symbol: str, reason: str, outcome: CloseOutcome
    ) -> None:
        """平仓未达标时发送飞书告警（含状态与原因说明）"""
        status_text = {
            CLOSE_FILLED: "已成交",
            CLOSE_PARTIAL: "部分成交",
            CLOSE_ACCEPTED: "已受理未成交",
            CLOSE_FAILED: "未成交",
        }.get(outcome.status, outcome.status)
        message = f"""
【新币做空平仓告警】
交易对: {symbol}
平仓原因: {reason}
结果状态: {status_text}（{outcome.status}）
已平数量: {outcome.closed_qty} / 目标: {outcome.target_qty}
说明: {outcome.reason}
时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        await self._notify_close_failure(symbol, reason, message)

    async def _notify_close_failure(self, symbol: str, reason: str, message: str) -> None:
        """发送平仓告警通知（通知异常不影响主流程）"""
        project = self.config.get('notification', {}).get('project', 'new_coin')
        try:
            await self.notification.send(message=message, level="warning", project=project)
        except Exception as e:
            logger.error("平仓告警发送失败", symbol=symbol, reason=reason, error=str(e))

    
    async def cancel_all_algo_orders(self, symbol: str) -> Dict[str, Any]:
        """
        取消指定合约上所有未触发的条件单（止盈止损单）

        在完全平仓后调用，清理孤儿条件单，防止后续价格波动触发非预期交易。
        使用本地存储的 algoId 直接取消，不再依赖已废弃的查询 API。

        Args:
            symbol: 交易对名称

        Returns:
            取消结果统计字典：
            - total: 查询到的条件单总数
            - cancelled: 本次成功取消数量
            - failed: 本次取消失败数量
            - algo_ids: 已取消的条件单 ID 列表
        """
        result = {
            'total': 0,
            'cancelled': 0,
            'failed': 0,
            'algo_ids': [],
        }

        # 1. 从 position_tracking 获取本地存储的 algoId
        tracking = self.position_tracking.get(symbol, {})
        algo_ids = tracking.get('algo_ids', {})

        # 2. 如果本地无 algoId，尝试从 condition_orders 表回退查询
        #    解决容器重启后 position_tracking 丢失导致无法清理条件单的问题
        if not algo_ids:
            logger.info(
                "无本地存储的条件单 algoId，尝试从数据库回退查询",
                symbol=symbol,
            )
            try:
                db_orders = await self.db.fetch_all(
                    "SELECT algo_id, order_type FROM condition_orders WHERE strategy_name='new_coin' AND status='OPEN' AND symbol=$1",
                    symbol
                )
                if db_orders:
                    for i, order in enumerate(db_orders):
                        algo_id = order.get('algo_id')
                        if algo_id is not None:
                            algo_ids[f'db_{i}'] = algo_id
                    logger.info(
                        "从数据库回退查询到条件单",
                        symbol=symbol,
                        count=len(db_orders),
                    )
                else:
                    logger.info(
                        "数据库中无 OPEN 条件单",
                        symbol=symbol,
                    )
            except Exception as e:
                logger.warning(
                    "从数据库查询条件单失败",
                    symbol=symbol,
                    error=str(e),
                )

        if not algo_ids:
            logger.info(
                "无待取消的条件单",
                symbol=symbol,
            )
            return result

        result['total'] = len(algo_ids)

        logger.info(
            "开始取消孤儿条件单",
            symbol=symbol,
            total=len(algo_ids),
            source='position_tracking' if tracking.get('algo_ids') else 'condition_orders_db',
        )

        # 2. 遍历取消每个条件单
        for role, algo_id in algo_ids.items():
            if algo_id is None:
                result['failed'] += 1
                logger.warning("条件单algoId为空，跳过", symbol=symbol, role=role)
                continue

            try:
                await self.binance_api.cancel_algo_order(symbol, algo_id)
                result['cancelled'] += 1
                result['algo_ids'].append(algo_id)
                logger.info(
                    "取消条件单成功",
                    symbol=symbol,
                    algo_id=algo_id,
                    role=role,
                )
            except BinanceAPIError as e:
                # -2011 错误码（Order was not found）视为成功（幂等）
                if e.code == self._ORDER_NOT_FOUND_CODE:
                    result['cancelled'] += 1
                    result['algo_ids'].append(algo_id)
                    logger.info(
                        "条件单已不存在（可能已触发）",
                        symbol=symbol,
                        algo_id=algo_id,
                        role=role,
                    )
                else:
                    result['failed'] += 1
                    logger.warning(
                        "取消条件单失败",
                        symbol=symbol,
                        algo_id=algo_id,
                        role=role,
                        error_code=e.code,
                        error=str(e.message),
                    )
            except Exception as e:
                result['failed'] += 1
                logger.warning(
                    "取消条件单失败",
                    symbol=symbol,
                    algo_id=algo_id,
                    role=role,
                    error=str(e),
                )

        # 3. 清理已取消的 algo_ids
        if symbol in self.position_tracking and 'algo_ids' in self.position_tracking[symbol]:
            self.position_tracking[symbol]['algo_ids'] = {}

        # 4. 记录汇总日志
        logger.info(
            "孤儿条件单清理完成",
            symbol=symbol,
            total=result['total'],
            cancelled=result['cancelled'],
            failed=result['failed'],
        )

        return result
    
    async def _get_exchange_position_qty(self, symbol: str) -> Optional[float]:
        """
        获取交易所实际做空持仓数量

        币安 positionRisk 接口对零持仓返回空列表，返回 0.0 表示无做空持仓。

        Args:
            symbol: 交易对

        Returns:
            做空持仓数量（绝对值）；查询失败返回 None
        """
        try:
            positions = await self.binance_api._request(
                "GET", "/papi/v1/um/positionRisk",
                params={'symbol': symbol}, signed=True
            )
            for pos in positions:
                if float(pos.get('positionAmt', 0)) < 0:
                    return abs(float(pos['positionAmt']))
            return 0.0
        except Exception as e:
            logger.warning(f"获取持仓数量失败: {symbol}", error=str(e))
            return None

    def detect_take_profit_fills(self, symbol: str, exchange_qty: float) -> Optional[int]:
        """
        通过对比持仓数量变化检测止盈单成交

        对比交易所实际持仓数量与上次跟踪数量：
        - 100% -> 70%：target1 成交，标记 target1_reached
        - 70% -> 30%：target2 成交，标记 target2_reached 并激活移动止损
        - 0%：全部平仓

        Args:
            symbol: 交易对
            exchange_qty: 交易所当前做空持仓数量（绝对值）

        Returns:
            达成的目标级别（1/2），0 表示全部平仓，None 表示无变化或未启用
        """
        if not self.position_detection_enabled:
            return None
        last_qty = self._last_tracked_qty.get(symbol)
        if last_qty is None:
            # 首次跟踪，记录当前数量
            self._last_tracked_qty[symbol] = exchange_qty
            return None
        if exchange_qty < self.zero_qty_threshold:
            # 全部平仓
            self._last_tracked_qty[symbol] = exchange_qty
            return 0
        # 允许微小误差（精度截断/手续费）
        qty_tolerance = max(last_qty * self.qty_tolerance_ratio, self.qty_tolerance_absolute)
        if exchange_qty >= last_qty - qty_tolerance:
            return None
        # 持仓减少，检测止盈目标
        self._last_tracked_qty[symbol] = exchange_qty
        tracking = self.position_tracking.get(symbol)
        if not tracking:
            return None
        if not tracking.get('target1_reached'):
            self.update_target_status(symbol, 1, exchange_qty=exchange_qty)
            logger.info("检测到第一目标止盈成交", symbol=symbol, last_qty=last_qty, current_qty=exchange_qty)
            return 1
        if not tracking.get('target2_reached'):
            self.update_target_status(symbol, 2, exchange_qty=exchange_qty)
            logger.info("检测到第二目标止盈成交，激活移动止损", symbol=symbol, last_qty=last_qty, current_qty=exchange_qty)
            return 2
        return None

    def clear_position_tracking(self, symbol: str) -> None:
        """清理持仓跟踪与上次跟踪数量（幂等）"""
        self.position_tracking.pop(symbol, None)
        self._last_tracked_qty.pop(symbol, None)

    def _should_close_remaining(self, symbol: str, tracking: Dict[str, Any]) -> bool:
        """
        判断移动止盈是否应平掉剩余仓位（读侧兜底）

        正常条目直接依据 remaining_quantity；残缺条目（重启恢复）缺该字段时退化到
        最近跟踪数量，二者均不可用则返回 True，交由 _close_position 依据交易所实际
        持仓决策——避免残缺条目被当作 0 而静默漏平尾仓。

        Args:
            symbol: 交易对
            tracking: 该 symbol 的跟踪条目

        Returns:
            bool: True 表示应尝试平仓；False 表示剩余量为 0（确已平完）无需平仓
        """
        remaining_raw = tracking.get('remaining_quantity')
        if remaining_raw is None:
            fallback_qty = self._last_tracked_qty.get(symbol)
            logger.warning(
                f"{symbol} 持仓跟踪缺少剩余数量，退化使用最近跟踪数量以避免漏平尾仓",
                fallback_quantity=fallback_qty,
            )
            return fallback_qty is None or fallback_qty > 0
        return float(remaining_raw) > 0

    def _build_tracking_entry(
        self,
        *,
        entry_price: float,
        entry_quantity: float,
        atr: float,
        entry_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        构建字段完整的持仓跟踪条目（唯一创建入口）

        所有创建 position_tracking 条目的路径都必须经此方法，确保字段集完全一致，
        避免重启恢复等路径产生残缺条目（如只建 algo_ids），导致读侧 KeyError 或静默漏平。

        Args:
            entry_price: 入场价格
            entry_quantity: 入场数量
            atr: 入场时的 ATR
            entry_time: 入场时间（aware datetime）；缺省时使用当前 UTC 时间

        Returns:
            Dict[str, Any]: 字段完整的跟踪条目（每次返回独立对象，algo_ids 为独立空字典）
        """
        price = float(entry_price)
        quantity = float(entry_quantity)
        return {
            'entry_price': price,
            'entry_time': entry_time or datetime.now(timezone.utc),
            'entry_quantity': quantity,
            'atr': float(atr),
            'lowest_price': price,  # 持仓期间最低价（做空反弹触发移动止盈）
            'highest_price': price,  # 做空时追踪最高价（反弹触发止损用）
            'target1_reached': False,
            'target2_reached': False,
            'remaining_quantity': quantity,
            'algo_ids': {},  # 存储条件单 algoId，key='sl'/'tp1'/'tp2'/'trailing_stop'
            'direction': 'SHORT',
            'trailing_activated': False,
            'trailing_stop_price': None,
            'pending_profit_pct': None,
            'current_tier_index': -1,
        }

    def ensure_tracking_entry(
        self,
        symbol: str,
        *,
        entry_price: Optional[float] = None,
        entry_quantity: Optional[float] = None,
        atr: Optional[float] = None,
        entry_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        幂等获取持仓跟踪条目：不存在则创建全字段条目，已存在则只补缺失键

        已存在的条目仅通过 setdefault 补齐缺失字段，绝不覆盖已有值，以保留
        重启后已恢复的 algo_ids、已记录的最高/最低价、已置位的目标标志等状态。

        Args:
            symbol: 交易对
            entry_price: 入场价格（创建/补齐时使用；缺省按 0.0）
            entry_quantity: 入场数量（创建/补齐时使用；缺省按 0.0）
            atr: 入场 ATR（创建/补齐时使用；缺省按 0.0）
            entry_time: 入场时间（创建/补齐时使用；缺省使用当前 UTC 时间）

        Returns:
            Dict[str, Any]: 该 symbol 的跟踪条目（同一引用，可原地修改）
        """
        defaults = self._build_tracking_entry(
            entry_price=float(entry_price or 0.0),
            entry_quantity=float(entry_quantity or 0.0),
            atr=float(atr or 0.0),
            entry_time=entry_time,
        )
        entry = self.position_tracking.get(symbol)
        if entry is None:
            self.position_tracking[symbol] = defaults
            return defaults
        # 已存在：只补缺失键，绝不覆盖已有值（保住已恢复的 algo_ids / 极值等）
        for key, value in defaults.items():
            entry.setdefault(key, value)
        return entry

    def _mark_partial_close(self, symbol: str, closed_quantity: float, target_level: int) -> None:
        """
        市价部分平仓成功后回写跟踪状态（读侧兜底）

        根因修复后条目字段必完整；此处仍对重启恢复的残缺条目兜底，
        避免写回 remaining_quantity 时抛 KeyError，造成"假失败"日志与状态丢失。

        Args:
            symbol: 交易对
            closed_quantity: 本次实际平仓数量
            target_level: 达成的目标级别（1 或 2）
        """
        tracking_entry = self.position_tracking.get(symbol)
        if tracking_entry is None:
            logger.warning(
                f"{symbol} 无持仓跟踪条目，跳过部分平仓状态回写",
                target_level=target_level,
            )
            return

        previous_remaining = tracking_entry.get('remaining_quantity')
        if previous_remaining is None:
            # 残缺条目兜底：退化使用入场数量作为剩余量基准
            previous_remaining = tracking_entry.get('entry_quantity', 0.0)
            logger.warning(
                f"{symbol} 持仓跟踪缺少剩余数量，退化使用入场数量",
                entry_quantity=previous_remaining,
            )
        tracking_entry['remaining_quantity'] = max(
            0.0, float(previous_remaining) - float(closed_quantity)
        )
        if target_level == 1:
            tracking_entry['target1_reached'] = True
        else:
            tracking_entry['target2_reached'] = True
        # 同步上次跟踪数量（用于止盈成交检测）
        self._last_tracked_qty[symbol] = tracking_entry['remaining_quantity']

    def update_target_status(self, symbol: str, target_level: int, exchange_qty: Optional[float] = None) -> None:
        """
        更新目标达成状态

        当止盈单成交后，由持仓数量检测（detect_take_profit_fills）调用此方法更新状态。
        remaining_quantity 优先使用交易所实际剩余数量（exchange_qty），缺失时按比例估算。

        Args:
            symbol: 交易对
            target_level: 目标级别（1或2）
            exchange_qty: 交易所实际剩余持仓数量（可选，优先使用）
        """
        if symbol not in self.position_tracking:
            return

        tracking = self.position_tracking[symbol]

        close_percent: Optional[float]
        if target_level == 1:
            tracking['target1_reached'] = True
            logger.info(f"第一目标已达成: {symbol}")
            close_percent = float(self.target1_close_percent)
        elif target_level == 2:
            tracking['target2_reached'] = True
            logger.info(f"第二目标已达成: {symbol}")
            close_percent = float(self.target2_close_percent)
        else:
            close_percent = None

        # 剩余数量：优先使用交易所实际数量，缺失时按比例估算
        if exchange_qty is not None:
            tracking['remaining_quantity'] = float(exchange_qty)
        elif close_percent is not None:
            # 先取值再赋值（禁止 *=）：残缺条目（重启恢复）缺字段时不再抛 KeyError
            current_remaining = tracking.get('remaining_quantity')
            if current_remaining is None:
                logger.warning(
                    f"{symbol} 持仓跟踪缺少剩余数量，无法按比例估算",
                    target_level=target_level,
                )
            else:
                tracking['remaining_quantity'] = float(current_remaining) * (1 - close_percent)

    async def replenish_conditional_orders(
        self, symbol: str, entry_price: Decimal, *, missing: List[str]
    ) -> bool:
        """增量补全该标的缺失的保护条件单（只补缺失类型，绝不撤/改有效保护单）。

        P0-D 安全不变式：默认路径先完成全部只读准备（Phase A：读持仓/ensure-active/
        ATR/精度/现价/组价组量），再**只挂 `missing` 覆盖的类型**（Phase C），
        全程**零撤单**——消除「仅缺 TP 时先撤 SL 再重挂」造成的 SL 空窗。
        `missing` 由守卫 `find_missing_protection(strict=True)` 的权威结果传入
        （恒为非空列表）；空列表/None 由守卫提前返回，不进入本函数。

        Args:
            symbol: 交易对
            entry_price: 入场价格（从数据库恢复）
            missing: 缺失的保护单类型列表（关键字必填，取值 [_MISSING_SL]/[_MISSING_TP]
                或两者）。不设默认值：money-safety 函数不允许「静默默认」，强制每个
                调用点显式声明缺失类型，避免默认全类型导致重复挂 SL。

        Returns:
            bool: True 表示补全完成或无需补全（含无空头持仓）；False 表示存在缺口

        Note:
            应急回退：`trading.replenish.cancel_after_ready=false` 时回退到旧「全量
            重建」行为（先算后撤 → strict 撤单 → 全量挂 SL+TP1+TP2），**会重新引入
            SL 空窗**，仅供短期止血，不作为默认。
        """
        try:
            if not missing:
                # 空缺失类型属调用方违约：零挂零撤且不置位（fail-closed，守卫已保证非空）
                logger.warning(f"补全条件单缺少缺失类型入参，跳过: {symbol}")
                return False
            if self._should_skip_replenish(symbol):
                return True
            logger.info(f"开始补全条件单: {symbol}", entry_price=float(entry_price))
            status, plans = await self._prepare_replenish_plans(symbol, entry_price)
            if status == _REPLENISH_NO_POSITION:
                return True
            if status != _REPLENISH_READY:
                return False
            if not self.replenish_cancel_after_ready:
                # 应急回退：旧全量重建（先算后撤，撤单失败必须阻断挂新单，避免重复单）
                if not await self._cancel_orders_strict(symbol):
                    return False
                return await self._execute_replenish_plans(symbol, plans)
            return await self._execute_incremental_plans(symbol, plans, missing)
        except Exception as e:
            return self._handle_replenish_exception(symbol, e)

    def _should_skip_replenish(self, symbol: str) -> bool:
        """补全前置跳过判定（P0-3 A1/A2）：托管清单或已补全过则跳过。"""
        if symbol in self.replenish_skip_symbols:
            logger.debug(f"跳过托管交易对补全条件单: {symbol}")
            return True
        if symbol in self._replenished_symbols:
            logger.debug(f"条件单已补全过，跳过: {symbol}")
            return True
        return False

    async def _ensure_symbol_active(self, symbol: str) -> bool:
        """取 K 线前确保标的处于 active 注册态（P0-1，配置化重试）。

        Returns:
            bool: True 表示已 active（或开关关闭）；False 表示重试后仍失败
        """
        if not self.ensure_active_before_use:
            return True
        if symbol in self._registered_symbols:
            return True
        attempts = max(1, self.ensure_active_retries)
        for attempt in range(attempts):
            try:
                ok = await self.kline_service.register_symbol(
                    symbol, intervals=[self.kline_interval]
                )
            except Exception as e:
                logger.warning(f"K线服务注册异常: {symbol}: {e}")
                ok = False
            if ok:
                self._registered_symbols.add(symbol)
                logger.info(f"已确保 K 线注册为 active: {symbol}")
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(self.ensure_active_retry_interval)
        logger.warning(f"ensure-active 重试后仍失败，跳过 ATR 与撤单: {symbol}")
        return False

    async def _resolve_short_quantity(self, symbol: str) -> Decimal:
        """读取该标的的未平空头持仓数量（无持仓返回 0）。"""
        positions = await self.binance_api._request(
            "GET", "/papi/v1/um/positionRisk", signed=True
        )
        matched = self._match_exchange_short_qty(symbol, positions)
        return Decimal(str(matched)) if matched else Decimal('0')

    async def _get_current_price(self, symbol: str) -> Decimal:
        """读取标的当前价格（无效返回 0，由调用方判为准备失败）。"""
        ticker = await self.binance_api._request(
            "GET", "/fapi/v1/ticker/price",
            params={'symbol': symbol}, signed=False
        )
        return Decimal(str(ticker.get('price', 0)))

    def _plan_stop_loss(
        self, entry_price: Decimal, atr: Decimal, tick_size: Decimal, slippage: Decimal
    ) -> Dict[str, Decimal]:
        """组止损计划：最终止损价 = MAX(ATR止损, 紧急止损, 最小绝对止损)。

        Returns:
            dict: {'price': 触发价, 'limit_price': 限价}
        """
        min_stop = entry_price * (Decimal('1') + self.stop_loss_percent)
        emergency = entry_price * (Decimal('1') + self.emergency_stop_trigger_percent)
        atr_stop = entry_price + (atr * self.atr_stop_multiplier)
        stop_price = self._format_price(max(min_stop, emergency, atr_stop), tick_size)
        limit_price = self._format_price(stop_price * (Decimal('1') + slippage), tick_size)
        return {'price': stop_price, 'limit_price': limit_price}

    def _plan_take_profit(
        self, *, entry_price: Decimal, atr: Decimal, multiplier: Decimal,
        close_percent: Decimal, quantity: Decimal, tick_size: Decimal,
        step_size: Decimal, current_price: Decimal, slippage: Decimal
    ) -> Dict[str, Any]:
        """组单条止盈计划（TP1/TP2 共用，消除重复）。

        Returns:
            dict: 含 skip / market_close / price / limit_price / quantity / skip_reason
        """
        target_price = entry_price - (atr * multiplier)
        if target_price <= 0:
            return {'skip': True, 'skip_reason': '目标价<=0', 'price': target_price}
        price = self._format_price(target_price, tick_size)
        tp_quantity = self._format_quantity(quantity * close_percent, step_size)
        if tp_quantity <= 0:
            return {'skip': True, 'skip_reason': '数量<=0', 'price': price}
        if current_price <= price:
            # 现价已过目标：TP 应已触发，改为市价平仓该部分（纯判定，不依赖撤单结果）
            return {'skip': False, 'market_close': True, 'price': price, 'quantity': tp_quantity}
        notional = tp_quantity * price
        if notional < self.min_notional:
            return {
                'skip': True, 'price': price, 'quantity': tp_quantity,
                'skip_reason': f'名义价值{float(notional)}<{float(self.min_notional)}',
            }
        limit_price = self._format_price(price * (Decimal('1') + slippage), tick_size)
        return {
            'skip': False, 'market_close': False, 'price': price,
            'limit_price': limit_price, 'quantity': tp_quantity,
        }

    async def _prepare_replenish_plans(
        self, symbol: str, entry_price: Decimal
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Phase A：只读准备（读持仓/ensure-active/ATR/精度/现价/组价组量）。

        本阶段绝不撤单、绝不挂单；任何失败直接返回且不触碰交易所。

        Returns:
            (status, plans)：status ∈ {_REPLENISH_READY, _REPLENISH_NO_POSITION,
            _REPLENISH_FAILED}；仅 READY 时 plans 非空
        """
        current_quantity = await self._resolve_short_quantity(symbol)
        if current_quantity <= 0:
            logger.warning(f"未找到做空持仓，跳过补全条件单: {symbol}")
            return _REPLENISH_NO_POSITION, None
        if not await self._ensure_symbol_active(symbol):
            return _REPLENISH_FAILED, None
        atr = await self._calculate_atr(symbol)
        if atr <= 0:
            logger.warning(f"ATR计算失败，跳过补全条件单: {symbol}")
            return _REPLENISH_FAILED, None
        return await self._build_replenish_plans(symbol, entry_price, current_quantity, atr)

    async def _build_replenish_plans(
        self, symbol: str, entry_price: Decimal, quantity: Decimal, atr: Decimal
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """A7-A9：取精度/现价并组价组量（纯计算，只读，不触达交易所）。"""
        tick_size, step_size = await self._get_symbol_precision(symbol)
        if tick_size <= 0 or step_size <= 0:
            logger.warning(f"精度获取失败，跳过补全条件单: {symbol}")
            return _REPLENISH_FAILED, None
        current_price = await self._get_current_price(symbol)
        if current_price <= 0:
            logger.warning(f"现价获取失败，跳过补全条件单: {symbol}")
            return _REPLENISH_FAILED, None
        slippage = self.limit_order_slippage
        sl_plan = self._plan_stop_loss(entry_price, atr, tick_size, slippage)
        if sl_plan['price'] <= 0 or sl_plan['limit_price'] <= 0:
            logger.warning(f"止损计划不可用，跳过补全条件单: {symbol}")
            return _REPLENISH_FAILED, None
        tp1_plan = self._plan_take_profit(
            entry_price=entry_price, atr=atr, multiplier=self.target1_atr_multiplier,
            close_percent=self.target1_close_percent, quantity=quantity,
            tick_size=tick_size, step_size=step_size, current_price=current_price,
            slippage=slippage,
        )
        tp2_plan = self._plan_take_profit(
            entry_price=entry_price, atr=atr, multiplier=self.target2_atr_multiplier,
            close_percent=self.target2_close_percent, quantity=quantity,
            tick_size=tick_size, step_size=step_size, current_price=current_price,
            slippage=slippage,
        )
        plans = {
            'quantity': quantity, 'entry_price': entry_price, 'atr': atr,
            'sl': sl_plan, 'tp1': tp1_plan, 'tp2': tp2_plan,
        }
        return _REPLENISH_READY, plans

    async def _cancel_orders_strict(self, symbol: str) -> bool:
        """Phase B：严格撤旧条件单；异常或存在失败项即阻断挂新单（P0-3 B1）。

        Returns:
            bool: True 表示撤单成功（或本无旧单）；False 表示撤单失败
        """
        try:
            result = await self.cancel_all_algo_orders(symbol)
        except Exception as e:
            logger.error(f"撤销旧条件单异常，阻断挂新单: {symbol}", error=str(e))
            return False
        failed = int(result.get('failed', 0)) if isinstance(result, dict) else 0
        if failed > 0:
            logger.error(f"撤销旧条件单存在失败项，阻断挂新单: {symbol}", failed=failed)
            return False
        return True

    def _ensure_replenish_tracking(self, symbol: str, plans: Dict[str, Any]) -> None:
        """确保补挂前持仓跟踪已存在（挂单 / 部分平仓回写路径均依赖它）。

        经唯一工厂 `_build_tracking_entry` 构建，字段集与开仓 / 重启恢复路径一致；
        已存在则**保留不清空、不覆盖**（满足 INV-4：不覆盖既有 algo_ids）。
        """
        if symbol in self.position_tracking:
            return
        self.position_tracking[symbol] = self._build_tracking_entry(
            entry_price=plans['entry_price'], entry_quantity=plans['quantity'],
            atr=plans['atr'],
        )
        self._last_tracked_qty[symbol] = float(plans['quantity'])

    async def _backfill_algo_ids_from_db(self, symbol: str) -> None:
        """从 DB OPEN 记录尽力回填缺失的 algo_ids（best-effort，只填空缺、绝不覆盖）。

        增量路径下，判定为「已存在」的类型不会重新挂单；若其 algo_id 因重启等
        原因未写入本地 `position_tracking`，后续 `cancel_all_algo_orders` 会因
        `algo_ids` 非空而**跳过 DB 兜底查询**（见该函数 `if not algo_ids:`），
        导致该保护单被遗漏撤销。此处在挂缺失类型前按类型回填，保证取消依据完整。

        映射（DB 无法区分 TP 档位，按行序尽力填；algo_id 仅用于后续取消清理）：
        - `STOP_LOSS` → `'sl'`
        - `TAKE_PROFIT` → 依次填 `'tp1'`、`'tp2'`

        只填缺失键（`setdefault`），绝不覆盖既有键（满足 INV-4）；symbol 不在
        `position_tracking` 时不写；查询异常 / 字段缺失一律告警吞掉，不阻断补挂。
        """
        entry = self.position_tracking.get(symbol)
        if not isinstance(entry, dict):
            return
        algo_ids = entry.get('algo_ids')
        if not isinstance(algo_ids, dict):
            return
        try:
            rows = await self.db.fetch_all(
                "SELECT algo_id, order_type FROM condition_orders "
                "WHERE strategy_name='new_coin' AND status='OPEN' AND symbol=$1",
                symbol,
            )
        except Exception as e:
            logger.warning(
                "回填条件单 algoId 失败（忽略，不阻断补挂）", symbol=symbol, error=str(e)
            )
            return
        tp_slots = ('tp1', 'tp2')
        tp_index = 0
        for row in (rows or []):
            algo_id = row.get('algo_id')
            if algo_id is None:
                continue
            order_type = str(row.get('order_type', '')).upper()
            if order_type == 'STOP_LOSS':
                algo_ids.setdefault('sl', algo_id)
            elif order_type == 'TAKE_PROFIT' and tp_index < len(tp_slots):
                algo_ids.setdefault(tp_slots[tp_index], algo_id)
                tp_index += 1

    async def _place_protection_orders(
        self, symbol: str, plans: Dict[str, Any], *, place_sl: bool, place_tp: bool
    ) -> bool:
        """按需挂保护单（SL / TP1+TP2），返回本轮所需类型是否全部到位。

        增量与全量两条补挂路径共用的挂单内核：`place_sl` / `place_tp` 决定本次是否
        需要挂该类型，未请求的类型一律不挂、不撤。

        Returns:
            bool: True 表示本轮请求的类型全部挂成功或幂等命中；False 表示存在缺口
        """
        all_success = True
        if place_sl:
            ok = await self._place_conditional_and_record(
                symbol, algo_key='sl', stop_price=plans['sl']['price'],
                limit_price=plans['sl']['limit_price'], quantity=plans['quantity'],
            )
            all_success = all_success and ok
        if place_tp:
            for level, key in ((1, 'tp1'), (2, 'tp2')):
                ok = await self._apply_take_profit(symbol, plans[key], level)
                all_success = all_success and ok
        return all_success

    def _finalize_replenish(self, symbol: str, all_success: bool, *, label: str) -> None:
        """统一收尾：全部到位才置位 `_replenished_symbols`，并按结果记日志。

        Args:
            symbol: 交易对
            all_success: 本轮所需类型是否全部到位（挂成功或幂等命中）
            label: 日志标签（如「增量补全」「全量补全」），用于区分调用路径
        """
        if all_success:
            self._replenished_symbols.add(symbol)
            logger.info(f"条件单{label}完成: {symbol}")
        else:
            logger.warning(f"条件单{label}部分失败: {symbol}")

    async def _execute_incremental_plans(
        self, symbol: str, plans: Dict[str, Any], missing: List[str]
    ) -> bool:
        """Phase C（增量）：只挂 `missing` 覆盖的类型，绝不撤/改既有有效保护单。

        类型级映射（方案甲，已知局限见 `_MISSING_TP` 注释）：
        - `_MISSING_SL in missing` → 补 1 条 SL；
        - `_MISSING_TP in missing` → 视为 TP 全缺，补 TP1+TP2。
        `missing` 未含的类型一律不挂、不撤。

        首步幂等建立持仓跟踪（已存在则保留，**不清空、不覆盖** algo_ids），紧接
        从 DB 回填既有保护单的 algo_id（best-effort，保证取消依据完整）；只有本轮
        全部缺失类型「挂成功或幂等命中」才置位 `_replenished_symbols`。
        `market_close` 分支只做 TP 的部分市价平仓，绝不撤 SL（P0-D-AC14）。

        Returns:
            bool: True 表示缺失类型全部补全成功；False 表示存在缺口
        """
        # 挂单路径（_place_conditional_and_record / _mark_partial_close）依赖 tracking 已存在
        self._ensure_replenish_tracking(symbol, plans)
        # 回填既有保护单 algo_id：避免 algo_ids 非空时 cancel_all_algo_orders 跳过 DB 兜底
        await self._backfill_algo_ids_from_db(symbol)
        all_success = await self._place_protection_orders(
            symbol, plans,
            place_sl=_MISSING_SL in missing, place_tp=_MISSING_TP in missing,
        )
        self._finalize_replenish(symbol, all_success, label="增量补全")
        return all_success

    async def _execute_replenish_plans(self, symbol: str, plans: Dict[str, Any]) -> bool:
        """Phase C（回退全量）：撤单完成后挂 SL/TP1/TP2；失败记录缺口，下周期收敛。

        Phase C 首步才建立持仓跟踪（Phase A 保持纯只读）：只有撤单已成功、即将
        挂单时才创建条目，避免撤单失败留下「已建 tracking 但无 algo_ids」的半成品。

        Returns:
            bool: True 表示全部补全成功（置位 _replenished_symbols）；False 表示存在缺口
        """
        # 挂单路径（_place_conditional_and_record / _mark_partial_close）依赖 tracking 已存在
        self._ensure_replenish_tracking(symbol, plans)
        all_success = await self._place_protection_orders(
            symbol, plans, place_sl=True, place_tp=True,
        )
        self._finalize_replenish(symbol, all_success, label="全量补全")
        return all_success

    async def _apply_take_profit(self, symbol: str, plan: Dict[str, Any], level: int) -> bool:
        """执行单条止盈计划：跳过 / 市价平仓 / 挂条件单。

        Returns:
            bool: True 表示无缺口（成功或按计划跳过）；False 表示挂单真实失败
        """
        if plan.get('skip'):
            logger.info(f"TP{level} 跳过补全: {symbol}", reason=plan.get('skip_reason'))
            return True
        if plan.get('market_close'):
            logger.info(
                f"当前价格已低于 TP{level} 目标价，直接市价平仓该部分",
                symbol=symbol, price=float(plan['price'])
            )
            await self._market_close_partial(symbol, plan['quantity'], level)
            return True
        return await self._place_conditional_and_record(
            symbol, algo_key=f'tp{level}', stop_price=plan['price'],
            limit_price=plan['limit_price'], quantity=plan['quantity'],
        )

    async def _market_close_partial(self, symbol: str, quantity: Decimal, level: int) -> None:
        """以市价平掉部分做空持仓（TP 已过目标价时使用）。"""
        if quantity <= 0:
            return
        try:
            await self.binance_api.place_order(
                symbol=symbol, side='BUY', order_type='MARKET',
                quantity=quantity, reduce_only=True
            )
            self._mark_partial_close(symbol, float(quantity), level)
            logger.info(f"市价平仓 TP{level} 部分成功: {symbol}", quantity=float(quantity))
        except Exception as e:
            logger.warning(f"市价平仓 TP{level} 部分失败: {symbol}", error=str(e))

    async def _place_conditional_and_record(
        self, symbol: str, *, algo_key: str,
        stop_price: Decimal, limit_price: Decimal, quantity: Decimal
    ) -> bool:
        """挂一条保护条件单并记录 algo_id（幂等：已存在的错误码视为成功）。

        Returns:
            bool: True 表示挂单成功或订单已存在；False 表示真实失败
        """
        order_type, record_type, exists_msg, success_msg = _CONDITION_ORDER_META[algo_key]
        try:
            result = await self.binance_api.place_conditional_order(
                symbol=symbol, side='BUY', order_type=order_type,
                stop_price=stop_price, price=limit_price,
                quantity=quantity, reduce_only=True
            )
        except Exception as e:
            error_str = str(e)
            if any(code in error_str for code in self.replenish_ignore_error_codes):
                logger.info(f"{exists_msg}: {symbol}")
                return True
            logger.warning(f"{success_msg.replace('成功', '失败')}: {symbol}", error=error_str)
            return False
        if result and 'algoId' in result and symbol in self.position_tracking:
            self.position_tracking[symbol]['algo_ids'][algo_key] = result['algoId']
            await record_condition_order(
                self.db, "new_coin", symbol,
                algo_id=result['algoId'], order_type=record_type
            )
        algo_id = result.get('algoId', 'N/A') if result else 'N/A'
        logger.info(
            f"{success_msg}: {symbol}",
            price=float(stop_price), quantity=float(quantity), algo_id=algo_id
        )
        return True

    def _handle_replenish_exception(self, symbol: str, error: Exception) -> bool:
        """补全顶层异常收敛：幂等错误码视为已处理，避免无限重试。"""
        error_str = str(error)
        if any(code in error_str for code in self.replenish_ignore_error_codes):
            logger.warning(
                f"条件单创建失败（订单可能已存在或仓位已变化），标记为已处理: {symbol}",
                error=error_str
            )
            self._replenished_symbols.add(symbol)
            return True
        logger.error(f"补全条件单失败: {symbol}", error=error_str, exc_info=True)
        return False
