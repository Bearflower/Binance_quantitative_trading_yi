"""
BTC/ETH策略回测脚本
从服务器数据库获取K线数据，运行策略回测并生成收益报告
"""
import asyncio
import asyncpg
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
import structlog

from shared.indicators import TechnicalIndicators
from strategies.btc_eth.strategy import BTCEthStrategy, FrequencyController, PositionState

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
        self.positions: Dict[str, PositionState] = {}
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
        
        # 初始化频率控制器
        self.frequency_controller = FrequencyController(
            config['strategy']['risk']['frequency_control']
        )
        
        # 策略配置
        self.scoring_config = config['strategy']['scoring']
        self.risk_config = config['strategy']['risk']
        self.binance_config = config['binance']
    
    async def fetch_klines_from_db(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime
    ) -> pd.DataFrame:
        """
        从数据库获取K线数据
        
        Args:
            symbol: 交易对
            interval: 时间周期
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            K线数据DataFrame
        """
        conn = await asyncpg.connect(
            host="172.19.0.3",  # common_service_postgres容器IP
            port=5432,
            user="binance",
            password="test_password_123456",
            database="binance_data",
            timeout=30.0
        )
        
        try:
            table_name = f"kline_{symbol.lower()}_{interval}"
            
            query = f"""
                SELECT 
                    open_time as timestamp,
                    open_price as open,
                    high_price as high,
                    low_price as low,
                    close_price as close,
                    volume
                FROM {table_name}
                WHERE open_time >= $1 AND open_time <= $2
                ORDER BY open_time ASC
            """
            
            rows = await conn.fetch(query, start_time, end_time)
            
            if not rows:
                logger.warning(f"未找到数据: {symbol} {interval}")
                return pd.DataFrame()
            
            df = pd.DataFrame([dict(row) for row in rows])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            
            # 转换数据类型
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            
            logger.info(
                f"获取{symbol} {interval}数据成功",
                count=len(df),
                start=df.index[0],
                end=df.index[-1]
            )
            
            return df
            
        finally:
            await conn.close()
    
    def calculate_signals(
        self,
        klines_1h: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ) -> List[Dict]:
        """
        计算交易信号
        
        Args:
            klines_1h: 1小时K线
            klines_4h: 4小时K线
            klines_1d: 日线K线
        
        Returns:
            交易信号列表
        """
        signals = []
        
        # 计算技术指标
        indicators_1h = TechnicalIndicators.calculate_all(klines_1h)
        indicators_4h = TechnicalIndicators.calculate_all(klines_4h)
        indicators_1d = TechnicalIndicators.calculate_all(klines_1d)
        
        # 遍历每个时间点
        for i in range(100, len(klines_1h)):  # 从第100根K线开始，确保有足够的历史数据
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
                continue  # 评分不足，跳过
            
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
            position_size = self.current_capital * position_ratio * Decimal(str(leverage))
            
            # 生成信号
            signal = {
                'timestamp': current_time,
                'price': current_price,
                'direction': direction,
                'grade': grade,
                'score': score,
                'atr': atr,
                'position_size': position_size,
                'leverage': leverage
            }
            
            signals.append(signal)
        
        return signals
    
    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """计算评分"""
        # 简化版评分计算
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
    
    def execute_backtest(self, signals: List[Dict]) -> Dict:
        """
        执行回测
        
        Args:
            signals: 交易信号列表
        
        Returns:
            回测结果
        """
        for signal in signals:
            # 模拟开仓
            position = PositionState()
            position.entry_price = signal['price']
            position.entry_time = signal['timestamp']
            position.direction = signal['direction']
            position.initial_quantity = signal['position_size'] / signal['price']
            position.current_quantity = position.initial_quantity
            position.atr = signal['atr']
            
            # 记录交易
            trade = {
                'entry_time': signal['timestamp'],
                'entry_price': float(signal['price']),
                'direction': signal['direction'],
                'grade': signal['grade'],
                'score': signal['score'],
                'position_size': float(signal['position_size']),
                'leverage': signal['leverage']
            }
            
            self.trades.append(trade)
        
        # 生成权益曲线
        self.equity_curve.append({
            'timestamp': datetime.now(),
            'equity': float(self.current_capital)
        })
        
        return {
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_trades': len(self.trades),
            'trades': self.trades
        }
    
    def generate_report(self, results: Dict) -> str:
        """生成回测报告"""
        report = f"""
# BTC/ETH策略回测报告

## 📊 回测概览

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **初始资金**: {results['initial_capital']:.2f} USDT
- **最终资金**: {results['final_capital']:.2f} USDT
- **总收益率**: {((results['final_capital'] - results['initial_capital']) / results['initial_capital'] * 100):.2f}%
- **总交易次数**: {results['total_trades']}

## 📈 交易明细

"""
        
        for i, trade in enumerate(results['trades'], 1):
            report += f"""
### 交易 #{i}
- **时间**: {trade['entry_time']}
- **方向**: {trade['direction']}
- **等级**: {trade['grade']}级
- **评分**: {trade['score']:.1f}分
- **入场价**: {trade['entry_price']:.2f}
- **仓位**: {trade['position_size']:.2f} USDT
- **杠杆**: {trade['leverage']}x

"""
        
        return report


async def main():
    """主函数"""
    import yaml
    
    # 加载配置
    with open('strategies/btc_eth/config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 创建回测引擎
    engine = BacktestEngine(config)
    
    # 获取数据时间范围
    end_time = datetime.now()
    start_time = end_time - timedelta(days=180)  # 半年数据
    
    logger.info("开始获取K线数据...")
    
    # 获取K线数据
    klines_1h = await engine.fetch_klines_from_db('BTCUSDT', '1h', start_time, end_time)
    klines_4h = await engine.fetch_klines_from_db('BTCUSDT', '4h', start_time, end_time)
    klines_1d = await engine.fetch_klines_from_db('BTCUSDT', '1d', start_time, end_time)
    
    if klines_1h.empty or klines_4h.empty or klines_1d.empty:
        logger.error("数据不足，无法进行回测")
        return
    
    logger.info("开始计算交易信号...")
    
    # 计算信号
    signals = engine.calculate_signals(klines_1h, klines_4h, klines_1d)
    
    logger.info(f"生成{len(signals)}个交易信号")
    
    # 执行回测
    results = engine.execute_backtest(signals)
    
    # 生成报告
    report = engine.generate_report(results)
    
    # 保存报告
    with open('backtest_report.md', 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info("回测完成，报告已保存到 backtest_report.md")
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
