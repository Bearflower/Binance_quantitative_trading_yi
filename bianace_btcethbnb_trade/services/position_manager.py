from datetime import datetime, timedelta
from typing import Dict, List, Optional

from utils.binance_trade_api import BinanceTradeAPI
from models.database import DatabaseManager
from services.risk_management import RiskManagement

class PositionManager:
    """智能仓位管理"""
    def __init__(self):
        self.binance_api = BinanceTradeAPI()
        self.db_manager = DatabaseManager()
        self.risk_management = RiskManagement()
        
        # 配置参数
        self.max_position_size = 0.1  # 单笔最大仓位占比
        self.max_total_position = 0.3  # 总仓位最大占比
        self.min_position_size = 0.01  # 最小仓位大小
        self.default_leverage = 5  # 默认杠杆倍数
    
    def calculate_position_size(self, symbol: str, signal_strength: float, account_balance: float) -> float:
        """
        计算最优仓位大小
        
        Args:
            symbol: 交易对
            signal_strength: 信号强度 (0-1)
            account_balance: 账户余额
            
        Returns:
            建议仓位大小 (USDT)
        """
        try:
            # 1. 基于信号强度计算基础仓位
            base_position = account_balance * self.max_position_size * signal_strength
            
            # 2. 考虑账户总风险
            account_risk = self.risk_management.assess_account_risk()
            risk_ratio = account_risk.risk_ratio
            
            # 3. 根据风险调整仓位
            if risk_ratio > 0.5:
                # 风险较高，减少仓位
                base_position *= (1 - (risk_ratio - 0.5))
            
            # 4. 考虑市场波动率
            volatility = self._get_market_volatility(symbol)
            if volatility > 0.02:  # 波动率大于 2%
                # 高波动市场，减少仓位
                base_position *= (1 - (volatility - 0.02))
            
            # 5. 确保仓位在合理范围内
            min_position = account_balance * self.min_position_size
            max_position = account_balance * self.max_position_size
            
            position_size = max(min_position, min(base_position, max_position))
            
            return round(position_size, 2)
        except Exception as e:
            print(f"计算仓位大小失败: {e}")
            # 返回默认仓位
            return round(account_balance * 0.05, 2)
    
    def optimize_asset_allocation(self, available_funds: float) -> Dict[str, float]:
        """
        优化资金分配
        
        Args:
            available_funds: 可用资金
            
        Returns:
            各交易对的资金分配
        """
        try:
            # 1. 获取市场数据
            symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
            market_data = {}
            
            for symbol in symbols:
                klines = self.binance_api.get_klines(symbol, '4h', limit=20)
                if klines:
                    # 计算最近的涨跌幅
                    close_prices = [float(kline[4]) for kline in klines]
                    if len(close_prices) >= 2:
                        change_pct = (close_prices[-1] - close_prices[0]) / close_prices[0]
                        market_data[symbol] = {
                            'change_pct': change_pct,
                            'volatility': self._calculate_volatility(close_prices)
                        }
            
            # 2. 基于市场表现分配资金
            allocation = {}
            total_score = 0
            scores = {}
            
            for symbol, data in market_data.items():
                # 计算评分：涨跌幅 + 波动率调整
                score = data['change_pct'] - data['volatility'] * 0.5
                scores[symbol] = max(0, score)  # 只考虑正评分
                total_score += scores[symbol]
            
            # 3. 分配资金
            if total_score > 0:
                for symbol, score in scores.items():
                    allocation[symbol] = available_funds * (score / total_score)
            else:
                # 平均分配
                for symbol in symbols:
                    allocation[symbol] = available_funds / len(symbols)
            
            # 4. 确保最小分配
            min_allocation = available_funds * 0.1
            for symbol in allocation:
                allocation[symbol] = max(min_allocation, allocation[symbol])
            
            # 5. 四舍五入
            for symbol in allocation:
                allocation[symbol] = round(allocation[symbol], 2)
            
            return allocation
        except Exception as e:
            print(f"优化资金分配失败: {e}")
            # 平均分配
            symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
            allocation = {}
            for symbol in symbols:
                allocation[symbol] = round(available_funds / len(symbols), 2)
            return allocation
    
    def recommend_leverage(self, symbol: str, market_volatility: Optional[float] = None) -> int:
        """
        推荐杠杆倍数
        
        Args:
            symbol: 交易对
            market_volatility: 市场波动率（可选）
            
        Returns:
            推荐杠杆倍数
        """
        try:
            # 如果没有提供波动率，计算一下
            if market_volatility is None:
                market_volatility = self._get_market_volatility(symbol)
            
            # 基于波动率推荐杠杆
            if market_volatility < 0.01:
                # 低波动，高杠杆
                return 10
            elif market_volatility < 0.02:
                # 中等波动，中等杠杆
                return 5
            elif market_volatility < 0.03:
                # 高波动，低杠杆
                return 3
            else:
                # 极高波动，极低杠杆
                return 2
        except Exception as e:
            print(f"推荐杠杆失败: {e}")
            return self.default_leverage
    
    def balance_positions(self) -> None:
        """
        平衡多币种仓位
        """
        try:
            # 1. 获取当前持仓
            positions = self.binance_api.get_um_position_risk()
            current_positions = {}
            total_position_value = 0
            
            for position in positions:
                position_amt = float(position.get('positionAmt', 0))
                if position_amt != 0:
                    symbol = position.get('symbol')
                    mark_price = float(position.get('markPrice', 0))
                    position_value = abs(position_amt) * mark_price
                    current_positions[symbol] = position_value
                    total_position_value += position_value
            
            if total_position_value == 0:
                return
            
            # 2. 获取账户余额
            account_info = self.binance_api.get_account_info()
            total_balance = 0
            if 'balances' in account_info:
                for balance in account_info['balances']:
                    if balance['asset'] == 'USDT':
                        total_balance = float(balance.get('walletBalance', 0))
                        break
            
            # 3. 计算目标仓位分配
            target_allocation = self.optimize_asset_allocation(total_balance * self.max_total_position)
            
            # 4. 调整仓位
            for symbol, target_value in target_allocation.items():
                current_value = current_positions.get(symbol, 0)
                diff = target_value - current_value
                
                if abs(diff) > total_balance * 0.01:  # 差异大于 1%
                    print(f"需要调整 {symbol} 仓位: 当前 {current_value}, 目标 {target_value}, 差异 {diff}")
                    # 这里可以添加实际的仓位调整逻辑
                    # 例如：平仓 excess 或开新仓
        except Exception as e:
            print(f"平衡仓位失败: {e}")
    
    def get_position_summary(self) -> Dict:
        """
        获取仓位摘要
        
        Returns:
            仓位摘要
        """
        try:
            # 1. 获取当前持仓
            positions = self.binance_api.get_um_position_risk()
            position_list = []
            total_position_value = 0
            total_unrealized_profit = 0
            
            for position in positions:
                position_amt = float(position.get('positionAmt', 0))
                if position_amt != 0:
                    symbol = position.get('symbol')
                    mark_price = float(position.get('markPrice', 0))
                    entry_price = float(position.get('entryPrice', 0))
                    unrealized_profit = float(position.get('unrealizedProfit', 0))
                    leverage = float(position.get('leverage', 1))
                    
                    position_value = abs(position_amt) * mark_price
                    total_position_value += position_value
                    total_unrealized_profit += unrealized_profit
                    
                    position_list.append({
                        'symbol': symbol,
                        'position_side': 'LONG' if position_amt > 0 else 'SHORT',
                        'position_amt': position_amt,
                        'entry_price': entry_price,
                        'current_price': mark_price,
                        'position_value': position_value,
                        'unrealized_profit': unrealized_profit,
                        'leverage': leverage
                    })
            
            # 2. 获取账户信息
            account_info = self.binance_api.get_account_info()
            total_balance = 0
            if 'balances' in account_info:
                for balance in account_info['balances']:
                    if balance['asset'] == 'USDT':
                        total_balance = float(balance.get('walletBalance', 0))
                        break
            
            # 3. 计算仓位占比
            position_ratio = total_position_value / total_balance if total_balance > 0 else 0
            
            # 4. 获取推荐仓位
            recommended_allocation = self.optimize_asset_allocation(total_balance * self.max_total_position)
            
            return {
                'total_balance': total_balance,
                'total_position_value': total_position_value,
                'position_ratio': position_ratio,
                'total_unrealized_profit': total_unrealized_profit,
                'positions': position_list,
                'recommended_allocation': recommended_allocation,
                'last_updated': datetime.now().isoformat()
            }
        except Exception as e:
            print(f"获取仓位摘要失败: {e}")
            return {
                'total_balance': 0,
                'total_position_value': 0,
                'position_ratio': 0,
                'total_unrealized_profit': 0,
                'positions': [],
                'recommended_allocation': {},
                'last_updated': datetime.now().isoformat()
            }
    
    def _get_market_volatility(self, symbol: str, period: str = '4h', limit: int = 20) -> float:
        """
        获取市场波动率
        
        Args:
            symbol: 交易对
            period: 时间周期
            limit: 数据点数量
            
        Returns:
            波动率
        """
        try:
            klines = self.binance_api.get_klines(symbol, period, limit=limit)
            if not klines or len(klines) < 2:
                return 0.02  # 默认波动率
            
            close_prices = [float(kline[4]) for kline in klines]
            return self._calculate_volatility(close_prices)
        except Exception as e:
            print(f"获取市场波动率失败: {e}")
            return 0.02  # 默认波动率
    
    def _calculate_volatility(self, prices: List[float]) -> float:
        """
        计算波动率
        
        Args:
            prices: 价格列表
            
        Returns:
            波动率
        """
        if len(prices) < 2:
            return 0.0
        
        # 计算收益率
        returns = []
        for i in range(1, len(prices)):
            ret = (prices[i] - prices[i-1]) / prices[i-1]
            returns.append(ret)
        
        # 计算标准差
        if len(returns) == 0:
            return 0.0
        
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        volatility = variance ** 0.5
        
        return volatility
    
    def get_optimal_leverage(self, symbol: str, account_balance: float, risk_tolerance: str = 'medium') -> int:
        """
        根据风险承受能力获取最优杠杆
        
        Args:
            symbol: 交易对
            account_balance: 账户余额
            risk_tolerance: 风险承受能力 (low/medium/high)
            
        Returns:
            最优杠杆倍数
        """
        try:
            # 获取市场波动率
            volatility = self._get_market_volatility(symbol)
            
            # 基于风险承受能力调整
            risk_multiplier = {
                'low': 0.5,
                'medium': 1.0,
                'high': 1.5
            }.get(risk_tolerance, 1.0)
            
            # 计算基础杠杆
            base_leverage = self.recommend_leverage(symbol, volatility)
            
            # 根据风险承受能力调整
            adjusted_leverage = int(base_leverage * risk_multiplier)
            
            # 确保杠杆在合理范围内
            adjusted_leverage = max(1, min(adjusted_leverage, 10))
            
            return adjusted_leverage
        except Exception as e:
            print(f"获取最优杠杆失败: {e}")
            return self.default_leverage
    
    def calculate_max_position(self, symbol: str, account_balance: float, leverage: int) -> float:
        """
        计算最大仓位
        
        Args:
            symbol: 交易对
            account_balance: 账户余额
            leverage: 杠杆倍数
            
        Returns:
            最大仓位 (USDT)
        """
        try:
            # 基于账户余额和杠杆计算最大仓位
            max_position = account_balance * leverage * 0.9  # 留 10% 安全边际
            
            # 考虑风险承受能力
            risk_factor = 0.3  # 最大风险敞口
            risk_based_position = account_balance * risk_factor * leverage
            
            # 取较小值
            max_position = min(max_position, risk_based_position)
            
            return round(max_position, 2)
        except Exception as e:
            print(f"计算最大仓位失败: {e}")
            return round(account_balance * 0.1, 2)
