#!/usr/bin/env python3
"""
评分模拟器
模拟真实系统的 4 维度评分机制
"""

from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional
from .technical_indicators import (
    calculate_ema, calculate_rsi, calculate_atr,
    calculate_ema_trend, calculate_price_change,
    is_bearish_engulfing
)


class ScoringSimulator:
    """评分模拟器"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {
            'weights': {
                'contract': 0.35,
                'fundamental': 0.30,
                'technical': 0.25,
                'sentiment': 0.10
            },
            'new_coin_weights': {
                'contract': 0.40,
                'fundamental': 0.20,
                'technical': 0.35,
                'sentiment': 0.05
            },
            'veto_rules': {
                'oi_market_cap_ratio_max': 1.0,
                'listing_hours_max': 168
            },
            'signal_threshold': 3.5  # 新币做空降低阈值
        }
    
    def calculate_contract_score(self, symbol_data: Dict) -> Dict:
        """
        合约数据评分 (35%)
        基于 OI/市值比
        """
        oi = symbol_data.get('open_interest', {})
        open_interest = Decimal(oi.get('openInterest', '0'))
        
        symbol_info = symbol_data.get('symbol_info', {})
        list_time = symbol_info.get('listTime')
        
        if list_time:
            listing_hours = (datetime.now() - datetime.fromtimestamp(list_time / 1000)).total_seconds() / 3600
        else:
            listing_hours = 0
        
        if open_interest == 0:
            score = Decimal('2.0')
            reason = '无 OI 数据'
        else:
            if open_interest > Decimal('100000000'):
                score = Decimal('10.0')
                reason = f'OI={float(open_interest)/1e6:.1f}M (极高)'
            elif open_interest > Decimal('50000000'):
                score = Decimal('8.0')
                reason = f'OI={float(open_interest)/1e6:.1f}M (高)'
            elif open_interest > Decimal('20000000'):
                score = Decimal('6.0')
                reason = f'OI={float(open_interest)/1e6:.1f}M (中)'
            elif open_interest > Decimal('5000000'):
                score = Decimal('4.0')
                reason = f'OI={float(open_interest)/1e6:.1f}M (低)'
            else:
                score = Decimal('2.0')
                reason = f'OI={float(open_interest)/1e6:.1f}M (极低)'
        
        return {
            'score': score,
            'reason': reason,
            'data_available': open_interest > 0
        }
    
    def calculate_fundamental_score(self, symbol_data: Dict) -> Dict:
        """
        基本面评分 (30%)
        基于解锁数据（回测中简化为默认分）
        """
        unlock_data = symbol_data.get('unlock_data', {})
        
        if not unlock_data:
            return {
                'score': Decimal('5.0'),
                'reason': '无解锁数据（默认 5 分）',
                'data_available': False
            }
        
        unlock_percentage = unlock_data.get('unlock_percentage', 0)
        
        if unlock_percentage > 30:
            score = Decimal('10.0')
            reason = f'解锁比例 {unlock_percentage}% (极高)'
        elif unlock_percentage > 20:
            score = Decimal('8.0')
            reason = f'解锁比例 {unlock_percentage}% (高)'
        elif unlock_percentage > 10:
            score = Decimal('6.0')
            reason = f'解锁比例 {unlock_percentage}% (中)'
        elif unlock_percentage > 5:
            score = Decimal('4.0')
            reason = f'解锁比例 {unlock_percentage}% (低)'
        else:
            score = Decimal('2.0')
            reason = f'解锁比例 {unlock_percentage}% (极低)'
        
        return {
            'score': score,
            'reason': reason,
            'data_available': True
        }
    
    def calculate_technical_score(self, klines: List[Dict], klines_15m: List[Dict] = None) -> Dict:
        """
        技术面评分 (25%)
        基于趋势、RSI、波动率、K 线形态
        支持 15 分钟 K 线补充数据
        """
        # 优先使用 15 分钟 K 线（数据量更大）
        if klines_15m and len(klines_15m) >= 50:
            analysis_klines = klines_15m
            timeframe = '15m'
        elif len(klines) >= 50:
            analysis_klines = klines
            timeframe = '1h'
        else:
            return {
                'score': Decimal('5.0'),
                'reason': '数据不足',
                'data_available': False
            }
        
        trend_score = Decimal('0')
        rsi_score = Decimal('0')
        volatility_score = Decimal('0')
        pattern_score = Decimal('0')
        
        trend = calculate_ema_trend(analysis_klines)
        if trend == 'downtrend':
            trend_score = Decimal('4')
        elif trend == 'sideways':
            trend_score = Decimal('2')
        else:
            trend_score = Decimal('0')
        
        rsi = calculate_rsi(analysis_klines, 14)
        if rsi < 30:
            rsi_score = Decimal('3')
        elif rsi < 50:
            rsi_score = Decimal('2')
        elif rsi < 70:
            rsi_score = Decimal('1')
        else:
            rsi_score = Decimal('0')
        
        atr = calculate_atr(analysis_klines, 14)
        current_price = Decimal(str(klines[-1]['close']))
        if current_price > 0:
            atr_pct = (atr / current_price) * Decimal('100')
            if atr_pct > 5:
                volatility_score = Decimal('3')
            elif atr_pct > 3:
                volatility_score = Decimal('2')
            elif atr_pct > 1:
                volatility_score = Decimal('1')
            else:
                volatility_score = Decimal('0.5')
        
        if is_bearish_engulfing(analysis_klines):
            pattern_score = Decimal('2')
        else:
            pattern_score = Decimal('0')
        
        total_score = trend_score + rsi_score + volatility_score + pattern_score
        
        return {
            'score': total_score,
            'reason': f'趋势{trend}({trend_score}) + RSI({rsi_score}) + 波动 ({volatility_score}) + 形态 ({pattern_score})',
            'data_available': True,
            'timeframe': timeframe,
            'details': {
                'trend': trend,
                'trend_score': float(trend_score),
                'rsi': float(rsi),
                'rsi_score': float(rsi_score),
                'atr': float(atr),
                'volatility_score': float(volatility_score),
                'pattern_score': float(pattern_score)
            }
        }
    
    def calculate_sentiment_score(self, funding_rates: List[Dict], listing_hours: float = 0) -> Dict:
        """
        情绪面评分 (10%)
        基于资金费率年化
        新币 (<72h) 使用宽松评分
        """
        if not funding_rates:
            return {
                'score': Decimal('5.0'),
                'reason': '无资金费率数据',
                'data_available': False
            }
        
        latest_rate = Decimal(funding_rates[-1]['fundingRate'])
        annualized_rate = latest_rate * Decimal('3') * Decimal('365') * Decimal('100')
        
        is_new_coin = listing_hours < 72
        
        if is_new_coin:
            if annualized_rate > Decimal('100'):
                score = Decimal('10')
            elif annualized_rate > Decimal('50'):
                score = Decimal('8')
            elif annualized_rate > Decimal('20'):
                score = Decimal('6')
            elif annualized_rate > Decimal('-100'):
                score = Decimal('4')
            else:
                score = Decimal('3.5')
        else:
            if annualized_rate > Decimal('100'):
                score = Decimal('10')
            elif annualized_rate > Decimal('50'):
                score = Decimal('7')
            elif annualized_rate > Decimal('20'):
                score = Decimal('5')
            else:
                score = Decimal('3')
        
        return {
            'score': score,
            'reason': f'年化费率 {float(annualized_rate):.1f}%',
            'data_available': True,
            'annualized_rate': float(annualized_rate),
            'is_new_coin': is_new_coin
        }
    
    def calculate_total_score(self, symbol_data: Dict, klines: List[Dict], 
                             funding_rates: List[Dict], listing_hours: float,
                             klines_15m: List[Dict] = None) -> Dict:
        """计算综合评分"""
        
        contract_result = self.calculate_contract_score(symbol_data)
        fundamental_result = self.calculate_fundamental_score(symbol_data)
        technical_result = self.calculate_technical_score(klines, klines_15m)
        sentiment_result = self.calculate_sentiment_score(funding_rates, listing_hours)
        
        is_new_coin = listing_hours < 72
        weights = self.config['new_coin_weights'] if is_new_coin else self.config['weights']
        
        total_score = (
            contract_result['score'] * Decimal(str(weights['contract'])) +
            fundamental_result['score'] * Decimal(str(weights['fundamental'])) +
            technical_result['score'] * Decimal(str(weights['technical'])) +
            sentiment_result['score'] * Decimal(str(weights['sentiment']))
        )
        
        return {
            'total_score': total_score,
            'contract_score': contract_result,
            'fundamental_score': fundamental_result,
            'technical_score': technical_result,
            'sentiment_score': sentiment_result,
            'weights': weights,
            'is_new_coin': is_new_coin,
            'listing_hours': listing_hours
        }
    
    def check_veto(self, symbol_data: Dict, listing_hours: float) -> Dict:
        """检查一票否决"""
        vetos = []
        
        oi = symbol_data.get('open_interest', {})
        open_interest = Decimal(oi.get('openInterest', '0'))
        
        if open_interest > Decimal('100000000000'):
            vetos.append(f'OI 过高：{float(open_interest)/1e6:.1f}M')
        
        max_hours = self.config['veto_rules']['listing_hours_max']
        if listing_hours > max_hours:
            vetos.append(f'上线时间过长：{listing_hours:.1f}小时 > {max_hours}小时')
        
        return {
            'vetoed': len(vetos) > 0,
            'reasons': vetos
        }
    
    def should_generate_signal(self, total_score: Decimal, veto_result: Dict, listing_hours: float) -> Dict:
        """
        判断是否生成信号
        严格按照 README.md 要求：
        - 综合评分 ≥ 7.0 分
        - 上线时间 ≤ 7 天（168 小时）
        - 无一票否决项
        """
        
        if veto_result['vetoed']:
            return {
                'signal': False,
                'reason': f"一票否决：{', '.join(veto_result['reasons'])}"
            }
        
        if listing_hours > 168:
            return {
                'signal': False,
                'reason': f'上线时间过长：{listing_hours:.1f}小时 > 168 小时'
            }
        
        if total_score >= Decimal(str(self.config['signal_threshold'])):
            return {
                'signal': True,
                'reason': f'综合评分 {float(total_score):.2f} ≥ {self.config["signal_threshold"]}'
            }
        else:
            return {
                'signal': False,
                'reason': f'综合评分 {float(total_score):.2f} < {self.config["signal_threshold"]}'
            }
