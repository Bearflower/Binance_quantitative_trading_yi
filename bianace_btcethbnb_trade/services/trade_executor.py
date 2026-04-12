#!/usr/bin/env python3
"""
交易执行器模块
负责解析 AI 分析结果并执行完整的交易流程

交易流程:
1. 解析 AI 分析结果，提取交易信号
2. 检查账户资金是否充足
3. 如不足，执行资金准备流程 (理财赎回 → 划转)
4. 在统一交易账户下单
5. 设置止盈止损
6. 记录交易数据到数据库
7. 发送飞书通知
"""

import logging
import os
from decimal import Decimal
from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import re

from utils.binance_trade_api import get_trade_api, BinanceTradeAPI, BinanceAPIError
from models.database import get_db_manager, DatabaseManager
from utils.lark_notifier import LarkNotifier

logger = logging.getLogger(__name__)


class TradeSignal:
    """交易信号类"""
    
    def __init__(self, signal_data: Dict[str, Any]):
        """
        初始化交易信号
        
        Args:
            signal_data: 信号数据字典
        """
        self.symbol = signal_data.get('symbol', '')
        self.side = signal_data.get('side', '')  # BUY/SELL
        self.position_side = signal_data.get('position_side', 'BOTH')  # LONG/SHORT/BOTH
        self.action = signal_data.get('action', '')  # OPEN/CLOSE
        self.quantity = Decimal(signal_data.get('quantity', '0'))
        self.price = Decimal(signal_data.get('price', '0')) if signal_data.get('price') else None
        self.tp_price = Decimal(signal_data.get('tp_price', '0')) if signal_data.get('tp_price') else None
        self.sl_price = Decimal(signal_data.get('sl_price', '0')) if signal_data.get('sl_price') else None
        self.leverage = signal_data.get('leverage', 20)
        self.margin_ratio = Decimal(signal_data.get('margin_ratio', '0.06'))  # 默认 6%
        
        # 验证信号有效性
        self._validate()
    
    def _validate(self):
        """验证信号有效性"""
        if not self.symbol:
            raise ValueError("交易对不能为空")
        
        if self.side not in ['BUY', 'SELL']:
            raise ValueError(f"无效的方向：{self.side}")
        
        if self.action not in ['OPEN', 'CLOSE']:
            raise ValueError(f"无效的动作：{self.action}")
        
        if self.quantity <= 0:
            raise ValueError("数量必须大于 0")
    
    def __str__(self):
        return (f"交易信号：{self.symbol} {self.side} {self.position_side}, "
                f"数量：{self.quantity}, 价格：{self.price}, "
                f"止盈：{self.tp_price}, 止损：{self.sl_price}")


class TradeExecutor:
    """交易执行器类"""
    
    def __init__(self):
        """初始化交易执行器"""
        self.api: BinanceTradeAPI = get_trade_api()
        self.db: DatabaseManager = get_db_manager()
        
        # 飞书通知
        lark_webhook = os.getenv('LARK_WEBHOOK_URL')
        self.notifier = LarkNotifier(lark_webhook) if lark_webhook else None
        
        # 交易配置
        self.default_leverage = int(os.getenv('LEVERAGE', '20'))
        self.max_positions = int(os.getenv('MAX_POSITIONS', '2'))
        self.single_position_margin = Decimal(os.getenv('SINGLE_POSITION_MARGIN', '30'))
        
        logger.info("交易执行器初始化完成")
    
    def parse_analysis_result(self, analysis_text: str) -> List[TradeSignal]:
        """
        解析 AI 分析结果，提取交易信号
        
        Args:
            analysis_text: AI 分析结果文本
        
        Returns:
            交易信号列表
        """
        signals = []
        
        try:
            # 尝试从文本中提取 JSON 格式的信号
            # 支持多种可能的格式
            
            # 格式 1: 直接 JSON
            json_match = re.search(r'\{[^{}]*"symbol"[^{}]*\}', analysis_text, re.DOTALL)
            if json_match:
                signal_data = json.loads(json_match.group())
                signals.append(TradeSignal(signal_data))
                return signals
            
            # 格式 2: 从固定格式提取
            # 示例：开仓 BTCUSDT 多单，数量 0.001, 价格 50000, 止盈 52000, 止损 49000
            pattern = r'(开仓 | 平仓)\s*(\w+)\s*(多单 | 空单).*?数量\s*[:：]?\s*([\d.]+).*?价格\s*[:：]?\s*([\d.]+)'
            matches = re.findall(pattern, analysis_text)
            
            for match in matches:
                action_str, symbol, position_type, quantity, price = match
                
                # 解析动作
                action = 'OPEN' if '开仓' in action_str else 'CLOSE'
                
                # 解析方向
                side = 'BUY' if '多单' in position_type else 'SELL'
                position_side = 'LONG' if '多单' in position_type else 'SHORT'
                
                signal_data = {
                    'symbol': symbol.upper(),
                    'side': side,
                    'position_side': position_side,
                    'action': action,
                    'quantity': quantity,
                    'price': price
                }
                
                # 尝试提取止盈止损
                tp_match = re.search(r'止盈\s*[:：]?\s*([\d.]+)', analysis_text)
                sl_match = re.search(r'止损\s*[:：]?\s*([\d.]+)', analysis_text)
                
                if tp_match:
                    signal_data['tp_price'] = tp_match.group(1)
                if sl_match:
                    signal_data['sl_price'] = sl_match.group(1)
                
                try:
                    signals.append(TradeSignal(signal_data))
                except ValueError as e:
                    logger.warning(f"信号解析失败：{e}")
            
        except Exception as e:
            logger.error(f"解析分析结果失败：{str(e)}", exc_info=True)
        
        return signals
    
    async def execute_signal(self, signal: TradeSignal) -> Dict[str, Any]:
        """
        执行交易信号
        
        Args:
            signal: 交易信号
        
        Returns:
            执行结果
        """
        logger.info(f"开始执行交易信号：{signal}")
        
        try:
            # 1. 检查当前持仓
            current_position = self.api.get_position(signal.symbol, signal.position_side)
            
            if signal.action == 'CLOSE':
                # 平仓操作
                if not current_position or Decimal(current_position['positionAmt']) == 0:
                    logger.warning(f"无持仓可平：{signal.symbol} {signal.position_side}")
                    return {'success': False, 'message': '无持仓可平'}
                
                # 平仓数量不能超过持仓
                position_amt = abs(Decimal(current_position['positionAmt']))
                close_quantity = min(signal.quantity, position_amt)
                
                if close_quantity <= 0:
                    logger.warning("平仓数量为 0")
                    return {'success': False, 'message': '平仓数量为 0'}
                
                signal.quantity = close_quantity
            
            # 2. 准备资金 (开仓时需要)
            if signal.action == 'OPEN':
                required_margin = self.api.calculate_required_margin(
                    signal.quantity, 
                    signal.price or Decimal(self.api.get_ticker_price(signal.symbol)),
                    signal.leverage
                )
                
                logger.info(f"需要保证金：{required_margin} USDT")
                await self.prepare_funds(required_margin)
            
            # 3. 下单
            order_params = {
                'symbol': signal.symbol,
                'side': signal.side,
                'position_side': signal.position_side,
                'quantity': signal.quantity,
                'price': signal.price,
                'reduce_only': (signal.action == 'CLOSE')
            }
            
            if signal.price:
                # 限价单
                order = self.api.place_limit_order(**order_params)
            else:
                # 市价单
                order = self.api.place_market_order(**order_params)
            
            logger.info(f"下单成功：订单 ID={order['orderId']}")
            
            # 4. 设置止盈止损
            tp_order_id = None
            sl_order_id = None
            
            if signal.tp_price or signal.sl_price:
                try:
                    # 设置止盈
                    if signal.tp_price:
                        tp_order = self.api.place_take_profit_market_order(
                            symbol=signal.symbol,
                            side='SELL' if signal.side == 'BUY' else 'BUY',
                            position_side=signal.position_side,
                            quantity=signal.quantity,
                            stop_price=signal.tp_price,
                            reduce_only=True
                        )
                        tp_order_id = tp_order['orderId']
                        logger.info(f"止盈单设置成功：订单 ID={tp_order_id}")
                    
                    # 设置止损
                    if signal.sl_price:
                        sl_order = self.api.place_stop_market_order(
                            symbol=signal.symbol,
                            side='SELL' if signal.side == 'BUY' else 'BUY',
                            position_side=signal.position_side,
                            quantity=signal.quantity,
                            stop_price=signal.sl_price,
                            reduce_only=True
                        )
                        sl_order_id = sl_order['orderId']
                        logger.info(f"止损单设置成功：订单 ID={sl_order_id}")
                
                except Exception as e:
                    logger.error(f"设置止盈止损失败：{str(e)}")
            
            # 5. 记录到数据库
            self.db.save_trade(
                order_data=order,
                tp_price=signal.tp_price,
                sl_price=signal.sl_price
            )
            
            # 6. 发送通知
            if self.notifier:
                self._send_trade_notification(order, signal)
            
            return {
                'success': True,
                'order': order,
                'tp_order_id': tp_order_id,
                'sl_order_id': sl_order_id
            }
        
        except BinanceAPIError as e:
            logger.error(f"API 错误：{e.code} - {e.msg}")
            return {'success': False, 'message': f'API 错误：{e.msg}'}
        
        except Exception as e:
            logger.error(f"执行失败：{str(e)}", exc_info=True)
            return {'success': False, 'message': f'执行失败：{str(e)}'}
    
    async def prepare_funds(self, required_amount: Decimal):
        """
        准备交易所需资金
        
        流程:
        1. 检查 U 本位合约账户余额
        2. 如果不足，检查现货账户余额
        3. 如果仍不足，检查理财账户余额并执行赎回
        
        Args:
            required_amount: 需要的金额
        """
        logger.info(f"准备资金：需要 {required_amount} USDT")
        
        # 1. 检查 U 本位合约账户余额
        umfut_balance = self.api.get_umfut_balance('USDT')
        logger.info(f"U 本位合约账户余额：{umfut_balance}")
        
        if umfut_balance >= required_amount:
            logger.info("合约账户资金充足")
            return
        
        shortage = required_amount - umfut_balance
        logger.info(f"资金缺口：{shortage}")
        
        # 2. 检查现货账户余额
        spot_balance = self.api.get_spot_balance('USDT')
        logger.info(f"现货账户余额：{spot_balance}")
        
        if spot_balance >= shortage:
            logger.info("从现货账户划转资金到合约账户")
            transfer_result = self.api.transfer_spot_to_umfut('USDT', shortage)
            
            # 记录划转
            self.db.save_transfer(
                transfer_result,
                remark=f'交易资金准备：现货→合约',
                related_order_id=None
            )
            logger.info("划转完成")
            return
        
        # 3. 检查理财账户余额
        logger.info("现货账户资金不足，检查理财账户")
        earn_positions = self.api.get_simple_earn_flexible_position(asset='USDT')
        
        total_earn_balance = sum(
            Decimal(pos.get('totalAmount', '0')) for pos in earn_positions
        )
        logger.info(f"理财账户余额：{total_earn_balance}")
        
        if total_earn_balance < shortage:
            from utils.binance_trade_api import InsufficientFundsError
            raise InsufficientFundsError(
                f"资金不足：需要 {required_amount}, 可用：合约{umfut_balance} + 现货{spot_balance} + 理财{total_earn_balance}"
            )
        
        # 执行赎回
        logger.info(f"赎回理财产品：{shortage} USDT")
        
        for position in earn_positions:
            if shortage <= 0:
                break
            
            product_id = position['productId']
            available_amount = Decimal(position.get('totalAmount', '0'))
            redeem_amount = min(available_amount, shortage)
            
            redeem_result = self.api.redeem_simple_earn_flexible(
                product_id=product_id,
                amount=redeem_amount,
                redeem_all=(redeem_amount == available_amount)
            )
            
            # 记录赎回
            self.db.save_redemption(
                redeem_result,
                success=True,
                remark=f'交易资金准备：赎回理财'
            )
            
            shortage -= redeem_amount
            logger.info(f"赎回成功：{redeem_amount} USDT")
        
        # 等待赎回完成 (实际生产中需要更长的等待时间)
        import asyncio
        await asyncio.sleep(5)
        
        # 从资金账户划转到现货，再划转到合约
        total_needed = required_amount - umfut_balance
        logger.info(f"从资金账户划转到现货账户：{total_needed}")
        transfer1 = self.api.transfer_funding_to_spot('USDT', total_needed)
        self.db.save_transfer(transfer1, remark='理财赎回资金：资金→现货')
        
        logger.info(f"从现货账户划转到合约账户：{total_needed}")
        transfer2 = self.api.transfer_spot_to_umfut('USDT', total_needed)
        self.db.save_transfer(transfer2, remark='理财赎回资金：现货→合约')
        
        logger.info("资金准备完成")
    
    def _send_trade_notification(self, order: Dict[str, Any], signal: TradeSignal):
        """发送交易通知"""
        if not self.notifier:
            return
        
        message = f"""
📊 **交易执行通知**

交易对：{order['symbol']}
方向：{order['side']} {order['positionSide']}
类型：{order['type']}
数量：{order['origQty']}
价格：{order.get('price', '市价')}
状态：{order['status']}
订单 ID: {order['orderId']}
时间：{datetime.fromtimestamp(order['updateTime']/1000)}

止盈价：{signal.tp_price or '未设置'}
止损价：{signal.sl_price or '未设置'}
        """
        
        self.notifier.send_text_message(message)


# 全局交易执行器实例
_executor: Optional[TradeExecutor] = None


def get_trade_executor() -> TradeExecutor:
    """获取全局交易执行器实例"""
    global _executor
    if _executor is None:
        _executor = TradeExecutor()
    return _executor


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("交易执行器测试")
    print("=" * 60)
    
    executor = get_trade_executor()
    
    # 测试解析分析结果
    print("\n1. 测试解析 AI 分析结果...")
    test_analysis = """
    ### 开仓建议
    开仓 BTCUSDT 多单，数量 0.001, 价格 50000, 止盈 52000, 止损 49000
    """
    signals = executor.parse_analysis_result(test_analysis)
    print(f"解析到 {len(signals)} 个信号")
    for signal in signals:
        print(f"  - {signal}")
    
    print("\n" + "=" * 60)
    print("测试完成!")
    print("=" * 60)
