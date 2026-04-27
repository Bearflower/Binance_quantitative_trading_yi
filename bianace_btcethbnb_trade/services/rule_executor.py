#!/usr/bin/env python3
"""
规则引擎交易执行器

基于 traderule.txt 规则的交易执行模块：
- 集成信号检测、仓位计算、风险管理
- 保持现有 API 调用不变
- 优化的错误处理和重试机制
- 完整的交易流程控制

使用方式:
    from services.rule_executor import RuleTradeExecutor

    executor = RuleTradeExecutor()
    result = executor.execute_signals(signals)

版本: v2.0.0 (重构版 - 使用服务基类)
更新时间: 2026-04-27
"""

from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# 导入新核心模块
from services.base import BaseService, service_method
from core.data import get_data_fetcher
from core.signal import SignalDetector, get_signal_detector
from core.position_calculator import PositionCalculator, get_position_calculator
from core.risk_manager import RiskManager, get_risk_manager
from config.strategy_params import StrategyParams, get_params

# 导入现有 API（保持兼容）
from utils.binance_trade_api import BinanceTradeAPI, get_trade_api, BinanceAPIError


class RuleTradeExecutor(BaseService):
    """
    规则引擎交易执行器

    继承自 BaseService，提供统一的交易执行功能。

    功能：
    1. 信号执行
    2. 仓位计算
    3. 风险管理
    4. 订单管理
    """

    def __init__(self, params: StrategyParams = None, testnet: bool = False, **kwargs):
        """
        初始化交易执行器

        Args:
            params: 策略参数
            testnet: 是否使用测试网
            **kwargs: 传递给 BaseService 的参数
        """
        self.params = params
        self.testnet = testnet
        super().__init__(service_name="RuleTradeExecutor", **kwargs)

    def _initialize(self):
        """
        初始化交易执行器

        加载配置参数，初始化核心组件
        """
        # 初始化策略参数
        if self.params is None:
            self.params = get_params()

        # 初始化核心组件
        self.data_fetcher = get_data_fetcher()
        self.signal_detector = get_signal_detector(self.params)
        self.position_calculator = get_position_calculator(self.params)
        self.risk_manager = get_risk_manager(self.params)

        # 初始化交易 API
        self.trade_api = get_trade_api() if not self.testnet else BinanceTradeAPI(testnet=True)

        # 从配置加载参数
        self.max_positions = self.get_config_value(
            'account.max_positions',
            default=2,
            required=True
        )

        self.total_capital = Decimal(str(self.get_config_value(
            'account.total_capital',
            default=500,
            required=True
        )))

        # 记录初始化信息
        self.log_info("规则引擎交易执行器初始化完成")
        self.log_info(f"  总资金：{self.total_capital}U")
        self.log_info(f"  最大持仓：{self.max_positions}")
        self.log_info(f"  测试网：{self.testnet}")

    @service_method()
    def execute_signals(self, signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        执行交易信号

        Args:
            signals: 信号列表

        Returns:
            执行结果
        """
        self.log_info("=" * 60)
        self.log_info(f"开始执行交易，共 {len(signals)} 个信号")

        result = {
            'success': False,
            'total_signals': len(signals),
            'executed_count': 0,
            'failed_count': 0,
            'skipped_count': 0,
            'trades': [],
            'errors': []
        }

        if not signals:
            self.log_info("没有信号需要执行")
            result['success'] = True
            return result

        # 1. 检查当前持仓
        current_positions = self._get_current_positions()
        available_slots = self.max_positions - len(current_positions)

        self.log_info(f"当前持仓数：{len(current_positions)}")
        self.log_info(f"可用仓位：{available_slots}/{self.max_positions}")

        if available_slots <= 0:
            self.log_warning("已满仓，无法执行新交易")
            result['errors'].append('已满仓，无法执行新交易')
            return result

        # 2. 执行交易
        executed_trades = []
        for i, signal in enumerate(signals[:available_slots]):
            self.log_info(f"\n执行信号 {i+1}/{min(len(signals), available_slots)}")
            self.log_info(f"  交易对：{signal.get('币种')}")
            self.log_info(f"  方向：{signal.get('开仓方向')}")
            self.log_info(f"  等级：{signal.get('信号等级')}")

            try:
                # 执行单个交易
                trade_result = self._execute_single_trade(signal)

                if trade_result['success']:
                    executed_trades.append(trade_result)
                    result['executed_count'] += 1
                    self.log_info(f"✅ 执行成功：{signal.get('币种')}")
                else:
                    result['failed_count'] += 1
                    result['errors'].append(trade_result.get('error', '未知错误'))
                    self.log_error(f"❌ 执行失败：{signal.get('币种')} - {trade_result.get('error')}")

            except Exception as e:
                self.handle_error(e, context={'signal': signal, 'operation': 'execute_signals'})
                result['failed_count'] += 1
                result['errors'].append(f"{signal.get('币种')}: {str(e)}")

        result['trades'] = executed_trades
        result['success'] = result['executed_count'] > 0

        self.log_info("\n" + "=" * 60)
        self.log_info(f"执行完成：成功 {result['executed_count']}/{len(signals)}")
        self.log_info(f"失败 {result['failed_count']}, 跳过 {result['skipped_count']}")
        self.log_info("=" * 60)

        return result

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(BinanceAPIError),
        reraise=True
    )
    def _execute_single_trade(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行单个交易（带重试）

        Args:
            signal: 交易信号

        Returns:
            交易结果
        """
        symbol = signal.get('币种')
        direction = signal.get('开仓方向')
        grade = signal.get('信号等级')

        result = {
            'success': False,
            'symbol': symbol,
            'direction': direction,
            'grade': grade,
            'order_id': None,
            'error': None
        }

        try:
            # 1. 计算仓位参数
            entry_price = Decimal(str(signal.get('开仓价', '0')))
            stop_loss_price = Decimal(str(signal.get('止损价', '0')))
            direction_int = 1 if direction == '多' else -1

            position_params = self.position_calculator.calculate_position(
                symbol=symbol,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                direction=direction_int,
                signal_grade=grade
            )

            self.log_info(f"仓位参数计算完成:")
            self.log_info(f"  名义价值：{position_params['notional_value']:.2f}U")
            self.log_info(f"  保证金：{position_params['margin']:.2f}U")
            self.log_info(f"  杠杆：{position_params['leverage']}x")
            self.log_info(f"  合约数量：{position_params['quantity']:.6f}")

            # 2. 风险检查
            if not self._pre_trade_check(position_params):
                result['error'] = '风险检查未通过'
                return result

            # 3. 执行开仓（v6.13.2 改用限价单）
            side = 'BUY' if direction == '多' else 'SELL'
            quantity = position_params['quantity']

            self.log_info(f"开始下单：{symbol} {side} {quantity} (限价单 - v6.13.2)")

            # v6.13.2: 改用限价单，降低手续费（taker 0.05% → maker 0.02%）
            order = self.trade_api.place_limit_order(
                symbol=symbol,
                side=side,
                position_side='BOTH',
                quantity=quantity,
                price=entry_price  # 使用计算好的开仓价
            )

            if not order or 'orderId' not in order:
                result['error'] = '下单失败'
                return result

            order_id = order['orderId']
            self.log_info(f"✅ 限价单下单成功：订单 ID={order_id}")
            self.log_info(f"💰 手续费优化：maker 0.02% (原市价单 taker 0.05%)")

            # 4. 设置止损止盈
            tp_levels = self.risk_manager.calculate_take_profit_levels(
                entry_price=entry_price,
                direction=direction_int,
                r_value=abs(entry_price - stop_loss_price)
            )

            # 设置止损单
            stop_order = self._place_stop_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                stop_price=stop_loss_price,
                direction=direction_int
            )

            # 设置止盈单
            tp_orders = self._place_take_profit_orders(
                symbol=symbol,
                side=side,
                quantity=quantity,
                tp_levels=tp_levels,
                direction=direction_int
            )

            # 5. 记录交易结果
            result.update({
                'success': True,
                'order_id': order_id,
                'entry_price': float(entry_price),
                'stop_loss': float(stop_loss_price),
                'take_profits': [
                    {'level': tp['level'], 'price': float(tp['price']) if tp['price'] else None}
                    for tp in tp_levels
                ],
                'position': {
                    'margin': float(position_params['margin']),
                    'leverage': position_params['leverage'],
                    'quantity': float(quantity),
                    'notional_value': float(position_params['notional_value'])
                },
                'timestamp': datetime.now().isoformat()
            })

            self.log_info(f"交易执行完成：{symbol} {direction}")

        except BinanceAPIError as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'execute_single_trade'})
            result['error'] = f'API 错误：{str(e)}'
            raise  # 触发重试

        except Exception as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'execute_single_trade'})
            result['error'] = str(e)

        return result

    def _pre_trade_check(self, position_params: Dict[str, Any]) -> bool:
        """
        交易前风险检查

        Args:
            position_params: 仓位参数

        Returns:
            是否通过检查
        """
        # 1. 检查保证金使用率
        current_positions = self._get_current_positions()
        current_margin = sum(
            Decimal(str(pos.get('margin', '0'))) for pos in current_positions
        )

        new_margin = position_params['margin']
        total_margin = current_margin + new_margin

        max_margin_ratio = Decimal(str(self.params.get('account.max_total_margin_ratio', Decimal('0.3'))))
        max_allowed_margin = self.total_capital * max_margin_ratio

        if total_margin > max_allowed_margin:
            self.log_warning(f"保证金超限：{total_margin:.2f}U > {max_allowed_margin:.2f}U")
            return False

        # 2. 检查名义价值
        current_notional = sum(
            Decimal(str(pos.get('notional_value', '0'))) for pos in current_positions
        )

        new_notional = position_params['notional_value']
        total_notional = current_notional + new_notional

        max_total_notional = Decimal(str(self.params.get('position_sizing.max_total_notional', Decimal('4000'))))

        if total_notional > max_total_notional:
            self.log_warning(f"名义价值超限：{total_notional:.2f}U > {max_total_notional:.2f}U")
            return False

        self.log_info("✅ 风险检查通过")
        return True

    def _get_current_positions(self) -> List[Dict[str, Any]]:
        """获取当前持仓"""
        try:
            positions = self.trade_api.get_position_risk()

            # 过滤出有持仓的位置
            active_positions = [
                pos for pos in positions
                if Decimal(str(pos.get('positionAmt', '0'))) != 0
            ]

            return active_positions

        except Exception as e:
            self.handle_error(e, context={'operation': 'get_current_positions'})
            return []

    def _place_stop_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        stop_price: Decimal,
        direction: int
    ) -> Optional[Dict[str, Any]]:
        """
        设置止损单

        Args:
            symbol: 交易对
            side: 方向（BUY/SELL）
            quantity: 数量
            stop_price: 止损价
            direction: 持仓方向

        Returns:
            订单结果
        """
        try:
            # 使用条件单接口设置止损
            strategy_type = 'STOP_MARKET'

            stop_order = self.trade_api.place_pm_conditional_order(
                symbol=symbol,
                side=side,
                position_side='BOTH',
                strategy_type=strategy_type,
                quantity=quantity,
                stop_price=stop_price,
                reduce_only=True
            )

            if stop_order and 'strategyId' in stop_order:
                self.log_info(f"✅ 止损单设置成功：策略 ID={stop_order['strategyId']}")
                return stop_order
            else:
                self.log_warning(f"止损单设置失败：{stop_order}")
                return None

        except Exception as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'place_stop_order'})
            return None

    def _place_take_profit_orders(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        tp_levels: List[Dict[str, Any]],
        direction: int
    ) -> List[Dict[str, Any]]:
        """
        设置止盈单

        Args:
            symbol: 交易对
            side: 方向
            quantity: 数量
            tp_levels: 止盈水平列表
            direction: 持仓方向

        Returns:
            止盈单列表
        """
        tp_orders = []

        for i, tp in enumerate(tp_levels[:2]):  # 只设置 TP1 和 TP2
            if tp['price'] is None:
                continue

            try:
                # 计算该档位的数量
                tp_quantity = quantity * tp['ratio']

                # 设置止盈单
                strategy_type = 'TAKE_PROFIT_MARKET'

                tp_order = self.trade_api.place_pm_conditional_order(
                    symbol=symbol,
                    side=side,
                    position_side='BOTH',
                    strategy_type=strategy_type,
                    quantity=tp_quantity,
                    stop_price=tp['price'],
                    reduce_only=True
                )

                if tp_order and 'strategyId' in tp_order:
                    self.log_info(f"✅ 止盈单 {tp['level']} 设置成功：策略 ID={tp_order['strategyId']}")
                    tp_orders.append(tp_order)

            except Exception as e:
                self.handle_error(e, context={'symbol': symbol, 'tp_level': tp['level'], 'operation': 'place_take_profit_orders'})

        return tp_orders


# 全局实例
_global_executor: Optional[RuleTradeExecutor] = None


def get_rule_executor(params: StrategyParams = None, testnet: bool = False, **kwargs) -> RuleTradeExecutor:
    """
    获取规则引擎执行器实例（单例模式）

    Args:
        params: 策略参数
        testnet: 是否使用测试网
        **kwargs: 传递给 RuleTradeExecutor 的参数

    Returns:
        RuleTradeExecutor 实例
    """
    global _global_executor
    if _global_executor is None:
        _global_executor = RuleTradeExecutor(params, testnet, **kwargs)
    return _global_executor


# 便捷函数
def execute_signals(signals: List[Dict[str, Any]], testnet: bool = False) -> Dict[str, Any]:
    """
    执行信号的便捷函数

    Args:
        signals: 信号列表
        testnet: 是否使用测试网

    Returns:
        执行结果
    """
    return get_rule_executor(testnet=testnet).execute_signals(signals)
