"""
交易执行模块
执行做空交易、设置止损止盈
"""
from typing import Dict, Any, Optional
import asyncio
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import structlog

from shared.binance_api import BinanceClient, BinanceAPIError
from shared.database import DatabaseManager
from shared.notification import NotificationClient
from shared.kline_service import KLineService
from shared.condition_orders import record_condition_order
from shared.dynamic_trailing import (
    calculate_dynamic_trailing_stop,
    get_volatility_adjustment,
    TrailingStopResult,
)


logger = structlog.get_logger()


class TradingExecutor:
    """交易执行器

    功能：
    - 执行做空订单
    - 设置止损止盈
    - 记录交易日志
    - 发送通知
    """

    # 补全 TP2 条件单时忽略的错误码（订单已存在 / 仓位已变化 / 名义价值不足）
    _TP2_IGNORE_ERROR_CODES = ['-4164', '-2011', '-2021']
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
        kline_service: Optional[KLineService] = None
    ):
        """
        初始化交易执行器

        Args:
            binance_api: Binance API 客户端
            db: 数据库管理器
            notification: 通知客户端
            config: 配置字典
            kline_service: K线服务（用于ATR计算）
        """
        self.binance_api = binance_api
        self.db = db
        self.notification = notification
        self.config = config
        self.kline_service = kline_service

        # 交易配置
        trading_config = config.get('trading', {})
        self.leverage = trading_config.get('leverage', 2)
        self.max_positions = trading_config.get('max_positions', 3)
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
        
        # 紧急止损配置
        self.emergency_stop_enabled = trading_config.get('emergency_stop', {}).get('enabled', True)
        self.emergency_stop_check_minutes = trading_config.get('emergency_stop', {}).get('check_minutes', 15)
        self.emergency_stop_trigger_percent = Decimal(str(trading_config.get('emergency_stop', {}).get('trigger_percent', 0.015)))
        
        # ATR止损配置
        self.atr_stop_multiplier = Decimal(str(trading_config.get('atr_stop', {}).get('multiplier', 2.5)))
        
        # 限价单滑点参数（限价相对于触发价的偏移量，默认 0.1%）
        self.limit_order_slippage = Decimal(str(trading_config.get("limit_order_slippage", 0.001)))

        # 默认精度（API获取失败时的兜底值）
        default_precision = trading_config.get('default_precision', {})
        self.default_tick_size = Decimal(str(default_precision.get('tick_size', '0.01')))
        self.default_step_size = Decimal(str(default_precision.get('step_size', '0.001')))

        # 平仓比例（全仓平仓时使用）
        close_pos_config = trading_config.get('close_position', {})
        self.close_percent = Decimal(str(close_pos_config.get('close_percent', 1.0)))

        # 持仓跟踪（用于移动止盈和时间止损）
        self.position_tracking: Dict[str, Dict[str, Any]] = {}

        # 已补全TP2的币种集合（避免重复补全）
        self._replenished_symbols: set = set()

        # 波动率计算缓存（用于动态利润保护）
        self._volatility_cache: Dict[str, Any] = {}

        logger.info(
            "交易执行器初始化完成",
            leverage=self.leverage,
            max_positions=self.get_max_positions(),
            single_position_margin=float(self.single_position_margin),
            batch_take_profit_enabled=self.batch_take_profit_enabled,
            time_stop_enabled=self.time_stop_enabled
        )

    def get_max_positions(self) -> int:
        """获取最大持仓数量"""
        return self.max_positions

    async def execute_short(
        self,
        symbol: str,
        score_result: Dict[str, Any],
        current_price: float
    ) -> Optional[Dict[str, Any]]:
        """
        执行做空交易

        Args:
            symbol: 交易对
            score_result: 评分结果字典
            current_price: 当前价格

        Returns:
            订单信息字典，失败返回 None
        """
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
                return None

            # 2. 计算仓位大小
            position_size = self._calculate_position_size(balance, current_price)

            if position_size <= 0:
                logger.error("仓位大小计算失败")
                return None

            # 3. 获取交易对精度
            tick_size, step_size = await self._get_symbol_precision(symbol)

            # 4. 格式化数量
            quantity = self._format_quantity(position_size, step_size)

            # 5. 设置杠杆
            await self._set_leverage(symbol, self.leverage)

            # 6. 开空仓（传入当前价格作为限价）
            order = await self._place_short_order(symbol, quantity, Decimal(str(current_price)))

            if not order:
                logger.error("开空仓失败")
                return None

            # 7. 保存订单到数据库
            await self._save_order(order, score_result)

            # 8. 计算ATR（用于分批止盈）
            atr = await self._calculate_atr(symbol)
            
            # 9. 初始化持仓跟踪（必须在创建条件单之前，确保 record_condition_order 能正常执行）
            self.position_tracking[symbol] = {
                'entry_price': current_price,
                'entry_time': datetime.now(timezone.utc),  # 使用带时区的时间
                'entry_quantity': float(quantity),
                'atr': float(atr),
                'lowest_price': current_price,  # 记录持仓期间的最低价
                'target1_reached': False,
                'target2_reached': False,
                'remaining_quantity': float(quantity),
                'algo_ids': {},  # 存储条件单的 algoId，key='sl'/'tp1'/'tp2'/'trailing_stop'
                # 动态利润保护字段
                'direction': 'SHORT',
                'highest_price': current_price,  # 做空时追踪最高价（反弹触发止损用）
                'trailing_activated': False,
                'trailing_stop_price': None,
                'pending_profit_pct': None,
                'current_tier_index': -1,
            }

            # 10. 设置止损止盈（根据配置选择策略）
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

            # 11. 发送通知
            await self._send_notification(symbol, order, current_price, score_result)

            logger.info(
                f"做空执行成功: {symbol}",
                order_id=str(order.get('orderId')),
                quantity=quantity,
                atr=float(atr)
            )

            return order

        except Exception as e:
            logger.error(
                f"执行做空失败: {symbol}",
                error=str(e),
                exc_info=True
            )
            return None

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
        balance: Decimal,
        current_price: float
    ) -> Decimal:
        """
        计算仓位大小

        Args:
            balance: 账户余额
            current_price: 当前价格

        Returns:
            仓位大小（数量）
        """
        # 使用配置的单笔保证金
        margin = self.single_position_margin

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
                quantity=float(quantity)
            )
            return quantity

        return Decimal('0')

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
        current_price: Decimal
    ) -> Optional[Dict[str, Any]]:
        """
        开空仓（使用限价单）

        Args:
            symbol: 交易对
            quantity: 数量
            current_price: 当前价格（用于限价单）

        Returns:
            订单信息
        """
        try:
            order = await self.binance_api.place_order(
                symbol=symbol,
                side='SELL',
                order_type='LIMIT',
                quantity=quantity,
                price=current_price,
                timeInForce='GTC'
            )

            logger.info(
                f"开空仓成功: {symbol}",
                order_id=order.get('orderId'),
                quantity=float(quantity),
                price=float(current_price),
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

            # 设置止损单（限价条件单）
            sl_result = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side='BUY',
                order_type='STOP',
                stop_price=stop_loss_price,
                price=stop_limit_price,
                quantity=quantity,
                closePosition=True
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

            # 设置止盈单（限价条件单）
            tp_result = await self.binance_api.place_conditional_order(
                symbol=symbol,
                side='BUY',
                order_type='TAKE_PROFIT',
                stop_price=take_profit_price,
                price=tp_limit_price,
                quantity=quantity,
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
                
        except Exception as e:
            logger.error(f"计算ATR失败: {symbol}, 错误: {e}")
            return Decimal('0')
    
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
                closePosition=True
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
            # 计算持仓时长（分钟）
            holding_minutes = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
            
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
                
                # 清理持仓跟踪
                if symbol in self.position_tracking:
                    del self.position_tracking[symbol]
                
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
        - 持仓超过72小时且未达第一目标
        - 自动平仓100%
        
        Args:
            symbol: 交易对
            entry_time: 入场时间
        """
        try:
            # 计算持仓时长
            holding_hours = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            
            if holding_hours >= self.max_holding_hours:
                # 检查是否达到第一目标
                tracking = self.position_tracking.get(symbol, {})
                if not tracking.get('target1_reached'):
                    logger.warning(
                        f"触发时间止损: {symbol}",
                        holding_hours=holding_hours,
                        max_holding_hours=self.max_holding_hours
                    )
                    
                    # 平仓100%
                    await self._close_position(symbol, self.close_percent, "时间止损")
                    
                    # 清除持仓跟踪
                    if symbol in self.position_tracking:
                        del self.position_tracking[symbol]
                    
                    # 发送通知
                    await self.notification.send(
                        message=f"【时间止损触发】\n交易对: {symbol}\n持仓时长: {holding_hours:.1f}小时\n已平仓100%",
                        level="warning",
                        project="new_coin"
                    )
            
        except Exception as e:
            logger.error(f"检查时间止损失败: {symbol}, 错误: {e}")
    
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
                remaining_quantity = Decimal(str(tracking.get('remaining_quantity', 0)))
                if remaining_quantity > 0:
                    await self._close_position(symbol, self.close_percent, "移动止盈")
                
                # 清除持仓跟踪
                if symbol in self.position_tracking:
                    del self.position_tracking[symbol]
                
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
        平仓
        
        Args:
            symbol: 交易对
            close_percent: 平仓比例（0-1）
            reason: 平仓原因
            
        Returns:
            是否成功
        """
        try:
            # 获取当前持仓
            positions = await self.binance_api.get_position(symbol)
            
            short_position = None
            for pos in positions:
                # 单方向模式下 positionSide='BOTH'，用 positionAmt<0 判断
                if float(pos.get('positionAmt', 0)) < 0:
                    short_position = pos
                    break
            
            if not short_position:
                logger.warning(f"未找到做空持仓: {symbol}")
                return False
            
            # 计算平仓数量
            position_amt = abs(Decimal(str(short_position.get('positionAmt', 0))))
            close_quantity = position_amt * close_percent
            
            # 获取数量精度
            _, step_size = await self._get_symbol_precision(symbol)
            close_quantity = self._format_quantity(close_quantity, step_size)
            
            # 获取当前价格用于限价平仓
            ticker = await self.binance_api.get_ticker(symbol)
            close_price = float(ticker.get("lastPrice", 0))

            if close_price <= 0:
                logger.warning("无法获取当前价格，使用市价平仓", symbol=symbol)
                order = await self.binance_api.place_order(
                    symbol=symbol,
                    side='BUY',
                    order_type='MARKET',
                    quantity=close_quantity
                )
            else:
                # 优先使用限价单平仓，带超时重试机制
                close_pos_config = self.config.get('trading', {}).get('close_position', {})
                max_retries = close_pos_config.get('max_retries', 3)
                retry_interval = close_pos_config.get('retry_interval', 2)  # 重试间隔（秒）
                poll_interval = close_pos_config.get('poll_interval', 2)   # 轮询间隔（秒）
                timeout = close_pos_config.get('timeout', 10)              # 单次限价单超时（秒）

                filled = False
                order = None
                last_error = None

                for retry_attempt in range(max_retries + 1):
                    try:
                        # 每次重试更新价格（使用订单簿最优价，若无则用最新价）
                        try:
                            orderbook = await self.binance_api.get_orderbook(symbol, limit=5)
                            limit_price = Decimal(str(orderbook['bids'][0][0]))
                        except Exception:
                            orderbook = {}  # 避免后续 orderbook.get() 引发 NameError
                            limit_price = Decimal(str(close_price))

                        # 调整价格精度
                        tick_size, _ = await self._get_symbol_precision(symbol)
                        limit_price = self._format_price(limit_price, tick_size)

                        # 确保 orderbook 有 bids 数据
                        if not orderbook.get('bids'):
                            logger.warning("订单簿 bids 为空，使用最新价作为限价", symbol=symbol)
                            limit_price = Decimal(str(close_price))
                            limit_price = self._format_price(limit_price, tick_size)

                        logger.info(
                            f"限价平仓（第{retry_attempt + 1}次）",
                            symbol=symbol,
                            limit_price=float(limit_price)
                        )

                        order_result = await self.binance_api.place_order(
                            symbol=symbol,
                            side='BUY',
                            order_type='LIMIT',
                            quantity=close_quantity,
                            price=limit_price,
                            timeInForce='GTC'
                        )

                        # 轮询等待成交
                        elapsed = 0
                        while elapsed < timeout:
                            await asyncio.sleep(poll_interval)
                            elapsed += poll_interval

                            open_orders = await self.binance_api.get_open_orders(symbol)
                            order_still_open = any(
                                str(o.get('orderId')) == str(order_result['orderId'])
                                for o in open_orders
                            )

                            if not order_still_open:
                                logger.info(
                                    "限价平仓已成交",
                                    symbol=symbol,
                                    order_id=order_result.get('orderId'),
                                    elapsed_seconds=elapsed,
                                    retry_attempt=retry_attempt
                                )
                                order = order_result
                                filled = True
                                break

                        if filled:
                            break

                        # 超时未成交，撤销后重试
                        try:
                            await self.binance_api.cancel_order(symbol, str(order_result['orderId']))
                            logger.info(
                                "限价平仓超时，撤销后重试",
                                symbol=symbol,
                                retry_attempt=retry_attempt + 1,
                                max_retries=max_retries
                            )
                        except Exception as cancel_error:
                            # 订单可能在轮询与取消之间成交
                            if hasattr(cancel_error, 'code') and cancel_error.code == self._ORDER_NOT_FOUND_CODE:
                                logger.info(
                                    "限价平仓单已成交（取消时确认）",
                                    symbol=symbol,
                                    order_id=order_result.get('orderId')
                                )
                                order = order_result
                                filled = True
                                break
                            else:
                                raise

                        if retry_attempt < max_retries:
                            await asyncio.sleep(retry_interval)

                    except Exception as e:
                        last_error = e
                        logger.warning(
                            "限价平仓异常",
                            symbol=symbol,
                            error=str(e),
                            retry_attempt=retry_attempt + 1
                        )
                        if retry_attempt < max_retries:
                            await asyncio.sleep(retry_interval)

                if not filled:
                    # 所有重试均失败，回退到市价单
                    logger.warning(
                        "限价平仓所有重试均未成交，回退市价单",
                        symbol=symbol,
                        last_error=str(last_error) if last_error else "超时未成交"
                    )
                    order = await self.binance_api.place_order(
                        symbol=symbol,
                        side='BUY',
                        order_type='MARKET',
                        quantity=close_quantity
                    )
            
            if order:
                logger.info(
                    f"平仓成功: {symbol}",
                    reason=reason,
                    quantity=float(close_quantity),
                    order_id=order.get('orderId')
                )
                return True
            else:
                logger.error(f"平仓失败: {symbol}")
                return False
                
        except Exception as e:
            logger.error(f"平仓失败: {symbol}, 错误: {e}")
            return False
    
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
    
    def update_target_status(self, symbol: str, target_level: int) -> None:
        """
        更新目标达成状态
        
        当止盈单成交后，由外部调用此方法更新状态
        
        Args:
            symbol: 交易对
            target_level: 目标级别（1或2）
        """
        if symbol not in self.position_tracking:
            return
        
        tracking = self.position_tracking[symbol]
        
        if target_level == 1:
            tracking['target1_reached'] = True
            tracking['remaining_quantity'] *= (1 - float(self.target1_close_percent))
            logger.info(f"第一目标已达成: {symbol}")
        elif target_level == 2:
            tracking['target2_reached'] = True
            tracking['remaining_quantity'] *= (1 - float(self.target2_close_percent))
            logger.info(f"第二目标已达成: {symbol}")

    async def replenish_conditional_orders(self, symbol: str, entry_price: Decimal) -> bool:
        """
        为现有持仓补全缺失的条件单（止损 SL、TP1、TP2）
        
        策略重启后调用，补全所有缺失的条件单：
        - SL：止损条件单（closePosition，全仓止损）
        - TP1：第一目标止盈（reduce_only，30%）
        - TP2：第二目标止盈（reduce_only，40%）
        
        由于币安条件单查询 API 已废弃，无法判断哪些条件单活跃，
        因此采用"幂等创建"策略：如果订单已存在，币安会返回错误码，我们忽略即可。
        
        Args:
            symbol: 交易对
            entry_price: 入场价格（从数据库恢复）
            
        Returns:
            是否成功补全
        """
        try:
            # MCTPS 交易对由 MCTPS 策略管理，不在此补全条件单
            mctps_symbols = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"}
            if symbol in mctps_symbols:
                logger.debug(f"跳过 MCTPS 交易对补全条件单: {symbol}")
                return True

            # 如果该币种已补全过，跳过
            if symbol in self._replenished_symbols:
                logger.debug(f"条件单已补全过，跳过: {symbol}")
                return True

            logger.info(
                f"开始补全条件单: {symbol}",
                entry_price=float(entry_price)
            )

            # 1. 获取当前持仓数量
            positions = await self.binance_api._request(
                "GET", "/papi/v1/um/positionRisk", signed=True
            )
            short_position = None
            for pos in positions:
                if pos.get('symbol') == symbol and float(pos.get('positionAmt', 0)) < 0:
                    short_position = pos
                    break
            
            if not short_position:
                logger.warning(
                    f"未找到做空持仓，跳过补全条件单: {symbol}"
                )
                return False
            
            current_quantity = abs(Decimal(str(short_position['positionAmt'])))
            
            # 2. 计算ATR
            atr = await self._calculate_atr(symbol)
            if atr <= 0:
                logger.warning(
                    f"ATR计算失败，跳过补全条件单: {symbol}"
                )
                return False
            
            # 3. 获取精度
            tick_size, step_size = await self._get_symbol_precision(symbol)
            
            # 4. 初始化持仓跟踪（如果不存在）
            if symbol not in self.position_tracking:
                self.position_tracking[symbol] = {
                    'entry_price': float(entry_price),
                    'entry_time': datetime.now(timezone.utc),
                    'entry_quantity': float(current_quantity),
                    'atr': float(atr),
                    'lowest_price': float(entry_price),
                    'highest_price': float(entry_price),
                    'target1_reached': False,
                    'target2_reached': False,
                    'remaining_quantity': float(current_quantity),
                    'algo_ids': {},
                    'direction': 'SHORT',
                    'trailing_activated': False,
                    'trailing_stop_price': None,
                    'pending_profit_pct': None,
                    'current_tier_index': -1,
                }
            
            # 5. 获取当前价格
            ticker = await self.binance_api._request(
                "GET", "/fapi/v1/ticker/price",
                params={'symbol': symbol},
                signed=False
            )
            current_price = Decimal(str(ticker.get('price', 0)))
            
            # 用于记录整体是否全部成功
            all_success = True
            # 订单已存在时忽略的错误码（幂等创建）
            ignore_error_codes = {'-4164', '-2011', '-2021'}
            slippage = self.limit_order_slippage

            # === 6. 补全止损条件单（SL）===
            # 计算最终止损价 = MAX(ATR止损, 紧急止损, 最小绝对止损)
            min_stop_price = entry_price * (Decimal('1') + self.stop_loss_percent)
            emergency_stop_price = entry_price * (Decimal('1') + self.emergency_stop_trigger_percent)
            atr_stop_price = entry_price + (atr * self.atr_stop_multiplier)
            final_stop_price = max(min_stop_price, emergency_stop_price, atr_stop_price)
            stop_loss_price = self._format_price(final_stop_price, tick_size)
            stop_limit_price = self._format_price(stop_loss_price * (Decimal('1') + slippage), tick_size)

            try:
                sl_result = await self.binance_api.place_conditional_order(
                    symbol=symbol, side='BUY',
                    order_type='STOP',
                    stop_price=stop_loss_price,
                    price=stop_limit_price,
                    quantity=current_quantity,
                    closePosition=True
                )
                if sl_result and 'algoId' in sl_result and symbol in self.position_tracking:
                    self.position_tracking[symbol]['algo_ids']['sl'] = sl_result['algoId']
                    await record_condition_order(
                        self.db, "new_coin", symbol,
                        algo_id=sl_result['algoId'],
                        order_type="STOP_LOSS"
                    )
                logger.info(
                    f"补全止损条件单成功: {symbol}",
                    stop_loss=float(stop_loss_price),
                    algo_id=sl_result.get('algoId', 'N/A')
                )
            except Exception as e:
                error_str = str(e)
                if any(code in error_str for code in ignore_error_codes):
                    logger.info(f"止损条件单已存在，跳过: {symbol}")
                else:
                    logger.warning(f"补全止损条件单失败: {symbol}", error=error_str)
                    all_success = False

            # === 7. 补全 TP1 条件单 ===
            target1_price = entry_price - (atr * self.target1_atr_multiplier)
            target1_price = self._format_price(target1_price, tick_size)
            target1_quantity = current_quantity * self.target1_close_percent
            target1_quantity = self._format_quantity(target1_quantity, step_size)
            tp1_limit_price = self._format_price(target1_price * (Decimal('1') + slippage), tick_size)

            if target1_quantity > 0 and target1_price > 0:
                try:
                    tp1_result = await self.binance_api.place_conditional_order(
                        symbol=symbol, side='BUY',
                        order_type='TAKE_PROFIT',
                        stop_price=target1_price,
                        price=tp1_limit_price,
                        quantity=target1_quantity,
                        reduce_only=True
                    )
                    if tp1_result and 'algoId' in tp1_result and symbol in self.position_tracking:
                        self.position_tracking[symbol]['algo_ids']['tp1'] = tp1_result['algoId']
                        await record_condition_order(
                            self.db, "new_coin", symbol,
                            algo_id=tp1_result['algoId'],
                            order_type="TAKE_PROFIT"
                        )
                    logger.info(
                        f"补全 TP1 止盈条件单成功: {symbol}",
                        target_price=float(target1_price),
                        quantity=float(target1_quantity),
                        algo_id=tp1_result.get('algoId', 'N/A')
                    )
                except Exception as e:
                    error_str = str(e)
                    if any(code in error_str for code in ignore_error_codes):
                        logger.info(f"TP1 止盈条件单已存在，跳过: {symbol}")
                    else:
                        logger.warning(f"补全 TP1 止盈条件单失败: {symbol}", error=error_str)
                        all_success = False

            # === 8. 补全 TP2 条件单 ===
            target2_price = entry_price - (atr * self.target2_atr_multiplier)

            if target2_price <= 0:
                logger.info(
                    f"TP2 价格无效（<=0），跳过补全",
                    symbol=symbol,
                    target2_price=float(target2_price)
                )
            elif current_price <= target2_price:
                logger.info(
                    f"当前价格已低于 TP2 目标价，TP2 应已触发，直接市价平仓 TP2 部分",
                    symbol=symbol,
                    current_price=float(current_price),
                    target2_price=float(target2_price)
                )
                try:
                    tp2_quantity = current_quantity * self.target2_close_percent
                    tp2_quantity = self._format_quantity(tp2_quantity, step_size)
                    if tp2_quantity > 0:
                        await self.binance_api.place_order(
                            symbol=symbol, side='BUY',
                            order_type='MARKET',
                            quantity=tp2_quantity,
                            reduce_only=True
                        )
                        logger.info(
                            f"市价平仓 TP2 部分成功: {symbol}",
                            quantity=float(tp2_quantity)
                        )
                        if symbol in self.position_tracking:
                            remaining = self.position_tracking[symbol]['remaining_quantity'] - float(tp2_quantity)
                            self.position_tracking[symbol]['remaining_quantity'] = max(0, remaining)
                            self.position_tracking[symbol]['target2_reached'] = True
                except Exception as e:
                    logger.warning(
                        f"市价平仓 TP2 部分失败: {symbol}",
                        error=str(e)
                    )
            else:
                # 创建 TP2 条件单
                tp2_quantity = current_quantity * self.target2_close_percent
                tp2_quantity = self._format_quantity(tp2_quantity, step_size)
                tp2_price = self._format_price(target2_price, tick_size)
                tp2_limit_price = self._format_price(tp2_price * (Decimal('1') + slippage), tick_size)

                try:
                    tp2_result = await self.binance_api.place_conditional_order(
                        symbol=symbol, side='BUY',
                        order_type='TAKE_PROFIT',
                        stop_price=tp2_price,
                        price=tp2_limit_price,
                        quantity=tp2_quantity,
                        reduce_only=True
                    )
                    if tp2_result and 'algoId' in tp2_result and symbol in self.position_tracking:
                        self.position_tracking[symbol]['algo_ids']['tp2'] = tp2_result['algoId']
                        await record_condition_order(
                            self.db, "new_coin", symbol,
                            algo_id=tp2_result['algoId'],
                            order_type="TAKE_PROFIT"
                        )
                    logger.info(
                        f"补全 TP2 止盈条件单成功: {symbol}",
                        target_price=float(tp2_price),
                        quantity=float(tp2_quantity),
                        algo_id=tp2_result.get('algoId', 'N/A')
                    )
                except Exception as e:
                    error_str = str(e)
                    if any(code in error_str for code in ignore_error_codes):
                        logger.info(f"TP2 止盈条件单已存在，跳过: {symbol}")
                    else:
                        logger.warning(f"补全 TP2 止盈条件单失败: {symbol}", error=error_str)
                        all_success = False

            # 标记补全完成
            if all_success:
                self._replenished_symbols.add(symbol)
                logger.info(f"条件单全部补全完成: {symbol}")
            else:
                logger.warning(f"条件单补全部分失败: {symbol}")

            return all_success

        except Exception as e:
            error_str = str(e)
            # 如果是因为订单已存在等原因失败，标记为已处理避免无限重试
            if any(code in error_str for code in self._TP2_IGNORE_ERROR_CODES):
                logger.warning(
                    f"条件单创建失败（订单可能已存在或仓位已变化），标记为已处理: {symbol}",
                    error=error_str
                )
                self._replenished_symbols.add(symbol)
                return True
            logger.error(
                f"补全条件单失败: {symbol}",
                error=error_str,
                exc_info=True
            )
            return False
