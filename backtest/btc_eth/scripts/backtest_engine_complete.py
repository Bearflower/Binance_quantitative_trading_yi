"""
BTC/ETH策略完整回测引擎
包含开仓、平仓、止盈止损、盈亏计算等完整功能
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
import structlog

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


class Position:
    """持仓状态"""
    def __init__(self):
        self.entry_time = None
        self.entry_price = Decimal('0')
        self.direction = None  # 'LONG' or 'SHORT'
        self.quantity = Decimal('0')
        self.position_size = Decimal('0')
        self.leverage = 1
        self.grade = 'C'
        self.atr = Decimal('0')
        self.tp1_price = Decimal('0')
        self.tp2_price = Decimal('0')
        self.stop_loss = Decimal('0')
        self.highest_price = Decimal('0')  # 用于吊灯止损
        self.tp1_hit = False
        self.tp2_hit = False


class BacktestEngine:
    """完整回测引擎"""
    
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
        
        self.positions: List[Position] = []  # 支持多个持仓
        self.trades: List[Dict] = []  # 已完成交易
        self.equity_curve: List[Dict] = []  # 权益曲线
        
        # 策略配置
        self.scoring_config = config['strategy']['scoring']
        self.risk_config = config['strategy']['risk']
        self.binance_config = config['binance']
        
        # 止盈止损配置
        self.tp1_atr_multiplier = 2.5
        self.tp2_atr_multiplier = 4.0
        self.initial_stop_atr = 2.5
        self.trailing_stop_atr = 1.2
    
    def load_klines_from_csv(self, interval: str) -> pd.DataFrame:
        """
        从CSV文件加载K线数据
        
        Args:
            interval: 时间周期
        
        Returns:
            K线数据DataFrame
        """
        import os
        
        # 获取数据文件路径
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(script_dir, f"../data/btcusdt_{interval}.csv")
        
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
        运行完整回测
        
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
            current_high = Decimal(str(klines_1h['high'].iloc[i]))
            current_low = Decimal(str(klines_1h['low'].iloc[i]))
            
            # 1. 检查持仓并执行平仓
            for position in self.positions[:]:  # 使用切片创建副本，避免在迭代时修改列表
                self._check_and_close_position(
                    position, current_time, current_price, current_high, current_low
                )
            
            # 2. 检查是否可以开仓（不再限制持仓数量）
            self._check_and_open_position(
                current_time, current_price,
                indicators_1h.iloc[:i+1],
                indicators_4h.iloc[:i+1],
                indicators_1d.iloc[:i+1]
            )
            
            # 3. 更新权益曲线
            equity = float(self.current_capital)
            for position in self.positions:
                # 计算浮动盈亏
                if position.direction == 'LONG':
                    unrealized_pnl = (current_price - position.entry_price) * position.quantity
                else:
                    unrealized_pnl = (position.entry_price - current_price) * position.quantity
                equity += float(unrealized_pnl)
            
            self.equity_curve.append({
                'timestamp': current_time,
                'equity': equity
            })
        
        # 强制平仓最后的持仓
        for position in self.positions[:]:
            self._force_close_position(position, klines_1h.index[-1], klines_1h['close'].iloc[-1])
        
        return {
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_return': float((self.current_capital - self.initial_capital) / self.initial_capital * 100),
            'total_trades': len(self.trades),
            'win_trades': sum(1 for t in self.trades if t['pnl'] > 0),
            'loss_trades': sum(1 for t in self.trades if t['pnl'] <= 0),
            'trades': self.trades,
            'equity_curve': self.equity_curve
        }
    
    def _check_and_open_position(
        self,
        current_time,
        current_price: Decimal,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ):
        """检查并开仓"""
        # ========== 前置过滤器检查 ==========
        # 1. ADX趋势强度 >= 15
        adx = indicators_1d['ADX'].iloc[-1]
        if pd.isna(adx) or adx < 15:
            return
        
        # 2. 成交量放大 >= 1.2（无上限）
        volume = indicators_1h['Volume'].iloc[-1] if 'Volume' in indicators_1h.columns else 0
        volume_ma = indicators_1h['Volume_MA'].iloc[-1] if 'Volume_MA' in indicators_1h.columns else 1
        volume_ratio = volume / volume_ma if volume_ma > 0 else 0
        if volume_ratio < 1.2:
            return
        
        # 3. ATR%范围 1.0%-8.0%
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        atr_percent = float(atr / current_price * 100)
        if atr_percent < 1.0 or atr_percent > 8.0:
            return
        
        # 计算评分
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)
        
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
            return
        
        # 判断方向
        direction = self._determine_direction(indicators_1h, indicators_4h)
        
        # 计算ATR
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        
        # 计算仓位
        position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
        leverage = self.binance_config['leverage'][grade]
        position_size = self.current_capital * position_ratio
        
        # 计算数量
        quantity = position_size / current_price
        
        # 创建持仓
        position = Position()
        position.entry_time = current_time
        position.entry_price = current_price
        position.direction = direction
        position.quantity = quantity
        position.position_size = position_size
        position.leverage = leverage
        position.grade = grade
        position.atr = atr
        position.highest_price = current_price
        
        # 计算止盈止损价格
        if direction == 'LONG':
            position.tp1_price = current_price + atr * Decimal(str(self.tp1_atr_multiplier))
            position.tp2_price = current_price + atr * Decimal(str(self.tp2_atr_multiplier))
            position.stop_loss = current_price - atr * Decimal(str(self.initial_stop_atr))
        else:
            position.tp1_price = current_price - atr * Decimal(str(self.tp1_atr_multiplier))
            position.tp2_price = current_price - atr * Decimal(str(self.tp2_atr_multiplier))
            position.stop_loss = current_price + atr * Decimal(str(self.initial_stop_atr))
        
        # 添加到持仓列表
        self.positions.append(position)
        
        logger.info(
            f"开仓: {direction} {grade}级 评分{score:.1f}",
            entry_price=float(current_price),
            position_size=float(position_size),
            tp1=float(position.tp1_price),
            tp2=float(position.tp2_price),
            stop_loss=float(position.stop_loss)
        )
    
    def _check_and_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal,
        current_high: Decimal,
        current_low: Decimal
    ):
        """检查并平仓"""
        if not position:
            return
        
        close_reason = None
        close_price = None
        
        # 1. 检查止盈
        if position.direction == 'LONG':
            if not position.tp1_hit and current_high >= position.tp1_price:
                # TP1止盈
                position.tp1_hit = True
                # 平仓25%
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.tp1_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
                logger.info(f"TP1止盈: 价格{float(position.tp1_price):.2f}, 盈亏{float(pnl):.2f}")
                
            if not position.tp2_hit and current_high >= position.tp2_price:
                # TP2止盈
                position.tp2_hit = True
                # 平仓25%
                close_quantity = position.quantity * Decimal('0.5')  # 剩余50%的一半
                pnl = (position.tp2_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
                logger.info(f"TP2止盈: 价格{float(position.tp2_price):.2f}, 盈亏{float(pnl):.2f}")
        else:
            if not position.tp1_hit and current_low <= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.entry_price - position.tp1_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
                logger.info(f"TP1止盈: 价格{float(position.tp1_price):.2f}, 盈亏{float(pnl):.2f}")
                
            if not position.tp2_hit and current_low <= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * Decimal('0.5')
                pnl = (position.entry_price - position.tp2_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
                logger.info(f"TP2止盈: 价格{float(position.tp2_price):.2f}, 盈亏{float(pnl):.2f}")
        
        # 2. 检查吊灯止损
        if position.direction == 'LONG':
            position.highest_price = max(position.highest_price, current_high)
            trailing_stop = position.highest_price - position.atr * Decimal('1.2')
            if current_low <= trailing_stop:
                close_reason = "吊灯止损"
                close_price = trailing_stop
        else:
            position.highest_price = min(position.highest_price, current_low)
            trailing_stop = position.highest_price + position.atr * Decimal('1.2')
            if current_high >= trailing_stop:
                close_reason = "吊灯止损"
                close_price = trailing_stop
        
        # 3. 检查时间止损
        holding_hours = (current_time - position.entry_time).total_seconds() / 3600
        if holding_hours >= 72 and not position.tp1_hit:
            close_reason = "时间止损"
            close_price = current_price
        
        # 4. 执行平仓
        if close_reason:
            self._close_position(position, current_time, close_price, close_reason)
    
    def _close_position(self, position: Position, current_time, close_price: Decimal, reason: str):
        """执行平仓"""
        if not position:
            return
        
        # 计算盈亏
        if position.direction == 'LONG':
            pnl = (close_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - close_price) * position.quantity
        
        # 更新资金
        self.current_capital += pnl
        
        # 记录交易
        trade = {
            'entry_time': position.entry_time,
            'entry_price': float(position.entry_price),
            'exit_time': current_time,
            'exit_price': float(close_price),
            'direction': position.direction,
            'grade': position.grade,
            'position_size': float(position.position_size),
            'leverage': position.leverage,
            'pnl': float(pnl),
            'pnl_percent': float(pnl / position.position_size * 100),
            'close_reason': reason
        }
        
        self.trades.append(trade)
        
        logger.info(
            f"平仓: {reason}",
            exit_price=float(close_price),
            pnl=float(pnl),
            current_capital=float(self.current_capital)
        )
        
        # 从持仓列表中移除
        if position in self.positions:
            self.positions.remove(position)
    
    def _force_close_position(self, position: Position, current_time, close_price):
        """强制平仓"""
        if position:
            self._close_position(position, current_time, Decimal(str(close_price)), "回测结束")
    
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
        win_rate = results['win_trades'] / results['total_trades'] * 100 if results['total_trades'] > 0 else 0
        
        report = f"""# BTC/ETH策略完整回测报告

## 📊 回测概览

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **初始资金**: {results['initial_capital']:.2f} USDT
- **最终资金**: {results['final_capital']:.2f} USDT
- **总收益率**: {results['total_return']:.2f}%
- **总交易次数**: {results['total_trades']}
- **盈利次数**: {results['win_trades']}
- **亏损次数**: {results['loss_trades']}
- **胜率**: {win_rate:.2f}%

## 📈 交易统计

### 按等级统计
"""
        
        # 按等级统计
        grade_stats = {}
        for trade in results['trades']:
            grade = trade['grade']
            if grade not in grade_stats:
                grade_stats[grade] = {'count': 0, 'win': 0, 'total_pnl': 0}
            grade_stats[grade]['count'] += 1
            if trade['pnl'] > 0:
                grade_stats[grade]['win'] += 1
            grade_stats[grade]['total_pnl'] += trade['pnl']
        
        for grade in ['S', 'A', 'B', 'C']:
            if grade in grade_stats:
                stats = grade_stats[grade]
                grade_win_rate = stats['win'] / stats['count'] * 100
                report += f"- **{grade}级**: {stats['count']}笔，胜率{grade_win_rate:.1f}%，总盈亏{stats['total_pnl']:.2f}U\n"
        
        report += "\n### 按平仓原因统计\n"
        
        # 按平仓原因统计
        reason_stats = {}
        for trade in results['trades']:
            reason = trade['close_reason']
            if reason not in reason_stats:
                reason_stats[reason] = {'count': 0, 'win': 0, 'total_pnl': 0}
            reason_stats[reason]['count'] += 1
            if trade['pnl'] > 0:
                reason_stats[reason]['win'] += 1
            reason_stats[reason]['total_pnl'] += trade['pnl']
        
        for reason, stats in reason_stats.items():
            reason_win_rate = stats['win'] / stats['count'] * 100
            report += f"- **{reason}**: {stats['count']}笔，胜率{reason_win_rate:.1f}%，总盈亏{stats['total_pnl']:.2f}U\n"
        
        report += "\n## 📋 交易明细\n\n"
        
        for i, trade in enumerate(results['trades'], 1):
            pnl_emoji = "✅" if trade['pnl'] > 0 else "❌"
            report += f"""### 交易 #{i} {pnl_emoji}
- **时间**: {trade['entry_time']} → {trade['exit_time']}
- **方向**: {trade['direction']}
- **等级**: {trade['grade']}级
- **入场价**: {trade['entry_price']:.2f}
- **出场价**: {trade['exit_price']:.2f}
- **仓位**: {trade['position_size']:.2f} USDT
- **杠杆**: {trade['leverage']}x
- **盈亏**: {trade['pnl']:.2f} USDT ({trade['pnl_percent']:.2f}%)
- **平仓原因**: {trade['close_reason']}

"""
        
        return report


def main():
    """主函数"""
    import yaml
    import os
    
    # 获取项目根目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    
    # 加载配置
    config_path = os.path.join(project_root, 'strategies/btc_eth/config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 创建回测引擎
    engine = BacktestEngine(config)
    
    logger.info("开始加载K线数据...")
    
    # 加载K线数据
    klines_1h = engine.load_klines_from_csv('1h')
    klines_4h = engine.load_klines_from_csv('4h')
    klines_1d = engine.load_klines_from_csv('1d')
    
    logger.info("开始运行完整回测...")
    
    # 运行回测
    results = engine.run_backtest(klines_1h, klines_4h, klines_1d)
    
    # 生成报告
    report = engine.generate_report(results)
    
    # 保存报告
    report_path = os.path.join(script_dir, '../reports/完整回测报告.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info("回测完成，报告已保存到 完整回测报告.md")
    print(report)


if __name__ == "__main__":
    main()
