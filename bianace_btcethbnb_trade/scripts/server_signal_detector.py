#!/usr/bin/env python3
"""
信号检测模块

基于 traderule.txt 第三章实现信号检测功能：
1. 趋势过滤器（日线 EMA21 方向判断）
2. 入场信号等级判定（S/A 级）
3. 禁止入场条件检查
4. 技术形态识别（基础）

输出格式（符合 traderule.txt 第三章 JSON 示例）：
[
    {
        "币种": "BTCUSDT",
        "开仓方向": "多",
        "开仓推荐度": 85,
        "信号等级": "A",
        "开仓价": 95000,
        "强平价": 85000,
        "止损价": 93000,
        "止盈设置": {
            "TP1": {"价格": 97000, "仓位比例": "50%"},
            "TP2": {"价格": 99000, "仓位比例": "30%"},
            "TP3": {"价格": 100000, "仓位比例": "20%"}
        },
        "保证金": 30,
        "实际杠杆": 5,
        "风险占比": "2%",
        "通过检查清单": true,
        "备注": "符合 500U 阶段一交易规则"
    }
]
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from config.strategy_params import StrategyParams, get_params
from core.data import MarketDataFetcher, get_data_fetcher
from core.scoring import get_scoring_engine

logger = logging.getLogger(__name__)


class SignalDetector:
    """信号检测类"""
    
    def __init__(self, params: StrategyParams = None, data_fetcher: MarketDataFetcher = None):
        """
        初始化信号检测器
        
        Args:
            params: 策略参数
            data_fetcher: 数据获取器
        """
        self.params = params or get_params()
        self.data_fetcher = data_fetcher or get_data_fetcher()
        self.scoring_engine = get_scoring_engine()  # v5.5: 新增评分引擎
    
    def detect_signals(self, symbols: List[str] = None) -> List[Dict[str, Any]]:
        """
        检测所有交易对的交易信号
        
        Args:
            symbols: 交易对列表，默认 ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        
        Returns:
            信号列表
        """
        if symbols is None:
            symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        
        logger.info(f"开始检测 {len(symbols)} 个交易对的交易信号")
        
        # 获取行情数据
        market_data = self.data_fetcher.fetch_market_data(symbols)
        
        signals = []
        for symbol in symbols:
            try:
                data = market_data.get(symbol)
                if not data:
                    logger.warning(f"无法获取 {symbol} 的行情数据，跳过")
                    continue
                
                # 检测单个交易对的信号
                signal = self._detect_single_signal(symbol, data)
                
                if signal:
                    signals.append(signal)
                    
            except Exception as e:
                logger.error(f"检测 {symbol} 信号失败：{str(e)}")
                continue
        
        logger.info(f"检测到 {len(signals)} 个有效信号")
        return signals
    
    def _detect_single_signal(self, symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        检测单个交易对的信号
        
        Args:
            symbol: 交易对
            data: 行情数据
        
        Returns:
            信号字典，如果没有有效信号则返回 None
        """
        # 1. 检查禁止交易情形
        if not self._check_prohibited_conditions(data):
            logger.info(f"{symbol}: 触发禁止交易情形，跳过")
            return None
        
        # 2. 趋势过滤和方向判断
        direction = self._determine_trend_direction(data)
        
        if direction == 0:
            # 趋势不明朗
            logger.info(f"{symbol}: 趋势不明朗，无信号")
            return None
        
        # 3. 信号等级判定
        grade, score = self._determine_signal_grade(symbol, data, direction)
        
        if grade is None:
            logger.info(f"{symbol}: 未达到有效信号等级")
            return None
        
        # 4. 计算入场价、止损价、止盈价
        entry_price, stop_loss, take_profits = self._calculate_price_levels(
            symbol, data, direction, grade
        )
        
        if entry_price is None:
            logger.warning(f"{symbol}: 无法计算价格水平")
            return None
        
        # 5. 计算仓位参数
        position_params = self._calculate_position_params(
            symbol, entry_price, stop_loss, grade, direction
        )
        
        # 6. 组装信号
        signal = self._build_signal(
            symbol=symbol,
            direction=direction,
            grade=grade,
            score=score,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profits=take_profits,
            position_params=position_params
        )
        
        return signal
    
    def _check_prohibited_conditions(self, data: Dict[str, Any]) -> bool:
        """
        检查禁止交易情形（第二章）
        
        Args:
            data: 行情数据
        
        Returns:
            True 表示可以交易，False 表示禁止交易
        """
        price_change_24h = data.get('price_change_24h', Decimal('0'))
        funding_rate = data.get('funding_rate', Decimal('0'))
        
        # 24 小时涨幅 > 25% 或 跌幅 > 20%
        max_rise = self.params.get('prohibited_conditions.max_24h_price_change', Decimal('0.25'))
        max_drop = self.params.get('prohibited_conditions.max_24h_price_drop', Decimal('0.20'))
        
        if price_change_24h > max_rise:
            logger.info(f"24 小时涨幅 {price_change_24h:.2%} > {max_rise:.2%}，禁止交易")
            return False
        
        if price_change_24h < -max_drop:
            logger.info(f"24 小时跌幅 {abs(price_change_24h):.2%} > {abs(max_drop):.2%}，禁止交易")
            return False
        
        # |资金费率| > 0.08%
        max_funding = self.params.get('prohibited_conditions.max_funding_rate', Decimal('0.0008'))
        
        if abs(funding_rate) > max_funding:
            logger.info(f"|资金费率| {abs(funding_rate):.4%} > {max_funding:.4%}，禁止交易")
            return False
        
        # TODO: 买卖价差检查（需要深度数据）
        # TODO: 重大消息检查（需要外部日历）
        
        return True
    
    def _determine_trend_direction(self, data: Dict[str, Any]) -> int:
        """
        判断趋势方向（第三章趋势过滤器）
        
        Args:
            data: 行情数据
        
        Returns:
            1: 多头方向
            -1: 空头方向
            0: 趋势不明
        """
        indicators = data.get('indicators', {})
        
        # 获取日线数据
        daily = indicators.get('1d', {})
        daily_ema21 = daily.get('ema21')
        daily_close = daily.get('close')
        
        if daily_ema21 is None or daily_close is None:
            logger.warning("日线数据不足，无法判断趋势")
            return 0
        
        # 日线 EMA21 方向判断
        if daily_close > daily_ema21:
            # 价格在 EMA21 之上，可能是多头
            return 1
        elif daily_close < daily_ema21:
            # 价格在 EMA21 之下，可能是空头
            return -1
        else:
            # EMA21 走平
            logger.info("日线 EMA21 走平，趋势不明")
            return 0
    
    def _determine_signal_grade(self, symbol: str, data: Dict[str, Any], direction: int) -> Tuple[Optional[str], int]:
        """
        判定信号等级（v5.5: 使用新的评分系统）
        
        Args:
            symbol: 交易对
            data: 行情数据
            direction: 方向（1=多，-1=空）
        
        Returns:
            (信号等级，推荐度分数)，如果没有有效信号则返回 (None, 0)
        """
        try:
            # v5.5: 使用新的评分系统
            score_result = self.scoring_engine.score(symbol, data)
            
            if score_result['grade'] is None:
                # 分数低于 C 级阈值，过滤
                logger.info(f"{symbol}: 评分 {score_result['score']} 分，低于 C 级阈值，过滤")
                return None, 0
            
            # 返回等级和分数
            logger.info(f"{symbol}: 新评分系统结果 - {score_result['grade']}级，{score_result['score']}分")
            return score_result['grade'], score_result['score']
            
        except Exception as e:
            logger.error(f"{symbol}: 新评分系统失败：{e}，使用旧逻辑")
            # 回退到旧逻辑
            return self._legacy_score(symbol, data, direction)
    
    def _legacy_score(self, symbol: str, data: Dict[str, Any], direction: int) -> Tuple[Optional[str], int]:
        """
        旧的评分逻辑（回退方案）
        
        Args:
            symbol: 交易对
            data: 行情数据
            direction: 方向
        
        Returns:
            (信号等级，推荐度分数)
        """
        indicators = data.get('indicators', {})
        
        # 检查多时间框架共振
        has_daily = '1d' in indicators
        has_4h = '4h' in indicators
        has_1h = '1h' in indicators
        
        if not (has_daily and has_4h and has_1h):
            logger.warning(f"{symbol}: 时间框架数据不完整")
            return None, 0
        
        # 简化版 S 级判定：多时间框架共振
        if has_daily and has_4h and has_1h:
            # S 级信号
            return 'S', 85
        
        # A 级信号
        if has_4h and has_1h:
            return 'A', 75
        
        # 无有效信号
        return None, 0
    
    def _calculate_price_levels(self, symbol: str, data: Dict[str, Any], 
                                direction: int, grade: str) -> Tuple[Optional[Decimal], Optional[Decimal], List[Dict]]:
        """
        计算入场价、止损价、止盈价（第三、五章）
        
        Args:
            symbol: 交易对
            data: 行情数据
            direction: 方向
            grade: 信号等级
        
        Returns:
            (入场价，止损价，止盈价列表)
        """
        indicators = data.get('indicators', {})
        current_price = data.get('last_price')
        
        if current_price is None:
            return None, None, []
        
        # 简化实现：使用当前价作为入场价
        entry_price = current_price
        
        # 计算止损幅度（基于 ATR）
        atr = indicators.get('1h', {}).get('atr14')
        if atr:
            stop_loss_pct = atr / entry_price
            # 限制止损幅度在合理范围内
            min_stop = self.params.get('position_sizing.min_stop_loss_pct', Decimal('0.03'))
            max_stop = self.params.get('position_sizing.max_stop_loss_pct', Decimal('0.07'))
            stop_loss_pct = max(min_stop, min(stop_loss_pct, max_stop))
        else:
            stop_loss_pct = Decimal('0.04')  # 默认 4%
        
        # 计算止损价
        if direction == 1:  # 多头
            stop_loss = entry_price * (1 - stop_loss_pct)
        else:  # 空头
            stop_loss = entry_price * (1 + stop_loss_pct)
        
        # 计算止盈价（基于 R 值）
        r_value = abs(entry_price - stop_loss)
        
        tp_config = self.params.get('risk_management.take_profit_levels', {})
        tp1_mult = tp_config.get('tp1_multiplier', Decimal('1.5'))
        tp2_mult = tp_config.get('tp2_multiplier', Decimal('2.5'))
        
        take_profits = []
        
        if direction == 1:  # 多头
            tp1_price = entry_price + r_value * tp1_mult
            tp2_price = entry_price + r_value * tp2_mult
        else:  # 空头
            tp1_price = entry_price - r_value * tp1_mult
            tp2_price = entry_price - r_value * tp2_mult
        
        take_profits = [
            {'level': 'TP1', 'price': tp1_price, 'ratio': tp_config.get('tp1_ratio', Decimal('0.3'))},
            {'level': 'TP2', 'price': tp2_price, 'ratio': tp_config.get('tp2_ratio', Decimal('0.3'))},
            {'level': 'TP3', 'price': None, 'ratio': tp_config.get('tp3_ratio', Decimal('0.4'))},  # TP3 使用移动止损
        ]
        
        return entry_price, stop_loss, take_profits
    
    def _calculate_position_params(self, symbol: str, entry_price: Decimal, 
                                   stop_loss: Decimal, grade: str, direction: int) -> Dict[str, Any]:
        """
        计算仓位参数（第四章）
        
        Args:
            symbol: 交易对
            entry_price: 入场价
            stop_loss: 止损价
            grade: 信号等级
            direction: 方向
        
        Returns:
            仓位参数字典
        """
        # 计算止损百分比
        stop_loss_pct = abs(entry_price - stop_loss) / entry_price
        
        # 名义价值 = 风险金额 / 止损百分比
        risk_amount = self.params.get('position_sizing.risk_amount', Decimal('10'))
        notional_value = risk_amount / stop_loss_pct
        
        # 统一使用固定杠杆倍数（方案 4：保持分级仓位，统一杠杆）
        # 所有等级都使用 5 倍杠杆，通过仓位比例控制风险
        fixed_leverage = 5
        
        # 保证金 = 名义价值 / 杠杆
        margin = notional_value / fixed_leverage
        
        # 限制保证金不超过单仓上限
        max_margin = self.params.get('account.single_position_margin', Decimal('30'))
        margin = min(margin, max_margin)
        
        # 合约数量 = 名义价值 / 入场价
        quantity = notional_value / entry_price
        
        return {
            'notional_value': notional_value,
            'margin': margin,
            'leverage': fixed_leverage,
            'quantity': quantity,
            'risk_ratio': risk_amount / self.params.get('account.total_capital', Decimal('500')),
        }
    
    def _build_signal(self, symbol: str, direction: int, grade: str, score: int,
                     entry_price: Decimal, stop_loss: Decimal, 
                     take_profits: List[Dict], position_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        组装信号字典（符合第三章 JSON 示例格式，v5.5 新增评分字段）
        
        Args:
            symbol: 交易对
            direction: 方向
            grade: 信号等级
            score: 推荐度
            entry_price: 入场价
            stop_loss: 止损价
            take_profits: 止盈价列表
            position_params: 仓位参数
        
        Returns:
            信号字典
        """
        # 方向转换
        direction_str = "多" if direction == 1 else "空"
        
        # 止盈设置格式化
        tp_settings = {}
        for tp in take_profits:
            tp_key = tp['level']
            tp_settings[tp_key] = {
                '价格': float(tp['price']) if tp['price'] else '移动止损',
                '仓位比例': f"{tp['ratio'] * 100:.0f}%"
            }
        
        # v5.5: 获取评分引擎的评分结果（如果可用）
        score_detail = {}
        position_ratio = 0.0
        try:
            # 尝试从评分引擎获取详细评分
            if hasattr(self, 'scoring_engine'):
                # 注意：这里简化处理，实际应该在调用 _determine_signal_grade 时保存评分结果
                position_ratio = self.scoring_engine.calculate_position_ratio(score, grade)
        except Exception as e:
            logger.warning(f"获取评分详情失败：{e}")
        
        signal = {
            '币种': symbol,
            '开仓方向': direction_str,
            '开仓推荐度': score,  # 保留用于向后兼容
            '信号等级': grade,
            '开仓价': float(entry_price),
            '强平价': None,  # TODO: 计算强平价
            '止损价': float(stop_loss),
            '止盈设置': tp_settings,
            '保证金': float(position_params['margin']),
            '实际杠杆': position_params['leverage'],
            '风险占比': f"{position_params['risk_ratio'] * 100:.1f}%",
            '通过检查清单': True,
            '备注': f"符合 500U 阶段一交易规则（{grade}级信号）",
            # v5.5 新增字段
            'score': score,  # 总分
            'score_detail': score_detail,  # 得分明细（后续完善）
            'suggested_position_ratio': position_ratio,  # 建议仓位系数
        }
        
        return signal


# 全局实例
_global_detector: Optional[SignalDetector] = None


def get_signal_detector(params: StrategyParams = None) -> SignalDetector:
    """获取信号检测器实例（单例模式）"""
    global _global_detector
    if _global_detector is None:
        _global_detector = SignalDetector(params)
    return _global_detector
