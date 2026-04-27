#!/usr/bin/env python3
"""
交易执行模块

功能：
1. 执行开仓交易（限价单）
2. 设置止损止盈订单
3. 管理订单执行流程
4. 处理交易执行异常
"""

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional
from utils.binance_trade_api import BinanceTradeAPI
from services.frequency_controller import FrequencyController
from config.settings import BINANCE_TESTNET

logger = logging.getLogger(__name__)


class TradeExecutor:
    """交易执行类"""

    def __init__(self, frequency_controller: FrequencyController):
        """
        初始化交易执行器

        Args:
            frequency_controller: 频率控制器实例
        """
        self.frequency_controller = frequency_controller
        self.trade_api = None

        # 初始化交易 API
        try:
            self.trade_api = BinanceTradeAPI(testnet=BINANCE_TESTNET)
            logger.info("已初始化币安交易 API")
        except Exception as e:
            logger.warning(f"币安交易 API 初始化失败：{e}")
            self.trade_api = None

    def execute_trades(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        执行交易（使用币安 API）

        Args:
            signals: 信号列表

        Returns:
            执行的交易列表
        """
        executed = []

        if not self.trade_api:
            logger.warning("交易 API 未初始化，无法执行交易")
            return executed

        # 构建信号推送内容
        signal_messages = []

        for i, signal in enumerate(signals):
            try:
                symbol = signal['币种']
                orders = signal.get('orders', {})

                # v6.12: 频率控制检查
                trade_allowed, reason = self.frequency_controller.check_trade_allowed(symbol)
                if not trade_allowed:
                    logger.warning(f"⛔ {symbol} 禁止开仓：{reason}")
                    signal_messages.append(f"⛔ {symbol} {signal['开仓方向']} 等级:{signal['信号等级']} 禁止开仓：{reason}")
                    continue  # 跳过该信号

                logger.info(f"准备执行：{symbol} {signal['开仓方向']}")

                # 添加延迟：避免触发币安 API 频率限制（每笔交易间隔 8 秒）
                # v6.13.3: 从 2 秒增加到 8 秒，避免 -1015 错误
                if i > 0:
                    logger.info(f"  ⏳ 等待 8 秒后执行下一个交易对（避免 API 限流）...")
                    time.sleep(8)

                # 执行单笔交易
                trade_record = self._execute_single_trade(signal, orders)

                if trade_record:
                    executed.append(trade_record)
                    signal_messages.append(f"✅ {symbol} {signal['开仓方向']} 等级:{signal['信号等级']} 开仓成功")
                else:
                    signal_messages.append(f"❌ {symbol} {signal['开仓方向']} 等级:{signal['信号等级']} 开仓失败")

            except Exception as e:
                logger.error(f"  ❌ 交易执行失败：{signal['币种']} - {str(e)}", exc_info=True)
                signal_messages.append(f"❌ {signal['币种']} {signal['开仓方向']} 等级:{signal['信号等级']} 开仓失败：{str(e)}")

        return executed, signal_messages

    def _execute_single_trade(self, signal: Dict[str, Any], orders: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        执行单笔交易

        Args:
            signal: 信号字典
            orders: 订单参数字典

        Returns:
            交易记录字典，如果失败则返回 None
        """
        symbol = signal['币种']

        try:
            # 步骤 1: 设置杠杆
            leverage = signal.get('实际杠杆', 5)
            logger.info(f"  设置杠杆：{leverage}x")
            self.trade_api.set_um_leverage(symbol, leverage=leverage)

            # 添加延迟：设置杠杆后等待 1 秒
            time.sleep(1)

            # 步骤 2: 执行开仓（v6.13.2 限价单）
            entry_order = orders.get('entry', {})
            entry_price = Decimal(str(entry_order.get('price', 0)))
            logger.info(f"  执行开仓：{entry_order} (限价单 - v6.13.2)")

            # v6.13.2: 改用限价单，降低手续费（taker 0.05% → maker 0.02%）
            entry_result = self.trade_api.place_limit_order(
                symbol=entry_order.get('symbol'),
                side=entry_order.get('side'),
                position_side=entry_order.get('position_side'),
                quantity=entry_order.get('quantity'),
                price=entry_price  # 使用订单中的价格
            )
            logger.info(f"  限价单开仓成功：订单 ID={entry_result.get('orderId')}")
            logger.info(f"  💰 手续费优化：maker 0.02% (原市价单 taker 0.05%)")

            # v6.12: 记录交易到数据库（用于频率控制）- 开仓成功后立即记录
            # 即使后续止损/止盈设置失败，开仓记录也必须保存
            self.frequency_controller.record_trade(
                symbol=symbol,
                trade_time=datetime.now(),
                pnl=Decimal('0'),  # 开仓时盈亏为 0
                direction=signal['开仓方向']
            )

            # 添加延迟：开仓后等待 2 秒再设置止盈止损 (避免 -1015 错误)
            time.sleep(2.0)

            # 步骤 3: 获取持仓数量 (用于止损止盈)
            positions = self.trade_api.get_position_risk(symbol)
            position_qty = abs(Decimal(positions[0]['positionAmt'])) if positions else Decimal('0')
            logger.info(f"  持仓数量：{position_qty}")

            # 添加延迟：查询持仓后等待 1 秒
            time.sleep(1)

            # 步骤 4: 设置止损
            stop_loss_order = orders.get('stop_loss', {})
            if stop_loss_order and position_qty > 0:
                # 更新为实际持仓数量
                stop_loss_order['quantity'] = position_qty
                logger.info(f"  设置止损：{stop_loss_order}")

                stop_result = self.trade_api.place_pm_conditional_order(**stop_loss_order)
                logger.info(f"  止损设置成功：策略 ID={stop_result.get('strategyId')}")

                # 添加延迟：止损设置后等待 1 秒
                time.sleep(1)

            # 步骤 5: 设置止盈
            take_profit_orders = orders.get('take_profits', [])
            for tp_order in take_profit_orders:
                if position_qty > 0:
                    # 更新为实际持仓数量 × 比例
                    tp_order['quantity'] = position_qty * tp_order.get('ratio', Decimal('0.3'))
                    logger.info(f"  设置止盈 ({tp_order.get('level')}): {tp_order}")

                    tp_result = self.trade_api.place_pm_conditional_order(**tp_order)
                    logger.info(f"  止盈设置成功：策略 ID={tp_result.get('strategyId')}")

                    # 添加延迟：每个止盈单之间间隔 1 秒
                    time.sleep(1)

            # 记录执行的交易
            trade_record = {
                'symbol': symbol,
                'direction': signal['开仓方向'],
                'grade': signal['信号等级'],
                'entry_price': Decimal(str(signal['开仓价'])),
                'stop_loss': Decimal(str(signal['止损价'])),
                'margin': Decimal(str(signal['保证金'])),
                'leverage': leverage,
                'position_qty': position_qty,
                'entry_order_id': entry_result.get('orderId'),
                'status': 'executed',
                'timestamp': datetime.now()
            }

            logger.info(f"  ✅ 交易执行完成：{symbol}")
            return trade_record

        except Exception as e:
            logger.error(f"  ❌ 交易执行失败：{symbol} - {str(e)}", exc_info=True)
            return None

    def get_orderbook_data(self, symbol: str, limit: int = 5) -> Optional[Dict]:
        """
        获取订单簿数据（用于限价单价格优化）

        Args:
            symbol: 交易对
            limit: 深度数量

        Returns:
            订单簿数据字典，如果失败则返回 None
        """
        if not self.trade_api:
            return None

        try:
            orderbook_data = self.trade_api.get_orderbook(symbol, limit=limit)
            logger.info(f"  {symbol} 订单簿获取成功")
            return orderbook_data
        except Exception as e:
            logger.warning(f"  {symbol} 订单簿获取失败：{e}，将使用入场价格")
            return None
