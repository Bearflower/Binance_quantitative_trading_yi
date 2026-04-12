from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from utils.binance_trade_api import BinanceTradeAPI
from models.database import DatabaseManager

class RiskLevel:
    """风险等级"""
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class RiskEvent:
    """风险事件"""
    def __init__(self, event_type: str, message: str, severity: str, timestamp: str):
        self.event_type = event_type
        self.message = message
        self.severity = severity
        self.timestamp = timestamp

class AccountRisk:
    """账户风险评估"""
    def __init__(self, total_balance: float, total_margin: float, risk_ratio: float, risk_level: str):
        self.total_balance = total_balance
        self.total_margin = total_margin
        self.risk_ratio = risk_ratio
        self.risk_level = risk_level

class RiskReport:
    """风险报告"""
    def __init__(self, account_risk: AccountRisk, position_risks: List[Dict], risk_events: List[RiskEvent]):
        self.account_risk = account_risk
        self.position_risks = position_risks
        self.risk_events = risk_events

class RiskManagement:
    """高级风控系统"""
    def __init__(self):
        self.binance_api = BinanceTradeAPI()
        self.db_manager = DatabaseManager()
        self.risk_levels = {
            "low": 0.2,
            "medium": 0.5,
            "high": 0.8,
            "critical": 0.9
        }
    
    def assess_risk_level(self, position: Dict) -> str:
        """
        评估单个持仓风险等级
        
        Args:
            position: 持仓数据
            
        Returns:
            风险等级
        """
        try:
            # 计算保证金比率
            position_amt = float(position.get('positionAmt', 0))
            if position_amt == 0:
                return RiskLevel.NONE
            
            entry_price = float(position.get('entryPrice', 0))
            mark_price = float(position.get('markPrice', entry_price))
            leverage = float(position.get('leverage', 1))
            
            # 计算未实现盈亏
            unrealized_profit = float(position.get('unrealizedProfit', 0))
            
            # 计算保证金
            initial_margin = float(position.get('initialMargin', 0))
            maint_margin = float(position.get('maintMargin', 0))
            
            # 计算风险比率
            if initial_margin > 0:
                risk_ratio = maint_margin / initial_margin if initial_margin > 0 else 0
            else:
                # 估算保证金
                notional_value = abs(position_amt) * mark_price
                estimated_margin = notional_value / leverage
                if estimated_margin > 0:
                    risk_ratio = (estimated_margin + unrealized_profit) / estimated_margin if unrealized_profit < 0 else 0
                else:
                    risk_ratio = 0
            
            # 确定风险等级
            if risk_ratio >= self.risk_levels["critical"]:
                return RiskLevel.CRITICAL
            elif risk_ratio >= self.risk_levels["high"]:
                return RiskLevel.HIGH
            elif risk_ratio >= self.risk_levels["medium"]:
                return RiskLevel.MEDIUM
            elif risk_ratio >= self.risk_levels["low"]:
                return RiskLevel.LOW
            else:
                return RiskLevel.NONE
        except Exception as e:
            print(f"评估风险等级失败: {e}")
            return RiskLevel.NONE
    
    def dynamic_stop_loss(self, position: Dict, market_volatility: float) -> float:
        """
        根据市场波动调整止损价格
        
        Args:
            position: 持仓数据
            market_volatility: 市场波动率（ATR）
            
        Returns:
            动态止损价格
        """
        try:
            position_amt = float(position.get('positionAmt', 0))
            entry_price = float(position.get('entryPrice', 0))
            
            if position_amt == 0 or entry_price == 0:
                return 0
            
            # 计算止损百分比，根据波动率调整
            base_stop_pct = 0.02  # 基础止损 2%
            volatility_adjustment = min(market_volatility / 100, 0.03)  # 波动率调整，最大 3%
            stop_pct = base_stop_pct + volatility_adjustment
            
            # 根据持仓方向计算止损价格
            if position_amt > 0:  # 多头
                stop_price = entry_price * (1 - stop_pct)
            else:  # 空头
                stop_price = entry_price * (1 + stop_pct)
            
            return stop_price
        except Exception as e:
            print(f"计算动态止损失败: {e}")
            return 0
    
    def assess_account_risk(self) -> AccountRisk:
        """
        评估账户整体风险
        
        Returns:
            账户风险评估
        """
        try:
            # 获取账户信息
            account_info = self.binance_api.get_account_info()
            
            # 计算总余额和总保证金
            total_balance = 0
            total_margin = 0
            
            if 'balances' in account_info:
                for balance in account_info['balances']:
                    if balance['asset'] == 'USDT':
                        total_balance = float(balance.get('walletBalance', 0))
                        break
            
            # 获取所有持仓
            positions = self.binance_api.get_um_position_risk()
            for position in positions:
                position_amt = float(position.get('positionAmt', 0))
                if position_amt != 0:
                    total_margin += float(position.get('initialMargin', 0))
            
            # 计算风险比率
            risk_ratio = total_margin / total_balance if total_balance > 0 else 0
            
            # 确定风险等级
            if risk_ratio >= self.risk_levels["critical"]:
                risk_level = RiskLevel.CRITICAL
            elif risk_ratio >= self.risk_levels["high"]:
                risk_level = RiskLevel.HIGH
            elif risk_ratio >= self.risk_levels["medium"]:
                risk_level = RiskLevel.MEDIUM
            elif risk_ratio >= self.risk_levels["low"]:
                risk_level = RiskLevel.LOW
            else:
                risk_level = RiskLevel.NONE
            
            return AccountRisk(total_balance, total_margin, risk_ratio, risk_level)
        except Exception as e:
            print(f"评估账户风险失败: {e}")
            return AccountRisk(0, 0, 0, RiskLevel.NONE)
    
    def generate_risk_report(self) -> RiskReport:
        """
        生成风险报告
        
        Returns:
            风险报告
        """
        try:
            # 评估账户风险
            account_risk = self.assess_account_risk()
            
            # 评估每个持仓的风险
            positions = self.binance_api.get_um_position_risk()
            position_risks = []
            
            for position in positions:
                position_amt = float(position.get('positionAmt', 0))
                if position_amt != 0:
                    risk_level = self.assess_risk_level(position)
                    position_risks.append({
                        'symbol': position.get('symbol'),
                        'position_side': 'LONG' if position_amt > 0 else 'SHORT',
                        'position_amt': position_amt,
                        'entry_price': float(position.get('entryPrice', 0)),
                        'mark_price': float(position.get('markPrice', 0)),
                        'unrealized_profit': float(position.get('unrealizedProfit', 0)),
                        'leverage': float(position.get('leverage', 1)),
                        'risk_level': risk_level
                    })
            
            # 获取风险事件
            risk_events = self.get_risk_events(7)  # 最近 7 天的风险事件
            
            return RiskReport(account_risk, position_risks, risk_events)
        except Exception as e:
            print(f"生成风险报告失败: {e}")
            return RiskReport(AccountRisk(0, 0, 0, RiskLevel.NONE), [], [])
    
    def get_risk_events(self, days: int = 7) -> List[RiskEvent]:
        """
        获取风险事件
        
        Args:
            days: 天数
            
        Returns:
            风险事件列表
        """
        try:
            # 使用 DatabaseManager 查询
            query = """
                SELECT event_type, message, severity, timestamp 
                FROM risk_events 
                WHERE timestamp >= %s 
                ORDER BY timestamp DESC
            """
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
            results = self.db_manager._execute_query(query, (start_date,))
            
            result = []
            for event in results:
                result.append(RiskEvent(
                    event_type=event['event_type'],
                    message=event['message'],
                    severity=event['severity'],
                    timestamp=event['timestamp']
                ))
            
            return result
        except Exception as e:
            print(f"获取风险事件失败：{e}")
            return []
    
    def record_risk_event(self, event_type: str, message: str, severity: str) -> None:
        """
        记录风险事件
        
        Args:
            event_type: 事件类型
            message: 事件消息
            severity: 严重程度
        """
        try:
            conn = sqlite3.connect('database/trading.db')
            cursor = conn.cursor()
            
            # 确保风险事件表存在
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                message TEXT,
                severity TEXT,
                timestamp TEXT
            )
            ''')
            
            # 插入风险事件
            cursor.execute('''
            INSERT INTO risk_events (event_type, message, severity, timestamp)
            VALUES (?, ?, ?, ?)
            ''', [event_type, message, severity, datetime.now().isoformat()])
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"记录风险事件失败: {e}")
    
    def check_position_risk(self, position: Dict) -> Tuple[str, Optional[str]]:
        """
        检查持仓风险
        
        Args:
            position: 持仓数据
            
        Returns:
            (风险等级, 建议操作)
        """
        risk_level = self.assess_risk_level(position)
        
        if risk_level == RiskLevel.CRITICAL:
            return risk_level, "立即平仓或追加保证金"
        elif risk_level == RiskLevel.HIGH:
            return risk_level, "考虑平仓或追加保证金"
        elif risk_level == RiskLevel.MEDIUM:
            return risk_level, "密切关注"
        else:
            return risk_level, None
    
    def adjust_stop_loss(self, position: Dict, current_price: float) -> float:
        """
        调整止损价格
        
        Args:
            position: 持仓数据
            current_price: 当前价格
            
        Returns:
            新的止损价格
        """
        try:
            position_amt = float(position.get('positionAmt', 0))
            entry_price = float(position.get('entryPrice', 0))
            
            if position_amt == 0:
                return 0
            
            # 计算当前盈利
            if position_amt > 0:  # 多头
                profit_pct = (current_price - entry_price) / entry_price
            else:  # 空头
                profit_pct = (entry_price - current_price) / entry_price
            
            # 根据盈利调整止损
            if profit_pct > 0.05:  # 盈利超过 5%
                # 移动止损到成本价附近
                if position_amt > 0:
                    stop_price = entry_price * 1.01  # 成本价上方 1%
                else:
                    stop_price = entry_price * 0.99  # 成本价下方 1%
            else:
                # 使用动态止损
                # 这里简化处理，实际应该计算市场波动率
                stop_price = self.dynamic_stop_loss(position, 0.02)  # 假设波动率为 2%
            
            return stop_price
        except Exception as e:
            print(f"调整止损失败: {e}")
            return 0
    
    def get_risk_summary(self) -> Dict:
        """
        获取风险摘要
        
        Returns:
            风险摘要
        """
        try:
            report = self.generate_risk_report()
            
            # 统计高风险持仓数量
            high_risk_positions = len([p for p in report.position_risks if p['risk_level'] in [RiskLevel.HIGH, RiskLevel.CRITICAL]])
            
            # 统计风险事件数量
            high_severity_events = len([e for e in report.risk_events if e.severity in ['high', 'critical']])
            
            return {
                'account_risk_level': report.account_risk.risk_level,
                'total_balance': report.account_risk.total_balance,
                'total_margin': report.account_risk.total_margin,
                'risk_ratio': report.account_risk.risk_ratio,
                'total_positions': len(report.position_risks),
                'high_risk_positions': high_risk_positions,
                'high_severity_events': high_severity_events,
                'last_updated': datetime.now().isoformat()
            }
        except Exception as e:
            print(f"获取风险摘要失败: {e}")
            return {
                'account_risk_level': RiskLevel.NONE,
                'total_balance': 0,
                'total_margin': 0,
                'risk_ratio': 0,
                'total_positions': 0,
                'high_risk_positions': 0,
                'high_severity_events': 0,
                'last_updated': datetime.now().isoformat()
            }
