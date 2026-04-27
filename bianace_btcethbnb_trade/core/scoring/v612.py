#!/usr/bin/env python3
"""
评分引擎 v6.12 - 方案 A 稳健型

核心特点：
1. 提高质量门槛，减少每日交易
2. 完整的6维度评分实现
3. 优化的仓位管理
4. 详细日志输出

Author: Trading System
Version: 6.12.0
"""

import logging
from typing import Dict, Any, Optional, Tuple
from .base import ScoringEngineBase

logger = logging.getLogger(__name__)


class ScoringEngineV612(ScoringEngineBase):
    """
    评分引擎 v6.12 - 方案 A 稳健型

    提高质量门槛，减少每日交易，优化夏普比率
    """

    def __init__(self, config_file: str = 'config/scoring_params.yaml'):
        """
        初始化 v6.12 评分引擎

        Args:
            config_file: 配置文件路径
        """
        super().__init__(config_file)

    def _get_default_config(self) -> Dict[str, Any]:
        """
        获取默认配置

        Returns:
            默认配置字典
        """
        return {
            'scoring': {
                'weights': {
                    'trend': 30,
                    'pattern': 30,
                    'momentum': 20,
                    'risk': 20
                },
                'grade_thresholds': {
                    'S': 75,
                    'A': 60
                },
                'position_ratio': {
                    'base': 0.3,
                    'max': 0.6,
                    'coefficient': 0.3
                },
                'veto': {
                    'max_funding_rate': 0.0008,
                    'max_volatility': 0.06,
                    'max_price_increase': 0.25,
                    'max_price_drop': 0.20
                },
                'parameters': {
                    'ema_slope': {
                        'periods': 5,
                        'strong_threshold': 0.0008,
                        'medium_threshold': 0.0004
                    },
                    'breakthrough': {
                        'lookback_periods': 20,
                        'volume_ratio_threshold': 1.3
                    },
                    'rsi': {
                        'healthy_min': 30,
                        'healthy_max': 70,
                        'extreme_min': 20,
                        'extreme_max': 80
                    },
                    'macd': {
                        'golden_cross_score': 6,
                        'zero_cross_score': 2,
                        'expanding_score': 2,
                        'accelerating_score': 2
                    }
                }
            },
            'symbols': {
                'BTCUSDT': {
                    'high_volatility_threshold': 0.04,
                    'break_through_pct': 0.015
                },
                'ETHUSDT': {
                    'high_volatility_threshold': 0.045,
                    'break_through_pct': 0.015
                },
                'BNBUSDT': {
                    'high_volatility_threshold': 0.07,
                    'break_through_pct': 0.025
                }
            },
            'data_requirements': {
                'min_klines': {
                    '1d': 55,
                    '4h': 20,
                    '1h': 20
                },
                'min_volume_periods': 20
            },
            'performance': {
                'enable_cache': True,
                'cache_ttl_seconds': 5,
                'max_scoring_time_ms': 50
            },
            'logging': {
                'enable_detail': True,
                'log_level': 'INFO'
            }
        }

    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行评分（v6.12 主入口）

        Args:
            symbol: 交易对符号
            data: 市场数据字典

        Returns:
            评分结果字典
        """
        # 1. 一票否决检查
        veto_reason = self._check_veto(data)
        if veto_reason:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': veto_reason
            }

        # 2. 数据完整性
        is_valid, confidence = self._check_data_integrity(data)
        if not is_valid:
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': '数据完整性不足'
            }

        # 3. 市场状态过滤
        market_state = self._check_market_state(data)
        if market_state != 'trending':
            return {
                'symbol': symbol,
                'score': 0.0,
                'grade': None,
                'direction': None,
                'position_ratio': 0.0,
                'veto_reason': f'市场状态：{market_state}'
            }

        # 4. 6 维度评分
        indicators = data.get('indicators', {})

        trend_strength = self._score_trend_strength(indicators)
        trend_consistency = self._score_trend_consistency(indicators)
        pattern = self._score_pattern(indicators)
        volume = self._score_volume(indicators)
        momentum = self._score_momentum(indicators)
        risk = self._score_risk(symbol, data)

        # 5. 加权总分
        total_raw = (
            trend_strength +
            trend_consistency +
            pattern +
            volume +
            momentum +
            risk
        )

        total_score = total_raw * confidence

        # 6. 等级映射
        grade, percentile = self._map_grade(
            total_score,
            self.config['scoring']['grade_thresholds']
        )

        # 7. 方向判断
        direction = self._determine_direction(indicators)

        # 8. 仓位计算
        position_ratio = self._calculate_position_ratio(total_score, grade)

        return {
            'symbol': symbol,
            'score': total_score,
            'grade': grade,
            'direction': direction,
            'position_ratio': position_ratio,
            'percentile': percentile,
            'confidence': confidence,
            'market_state': market_state,
            'breakdown': {
                'trend_strength': trend_strength,
                'trend_consistency': trend_consistency,
                'pattern': pattern,
                'volume': volume,
                'momentum': momentum,
                'risk': risk
            }
        }

    def _check_veto(self, data: Dict[str, Any]) -> Optional[str]:
        """
        一票否决检查

        Args:
            data: 市场数据字典

        Returns:
            否决原因，如果通过则返回None
        """
        veto_config = self.config['scoring'].get('veto', {})

        funding_rate = data.get('funding_rate', 0)
        if abs(funding_rate) > veto_config.get('max_funding_rate', 0.0008):
            return f"资金费率 {abs(funding_rate):.4%} 超限"

        return None

    def _check_market_state(self, data: Dict[str, Any]) -> str:
        """
        市场状态检查（v6.12 放宽版）

        Args:
            data: 市场数据字典

        Returns:
            市场状态
        """
        indicators = data.get('indicators', {})

        logger.info(f"市场状态检查开始")

        # 1. ADX 检查 - 如果数据缺失则跳过
        if '1d' in indicators and 'adx' in indicators['1d']:
            adx = indicators['1d'].get('adx', [0])[-1]
            if not isinstance(adx, (int, float)):
                adx = 0
            if adx > 0 and adx < 15:
                logger.warning(f"ADX 检查失败：ADX={adx:.1f} < 15")
                return f'ADX={adx:.1f} < 15'
            else:
                logger.info(f"ADX 检查通过：ADX={adx:.1f}")
        else:
            logger.info("ADX 检查跳过：数据缺失")

        # 2. 成交量检查 - 如果数据缺失则跳过
        if '1d' in indicators and 'volume' in indicators['1d']:
            volumes = indicators['1d'].get('volume', [])
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-21:-2]) / 19 if len(volumes) >= 21 else sum(volumes[-20:-1]) / 19
                current_vol = volumes[-2] if len(volumes) >= 2 else volumes[-1]
                if avg_vol > 0:
                    ratio = current_vol / avg_vol
                    if ratio < 1.5:
                        logger.warning(f"量比检查失败：量比={ratio:.1f} < 1.5")
                        return f'量比={ratio:.1f} < 1.5'
                    else:
                        logger.info(f"量比检查通过：量比={ratio:.1f}")
            else:
                logger.info("量比检查跳过：数据不足 20 条")
        else:
            logger.info("量比检查跳过：数据缺失")

        # 3. ATR 检查
        if '1d' in indicators:
            atr14_data = indicators['1d'].get('atr14', 0)
            close_data = indicators['1d'].get('close', 1)
            atr = float(atr14_data[-1]) if isinstance(atr14_data, list) else float(atr14_data)
            close = float(close_data[-1]) if isinstance(close_data, list) else float(close_data)
            if close > 0 and atr > 0:
                atr_pct = atr / close
                if atr_pct < 0.015 or atr_pct > 0.06:
                    logger.warning(f"ATR 检查失败：ATR={atr_pct:.2%} 超出范围")
                    return f'ATR={atr_pct:.2%} 超出范围'
                else:
                    logger.info(f"ATR 检查通过：ATR={atr_pct:.2%}")
            else:
                logger.info("ATR 检查跳过：ATR 或 close 为 0")
        else:
            logger.info("ATR 检查跳过：数据缺失")

        logger.info("市场状态检查通过：trending")
        return 'trending'

    def _score_trend_strength(self, indicators: Dict[str, Any]) -> float:
        """
        趋势强度评分（15 分）- 基于 EMA 斜率

        Args:
            indicators: 技术指标字典

        Returns:
            趋势强度得分
        """
        if '1d' not in indicators:
            logger.info(f"  trend_strength: 数据缺失 (1d)")
            return 0.0

        ema21_data = indicators['1d'].get('ema21_list', [])
        if isinstance(ema21_data, list):
            ema21 = [float(e) for e in ema21_data]
        else:
            logger.info(f"  trend_strength: EMA21_LIST 不是列表 (type={type(ema21_data)}), 得分=0")
            return 0.0

        if len(ema21) < 5:
            logger.info(f"  trend_strength: EMA21_LIST 长度不足 5 (len={len(ema21)}), 得分=0")
            return 0.0

        # 计算 EMA 斜率（最近 5 日）
        recent_ema = ema21[-5:]
        ema_change = (recent_ema[-1] - recent_ema[0]) / recent_ema[0] if recent_ema[0] > 0 else 0

        # 斜率评分
        if ema_change > 0.05:
            logger.info(f"  trend_strength: EMA 斜率={ema_change:.2%} > 5%, 得分=15.0")
            return 15.0
        elif ema_change > 0.03:
            logger.info(f"  trend_strength: EMA 斜率={ema_change:.2%} > 3%, 得分=12.0")
            return 12.0
        elif ema_change > 0.01:
            logger.info(f"  trend_strength: EMA 斜率={ema_change:.2%} > 1%, 得分=9.0")
            return 9.0
        elif ema_change > 0:
            logger.info(f"  trend_strength: EMA 斜率={ema_change:.2%} > 0, 得分=6.0")
            return 6.0
        else:
            logger.info(f"  trend_strength: EMA 斜率={ema_change:.2%} <= 0, 得分=3.0")
            return 3.0

    def _score_trend_consistency(self, indicators: Dict[str, Any]) -> float:
        """
        趋势一致性评分（15 分）- 多周期 EMA 方向一致性

        Args:
            indicators: 技术指标字典

        Returns:
            趋势一致性得分
        """
        directions = []
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                logger.info(f"  trend_consistency: {tf} 数据缺失")
                continue
            ema21 = indicators[tf].get('ema21_list', [])
            if len(ema21) >= 2:
                if ema21[-1] > ema21[-2]:
                    directions.append(1)
                    logger.info(f"  trend_consistency: {tf} EMA 上涨 (+1)")
                else:
                    directions.append(-1)
                    logger.info(f"  trend_consistency: {tf} EMA 下跌 (-1)")
            else:
                logger.info(f"  trend_consistency: {tf} EMA21_LIST 长度不足 (len={len(ema21)})")

        if not directions:
            logger.info(f"  trend_consistency: 无有效方向数据，得分=0")
            return 0.0

        # 一致性评分
        if len(directions) == 3 and all(d == 1 for d in directions):
            logger.info(f"  trend_consistency: 3 周期全部上涨，得分=15.0")
            return 15.0
        elif len(directions) >= 2 and sum(directions) > 0:
            logger.info(f"  trend_consistency: 多数上涨 (sum={sum(directions)}), 得分=12.0")
            return 12.0
        elif len(directions) >= 1 and sum(directions) > 0:
            logger.info(f"  trend_consistency: 少数上涨 (sum={sum(directions)}), 得分=9.0")
            return 9.0
        else:
            logger.info(f"  trend_consistency: 全部下跌 (sum={sum(directions)}), 得分=3.0")
            return 3.0

    def _score_pattern(self, indicators: Dict[str, Any]) -> float:
        """
        形态质量评分（30 分）- 基于 RSI 和布林带位置

        Args:
            indicators: 技术指标字典

        Returns:
            形态质量得分
        """
        score = 0.0

        # RSI 评分（15 分）
        if '1d' in indicators:
            rsi = indicators['1d'].get('rsi', 50)
            logger.info(f"  pattern: RSI={rsi:.1f}")
            if 40 <= rsi <= 60:
                logger.info(f"    RSI 健康区间，得分=15.0")
                score += 15.0
            elif 30 <= rsi < 40 or 60 < rsi <= 70:
                logger.info(f"    RSI 温和区间，得分=10.0")
                score += 10.0
            elif rsi < 30 or rsi > 70:
                logger.info(f"    RSI 极端区间，得分=5.0")
                score += 5.0
            else:
                logger.info(f"    RSI 数据异常")
        else:
            logger.info(f"  pattern: 1d 数据缺失，RSI 得分=0")

        # 布林带位置评分（15 分）
        if '1d' in indicators:
            bollinger = indicators['1d'].get('bollinger', {})
            if isinstance(bollinger, dict):
                close_data = indicators['1d'].get('close', 0)
                close = float(close_data[-1]) if isinstance(close_data, list) else float(close_data)
                lower_data = bollinger.get('lower', [])
                upper_data = bollinger.get('upper', [])
                if lower_data and upper_data:
                    lower = float(lower_data[-1])
                    upper = float(upper_data[-1])
                    if lower > 0 and upper > lower:
                        position = (close - lower) / (upper - lower)
                        logger.info(f"    布林带位置={position:.2f} (close={close:.2f}, lower={lower:.2f}, upper={upper:.2f})")
                        if 0.3 <= position <= 0.7:
                            logger.info(f"    布林带中轨附近，得分=15.0")
                            score += 15.0
                        elif 0.1 <= position < 0.3 or 0.7 < position <= 0.9:
                            logger.info(f"    布林带突破边缘，得分=10.0")
                            score += 10.0
                        else:
                            logger.info(f"    布林带极端位置，得分=5.0")
                            score += 5.0
                    else:
                        logger.info(f"    布林带数据异常：lower={lower}, upper={upper}")
                else:
                    logger.info(f"    布林带数据缺失：lower={lower_data}, upper={upper_data}")
            else:
                logger.info(f"    布林带格式错误")
        else:
            logger.info(f"  pattern: 1d 数据缺失，布林带得分=0")

        logger.info(f"  pattern: 总分={min(score, 30.0):.1f}")
        return min(score, 30.0)

    def _score_volume(self, indicators: Dict[str, Any]) -> float:
        """
        成交量评分（10 分）- 基于量比

        Args:
            indicators: 技术指标字典

        Returns:
            成交量得分
        """
        if '1d' not in indicators:
            logger.info(f"  volume: 1d 数据缺失，得分=0")
            return 0.0

        volumes = indicators['1d'].get('volume', [])
        if len(volumes) < 20:
            logger.info(f"  volume: 数据不足 20 条 (len={len(volumes)}), 得分=0")
            return 0.0

        avg_vol = sum(volumes[-20:-1]) / 20 if sum(volumes[-20:-1]) > 0 else 1
        current_vol = volumes[-1]
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0

        logger.info(f"  volume: 量比={volume_ratio:.2f} (current={current_vol:.0f}, avg={avg_vol:.0f})")
        if volume_ratio > 2.0:
            logger.info(f"    量比 > 2.0, 得分=10.0")
            return 10.0
        elif volume_ratio > 1.5:
            logger.info(f"    量比 > 1.5, 得分=8.0")
            return 8.0
        elif volume_ratio > 1.2:
            logger.info(f"    量比 > 1.2, 得分=6.0")
            return 6.0
        elif volume_ratio > 1.0:
            logger.info(f"    量比 > 1.0, 得分=4.0")
            return 4.0
        else:
            logger.info(f"    量比 <= 1.0, 得分=2.0")
            return 2.0

    def _score_momentum(self, indicators: Dict[str, Any]) -> float:
        """
        动量评分（20 分）- 基于 MACD 和价格动量

        Args:
            indicators: 技术指标字典

        Returns:
            动量得分
        """
        score = 0.0

        # MACD 评分（10 分）
        if '1d' in indicators:
            macd_data = indicators['1d'].get('macd', {})
            if isinstance(macd_data, dict):
                macd = macd_data.get('macd', 0)
                signal = macd_data.get('signal', 0)
                logger.info(f"  momentum: MACD={macd:.4f}, signal={signal:.4f}")
                if macd > signal:
                    logger.info(f"    MACD 金叉，得分=10.0")
                    score += 10.0
                elif abs(macd - signal) < 0.01:
                    logger.info(f"    MACD 接近，得分=5.0")
                    score += 5.0
                else:
                    logger.info(f"    MACD 死叉，得分=0")
            else:
                logger.info(f"  momentum: MACD 数据格式错误")
        else:
            logger.info(f"  momentum: 1d 数据缺失，MACD 得分=0")

        # 价格动量评分（10 分）
        if '1d' in indicators:
            close_list = indicators['1d'].get('close_list', [])
            if len(close_list) >= 5:
                momentum_5d = (close_list[-1] - close_list[-5]) / close_list[-5] if close_list[-5] > 0 else 0
                logger.info(f"    价格动量：5 日={momentum_5d:.2%} (close[-1]={close_list[-1]:.2f}, close[-5]={close_list[-5]:.2f})")
                if momentum_5d > 0.05:
                    logger.info(f"    价格动量 > 5%, 得分=10.0")
                    score += 10.0
                elif momentum_5d > 0.02:
                    logger.info(f"    价格动量 > 2%, 得分=7.0")
                    score += 7.0
                elif momentum_5d > 0:
                    logger.info(f"    价格动量 > 0, 得分=4.0")
                    score += 4.0
                else:
                    logger.info(f"    价格动量 <= 0, 得分=0")
            else:
                logger.info(f"    价格动量数据不足 (len={len(close_list)})")
        else:
            logger.info(f"  momentum: 1d 数据缺失，价格动量得分=0")

        logger.info(f"  momentum: 总分={min(score, 20.0):.1f}")
        return min(score, 20.0)

    def _score_risk(self, symbol: str, data: Dict[str, Any]) -> float:
        """
        风险溢价评分（10 分）- 基于波动率和资金费率

        Args:
            symbol: 交易对符号
            data: 市场数据字典

        Returns:
            风险溢价得分
        """
        score = 10.0

        # 波动率风险（5 分）
        if '1d' in data.get('indicators', {}):
            atr_data = data['indicators']['1d'].get('atr14', 0)
            close_data = data['indicators']['1d'].get('close', 1)
            # 处理列表类型
            atr = float(atr_data[-1]) if isinstance(atr_data, list) else float(atr_data)
            close = float(close_data[-1]) if isinstance(close_data, list) else float(close_data)
            if close > 0:
                volatility = atr / close
                logger.info(f"  risk: 波动率={volatility:.2%} (atr={atr:.2f}, close={close:.2f})")
                if volatility > 0.05:
                    logger.info(f"    波动率 > 5%, 扣分=5.0")
                    score -= 5.0
                elif volatility > 0.03:
                    logger.info(f"    波动率 > 3%, 扣分=2.5")
                    score -= 2.5
                else:
                    logger.info(f"    波动率正常，不扣分")
            else:
                logger.info(f"  risk: close=0, 无法计算波动率")
        else:
            logger.info(f"  risk: 1d 数据缺失，波动率扣分=0")

        # 资金费率风险（5 分）
        funding_rate = abs(data.get('funding_rate', 0))
        logger.info(f"  risk: 资金费率={funding_rate:.4f}")
        if funding_rate > 0.001:
            logger.info(f"    资金费率 > 0.1%, 扣分=5.0")
            score -= 5.0
        elif funding_rate > 0.0005:
            logger.info(f"    资金费率 > 0.05%, 扣分=2.5")
            score -= 2.5
        else:
            logger.info(f"    资金费率正常，不扣分")

        logger.info(f"  risk: 总分={max(score, 0.0):.1f}")
        return max(score, 0.0)

    def _calculate_position_ratio(self, score: float, grade: Optional[str]) -> float:
        """
        计算仓位比例（v6.12 优化版 - 提高质量门槛）

        Args:
            score: 总分
            grade: 等级

        Returns:
            仓位比例
        """
        if grade is None:
            return 0.0

        if grade == 'S':
            return min(0.50, 0.45 + (score - 85) / 300)
        elif grade == 'A':
            return min(0.35, 0.30 + (score - 75) / 300)
        elif grade == 'B':
            return min(0.18, 0.15 + (score - 65) / 300)
        elif grade == 'C':
            return min(0.06, 0.05 + (score - 55) / 300)
        else:
            return 0.0
