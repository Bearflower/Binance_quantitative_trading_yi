#!/usr/bin/env python3
"""
币种筛选过滤器
基于多维度指标筛选优质做空标的
"""

from decimal import Decimal
from typing import Dict, List


class CoinFilter:
    """币种筛选器"""
    
    DEFAULT_CONFIG = {
        'min_funding_rate_annual': Decimal('1.0'),
        'max_oi_to_market_cap': Decimal('0.5'),
        'min_unlock_percentage': Decimal('0.10'),
        'max_listing_hours': Decimal('168'),
        'min_win_rate_history': Decimal('0.30'),
        'min_history_trades': 5,
    }
    
    def __init__(self, config: Dict = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        
        # 历史表现记录
        self.symbol_performance = {}
    
    def check_funding_rate(self, funding_rates: List[Dict]) -> Dict:
        """检查资金费率"""
        if not funding_rates:
            return {
                'passed': False,
                'reason': '无资金费率数据',
                'annualized_rate': Decimal('0')
            }
        
        latest_rate = Decimal(funding_rates[-1]['fundingRate'])
        annualized_rate = latest_rate * Decimal('3') * Decimal('365') * Decimal('100')
        
        passed = annualized_rate >= self.config['min_funding_rate_annual']
        
        return {
            'passed': passed,
            'reason': f'年化费率 {float(annualized_rate):.1f}%',
            'annualized_rate': float(annualized_rate)
        }
    
    def check_oi(self, open_interest: Dict) -> Dict:
        """检查 OI 数据"""
        if not open_interest or 'openInterest' not in open_interest:
            return {
                'passed': False,
                'reason': '无 OI 数据',
                'oi_value': Decimal('0')
            }
        
        oi = Decimal(open_interest['openInterest'])
        oi_usd = oi / Decimal('1000000')  # 转换为百万美元
        
        # 简化：假设市值至少为 OI 的 2 倍
        oi_ratio = oi / (oi * Decimal('2')) if oi > 0 else Decimal('999')
        
        passed = oi_ratio <= self.config['max_oi_to_market_cap']
        
        return {
            'passed': passed,
            'reason': f'OI={float(oi_usd):.1f}M',
            'oi_value': float(oi_usd),
            'oi_ratio': float(oi_ratio)
        }
    
    def check_unlock_data(self, unlock_data: Dict) -> Dict:
        """检查解锁数据"""
        if not unlock_data:
            return {
                'passed': True,  # 无解锁数据时不否决
                'reason': '无解锁数据',
                'unlock_percentage': 0
            }
        
        unlock_percentage = Decimal(str(unlock_data.get('unlock_percentage', 0)))
        passed = unlock_percentage >= self.config['min_unlock_percentage']
        
        return {
            'passed': passed,
            'reason': f'解锁比例 {float(unlock_percentage):.1f}%',
            'unlock_percentage': float(unlock_percentage)
        }
    
    def check_listing_time(self, listing_hours: float) -> Dict:
        """检查上线时间"""
        passed = Decimal(str(listing_hours)) <= self.config['max_listing_hours']
        
        return {
            'passed': passed,
            'reason': f'上线 {listing_hours:.1f} 小时',
            'listing_hours': listing_hours
        }
    
    def check_history_performance(self, symbol: str) -> Dict:
        """检查历史表现"""
        if symbol not in self.symbol_performance:
            return {
                'passed': True,  # 无历史记录时不否决
                'reason': '无历史交易记录',
                'win_rate': 0,
                'total_trades': 0
            }
        
        perf = self.symbol_performance[symbol]
        total_trades = perf.get('total_trades', 0)
        
        if total_trades < self.config['min_history_trades']:
            return {
                'passed': True,
                'reason': f'历史交易数不足 ({total_trades}<{self.config["min_history_trades"]})',
                'win_rate': 0,
                'total_trades': total_trades
            }
        
        win_rate = perf.get('wins', 0) / total_trades
        passed = win_rate >= self.config['min_win_rate_history']
        
        return {
            'passed': passed,
            'reason': f'历史胜率 {float(win_rate):.1%}',
            'win_rate': float(win_rate),
            'total_trades': total_trades
        }
    
    def update_performance(self, symbol: str, is_win: bool):
        """更新币种历史表现"""
        if symbol not in self.symbol_performance:
            self.symbol_performance[symbol] = {'total_trades': 0, 'wins': 0}
        
        self.symbol_performance[symbol]['total_trades'] += 1
        if is_win:
            self.symbol_performance[symbol]['wins'] += 1
    
    def full_filter(self, symbol: str, symbol_data: Dict, 
                   funding_rates: List[Dict], listing_hours: float) -> Dict:
        """完整筛选流程"""
        
        # 1. 资金费率检查
        funding_result = self.check_funding_rate(funding_rates)
        
        # 2. OI 检查
        oi_result = self.check_oi(symbol_data.get('open_interest', {}))
        
        # 3. 解锁数据检查
        unlock_result = self.check_unlock_data(symbol_data.get('unlock_data', {}))
        
        # 4. 上线时间检查
        time_result = self.check_listing_time(listing_hours)
        
        # 5. 历史表现检查
        history_result = self.check_history_performance(symbol)
        
        # 综合判断
        all_passed = (
            funding_result['passed'] and
            oi_result['passed'] and
            unlock_result['passed'] and
            time_result['passed'] and
            history_result['passed']
        )
        
        return {
            'passed': all_passed,
            'filters': {
                'funding_rate': funding_result,
                'oi': oi_result,
                'unlock': unlock_result,
                'listing_time': time_result,
                'history': history_result
            }
        }
