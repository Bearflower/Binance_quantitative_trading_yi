#!/usr/bin/env python3
"""
市场分析模块

功能：
1. 获取行情数据
2. 检测交易信号
3. 生成订单参数
4. 执行风险检查
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional
from core.data import get_data_fetcher
from core.binance_data_fetcher import get_binance_data_fetcher
from core.signal.detector import get_signal_detector
from core.position_calculator import get_position_calculator
from core.risk_manager import get_risk_manager
from core.order_generator import get_order_generator, generate_all_orders
from core.emergency_handler import get_emergency_handler, check_extreme_market, is_trading_allowed
from config.strategy_params import get_params
from config.settings import SUPPORTED_CURRENCIES

logger = logging.getLogger(__name__)


class MarketAnalyzer:
    """市场分析类"""

    def __init__(self, data_source: str = 'kline_service'):
        """
        初始化市场分析器

        Args:
            data_source: 数据源类型 ('kline_service' 或 'binance_api')
        """
        self.data_source = data_source
        self.params = get_params()

        # 根据数据源选择对应的数据获取器
        if data_source == 'binance_api':
            self.data_fetcher = get_binance_data_fetcher()
            logger.info(f"数据源：币安 API")
        else:
            self.data_fetcher = get_data_fetcher()
            logger.info(f"数据源：K 线服务")

        self.signal_detector = get_signal_detector(self.params)
        self.position_calculator = get_position_calculator(self.params)
        self.risk_manager = get_risk_manager(self.params)
        self.order_generator = get_order_generator(self.params)
        self.emergency_handler = get_emergency_handler(self.params)

    def analyze_market(self, trade_api=None) -> Dict[str, Any]:
        """
        执行市场分析

        Args:
            trade_api: 交易 API 实例（用于获取订单簿数据）

        Returns:
            分析结果字典
        """
        logger.info("=" * 60)
        logger.info("开始执行市场分析")
        logger.info(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"数据源：{self.data_source}")
        logger.info("=" * 60)

        result = {
            'success': False,
            'timestamp': datetime.now(),
            'signals': [],
            'risk_report': None,
            'message': ''
        }

        try:
            # 步骤 1: 获取行情数据
            logger.info("步骤 1: 获取行情数据...")
            market_data = self.data_fetcher.fetch_market_data(SUPPORTED_CURRENCIES)
            logger.info(f"成功获取 {len(market_data)} 个交易对的行情数据")

            # 步骤 2: 应急检查（极端行情）
            logger.info("步骤 2: 应急检查...")
            emergency_status = self.emergency_handler.get_emergency_status()
            trading_allowed, halt_reason = is_trading_allowed()

            if not trading_allowed:
                logger.warning(f"⛔ 停止交易：{halt_reason}")
                result['success'] = True
                result['message'] = f'停止交易：{halt_reason}'
                return result

            # 检查极端行情
            for symbol, data in market_data.items():
                # data_fetcher 已将 priceChangePercent 转换为 price_change_24h（除以 100）
                # emergency_handler 期望百分比值（如 5.0 表示 5%），所以需要乘以 100
                price_change_24h = data.get('price_change_24h', Decimal('0'))
                price_change = price_change_24h * Decimal('100')  # 转换回百分比格式
                if check_extreme_market(symbol, price_change):
                    logger.warning(f"⚠️ {symbol} 极端行情，跳过该交易对")

            # 步骤 3: 检测交易信号
            logger.info("步骤 3: 检测交易信号...")
            signals = self.signal_detector.detect_signals(SUPPORTED_CURRENCIES)
            result['signals'] = signals

            if not signals:
                logger.info("未检测到有效交易信号")
                result['success'] = True
                result['message'] = '未检测到有效交易信号'
                return result

            logger.info(f"检测到 {len(signals)} 个有效信号:")
            for signal in signals:
                logger.info(f"  - {signal['币种']} {signal['开仓方向']} "
                          f"等级:{signal['信号等级']} 推荐度:{signal['开仓推荐度']}")

            # 步骤 4: 生成订单参数
            logger.info("步骤 4: 生成订单参数...")
            self._generate_order_params(signals, trade_api)

            # 步骤 5: 风险检查
            logger.info("步骤 5: 执行风险检查...")
            risk_report = self._generate_risk_report()
            result['risk_report'] = risk_report

            # 标记执行成功
            result['success'] = True
            result['message'] = f"检测到 {len(signals)} 个信号"

        except Exception as e:
            logger.error(f"分析执行失败：{str(e)}", exc_info=True)
            result['message'] = f'执行失败：{str(e)}'
            result['success'] = False

        logger.info("=" * 60)
        logger.info(f"分析完成：{result['message']}")
        logger.info("=" * 60)

        return result

    def _generate_order_params(self, signals: List[Dict[str, Any]], trade_api=None):
        """
        生成订单参数

        Args:
            signals: 信号列表
            trade_api: 交易 API 实例（用于获取订单簿数据）
        """
        for signal in signals:
            # 计算仓位
            position = self.position_calculator.calculate_position(
                symbol=signal['币种'],
                entry_price=Decimal(str(signal['开仓价'])),
                stop_loss_price=Decimal(str(signal['止损价'])),
                direction=1 if signal['开仓方向'] == '多' else -1,
                signal_grade=signal['信号等级']
            )

            # 生成订单模板
            order_template = self.order_generator.generate_order_template(
                symbol=signal['币种'],
                direction=1 if signal['开仓方向'] == '多' else -1,
                entry_price=Decimal(str(signal['开仓价'])),
                stop_loss_price=Decimal(str(signal['止损价'])),
                signal_grade=signal['信号等级'],
                position_data=position
            )

            # 格式化订单（获取 API 精度）
            if trade_api:
                tick_size, step_size = trade_api.get_symbol_precision(signal['币种'])
                api_precision = {'tick_size': tick_size, 'step_size': step_size}
                formatted_order = self.order_generator.format_order_for_api(
                    order_template, api_precision
                )
            else:
                formatted_order = self.order_generator.format_order_for_api(order_template)

            # 获取当前价格和订单簿数据（用于限价单）
            current_price = Decimal(str(signal['开仓价']))

            # 获取订单簿数据（用于限价单价格优化）
            orderbook_data = None
            if trade_api:
                try:
                    orderbook_data = trade_api.get_orderbook(signal['币种'], limit=5)
                    logger.info(f"  {signal['币种']} 订单簿获取成功")
                except Exception as e:
                    logger.warning(f"  {signal['币种']} 订单簿获取失败：{e}，将使用入场价格")
                    orderbook_data = None

            # 生成所有订单参数（v6.13.2 限价单）
            all_orders = generate_all_orders(
                order_template,
                formatted_order,
                use_limit_order=True,  # 使用限价单
                current_price=current_price,
                orderbook_data=orderbook_data
            )
            signal['orders'] = all_orders

        logger.info(f"订单参数生成完成")

    def _generate_risk_report(self) -> Dict[str, Any]:
        """生成风险报告"""
        # TODO: 从数据库获取当前持仓和账户权益
        # 简化实现：返回空报告
        return {
            'account_equity': Decimal('500'),
            'total_capital': Decimal('500'),
            'total_margin': Decimal('0'),
            'margin_usage': Decimal('0'),
            'risk_level': 'SAFE'
        }
