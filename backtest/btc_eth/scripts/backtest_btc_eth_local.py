"""
BTC/ETH策略回测脚本（本地CSV版本）
从本地CSV文件读取K线数据，运行策略回测并生成收益报告
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
import structlog

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, config: Dict):
        """
        初始化回测引擎
        
        Args:
            config: 策略配置
        """
        self.config = config
        self.initial_capital = Decimal(str(config['strategy']['risk']['frequency_control']['initial_capital_usdt']))
        self.current_capital = self.initial_capital
        self.highest_capital = self.initial_capital
        self.positions: Dict[str, Dict] = {}
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
        
        # 策略配置
        self.scoring_config = config['strategy']['scoring']
        self.risk_config = config['strategy']['risk']
        self.binance_config = config['binance']
    
    def load_klines_from_csv(self, interval: str) -> pd.DataFrame:
        """
        从CSV文件加载K线数据
        
        Args:
            interval: 时间周期
        
        Returns:
            K线数据DataFrame
        """
        filename = f"btcusdt_{interval}.csv"
        df = pd.read_csv(filename)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df.set_index('open_time', inplace=True)
        df.rename(columns={
            'open_price': 'open',
            'high_price': 'high',
            'low_price': 'low',
            'close_price': 'close'
        }, inplace=True)
        
        logger.info(
            f"加载{interval}数据成功",
            count=len(df),
            start=df.index[0],
            end=df.index[-1]
        )
        
        return df
    
    def run_backtest(
        self,
        klines_1h: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ) -> Dict:
        """
        运行回测
        
        Args:
            klines_1h: 1小时K线
            klines_4h: 4小时K线
            klines_1d: 日线K线
        
        Returns:
            回测结果
        """
        # 计算技术指标
        indicators_1h_dict = TechnicalIndicators.calculate_all(klines_1h)
        indicators_4h_dict = TechnicalIndicators.calculate_all(klines_4h)
        indicators_1d_dict = TechnicalIndicators.calculate_all(klines_1d)
        
        # 将字典转换为DataFrame
        indicators_1h = pd.DataFrame(indicators_1h_dict)
        indicators_4h = pd.DataFrame(indicators_4h_dict)
        indicators_1d = pd.DataFrame(indicators_1d_dict)
        
        # 遍历每个时间点
        for i in range(100, len(klines_1h)):
            current_time = klines_1h.index[i]
            current_price = Decimal(str(klines_1h['close'].iloc[i]))
            
            # 计算评分
            score = self._calculate_score(
                indicators_1h.iloc[:i+1],
                indicators_4h.iloc[:i+1],
                indicators_1d.iloc[:i+1]
            )
            
            # 判断信号等级
            if score >= self.scoring_config['grade_thresholds']['S']:
                grade = 'S'
            elif score >= self.scoring_config['grade_thresholds']['A']:
                grade = 'A'
            elif score >= self.scoring_config['grade_thresholds']['B']:
                grade = 'B'
            elif score >= self.scoring_config['grade_thresholds']['C']:
                grade = 'C'
            else:
                continue
            
            # 判断方向
            direction = self._determine_direction(
                indicators_1h.iloc[:i+1],
                indicators_4h.iloc[:i+1]
            )
            
            # 计算ATR
            atr = Decimal(str(indicators_1h['ATR'].iloc[i]))
            
            # 计算仓位
            position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
            leverage = self.binance_config['leverage'][grade]
            position_size = self.current_capital * position_ratio
            
            # 记录交易
            trade = {
                'entry_time': current_time,
                'entry_price': float(current_price),
                'direction': direction,
                'grade': grade,
                'score': score,
                'position_size': float(position_size),
                'leverage': leverage,
                'atr': float(atr)
            }
            
            self.trades.append(trade)
            
            # 更新权益曲线
            self.equity_curve.append({
                'timestamp': current_time,
                'equity': float(self.current_capital)
            })
        
        return {
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_trades': len(self.trades),
            'trades': self.trades,
            'equity_curve': self.equity_curve
        }
    
    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """计算评分"""
        score = 0.0
        
        # 趋势强度 (40%)
        ma21 = indicators_1h['MA21'].iloc[-1]
        ma55 = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55):
            if ma21 > ma55:
                score += 40
        
        # 形态质量 (35%)
        macd = indicators_1h['MACD'].iloc[-1]
        if pd.notna(macd) and macd > 0:
            score += 35
        
        # 动量背离 (25%)
        rsi = indicators_1h['RSI'].iloc[-1]
        if pd.notna(rsi) and 30 < rsi < 70:
            score += 25
        
        return score
    
    def _determine_direction(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> str:
        """判断方向"""
        long_votes = 0
        short_votes = 0
        
        # 1小时
        ma21 = indicators_1h['MA21'].iloc[-1]
        ma55 = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55):
            if ma21 > ma55:
                long_votes += 1
            else:
                short_votes += 1
        
        # 4小时
        ma21 = indicators_4h['MA21'].iloc[-1]
        ma55 = indicators_4h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55):
            if ma21 > ma55:
                long_votes += 1
            else:
                short_votes += 1
        
        return 'LONG' if long_votes > short_votes else 'SHORT'
    
    def generate_report(self, results: Dict) -> str:
        """生成回测报告"""
        report = f"""# BTC/ETH策略回测报告

## 📊 回测概览

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **初始资金**: {results['initial_capital']:.2f} USDT
- **最终资金**: {results['final_capital']:.2f} USDT
- **总收益率**: {((results['final_capital'] - results['initial_capital']) / results['initial_capital'] * 100):.2f}%
- **总交易次数**: {results['total_trades']}

## 📈 交易统计

### 按等级统计
"""
        
        # 按等级统计
        grade_stats = {}
        for trade in results['trades']:
            grade = trade['grade']
            if grade not in grade_stats:
                grade_stats[grade] = {'count': 0, 'total_size': 0}
            grade_stats[grade]['count'] += 1
            grade_stats[grade]['total_size'] += trade['position_size']
        
        for grade in ['S', 'A', 'B', 'C']:
            if grade in grade_stats:
                stats = grade_stats[grade]
                report += f"- **{grade}级**: {stats['count']}笔，平均仓位 {stats['total_size']/stats['count']:.2f} USDT\n"
        
        report += "\n### 按方向统计\n"
        
        # 按方向统计
        long_count = sum(1 for t in results['trades'] if t['direction'] == 'LONG')
        short_count = sum(1 for t in results['trades'] if t['direction'] == 'SHORT')
        report += f"- **做多**: {long_count}笔\n"
        report += f"- **做空**: {short_count}笔\n"
        
        report += "\n## 📋 交易明细\n\n"
        
        for i, trade in enumerate(results['trades'], 1):
            report += f"""### 交易 #{i}
- **时间**: {trade['entry_time']}
- **方向**: {trade['direction']}
- **等级**: {trade['grade']}级
- **评分**: {trade['score']:.1f}分
- **入场价**: {trade['entry_price']:.2f}
- **仓位**: {trade['position_size']:.2f} USDT
- **杠杆**: {trade['leverage']}x

"""
        
        return report


def main():
    """主函数"""
    import yaml
    
    # 加载配置
    with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 创建回测引擎
    engine = BacktestEngine(config)
    
    logger.info("开始加载K线数据...")
    
    # 加载K线数据
    klines_1h = engine.load_klines_from_csv('1h')
    klines_4h = engine.load_klines_from_csv('4h')
    klines_1d = engine.load_klines_from_csv('1d')
    
    logger.info("开始运行回测...")
    
    # 运行回测
    results = engine.run_backtest(klines_1h, klines_4h, klines_1d)
    
    # 生成报告
    report = engine.generate_report(results)
    
    # 保存报告
    with open('backtest_report.md', 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info("回测完成，报告已保存到 backtest_report.md")
    print(report)


if __name__ == "__main__":
    main()
