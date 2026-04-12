"""
回测引擎模块
基于 Backtrader 实现策略回测
"""

import backtrader as bt
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Any, Optional, List
import os

from utils.logger import get_logger
from strategy.pattern_detector import PatternDetector
from strategy.scoring import PatternScorer

logger = get_logger()


class PandasDataWithVolume(bt.feeds.PandasData):
    """扩展 Backtrader 数据源，支持成交量"""
    
    # 只使用标准列，不添加额外的 amount 列
    params = (
        ('volume', 5),
        ('openinterest', -1),  # 不使用 openinterest
    )


class PatternStrategy(bt.Strategy):
    """形态策略类"""
    
    params = (
        ('pattern_params', {}),
        ('scoring_weights', {}),
        ('max_positions', 5),
        ('stop_loss_ratio', 0.03),
        ('trailing_stop', 0.05),
        ('min_hold_days', 1),
        ('max_hold_days', 60),
        ('entry_timing', 'next_open'),
    )
    
    def __init__(self):
        self.dataclose = self.datas[0].close
        self.datahigh = self.datas[0].high
        self.datalow = self.datas[0].low
        self.dataopen = self.datas[0].open
        self.datavolume = self.datas[0].volume
        
        self.order = None
        self.buyprice = None
        self.buycomm = None
        
        # 重命名避免与 Backtrader 内部属性冲突
        self.stock_positions = {}
        self.position_info = {}
        
        self.detector = PatternDetector(self.p.pattern_params)
        self.scorer = PatternScorer(self.p.scoring_weights)
        
        self.trade_count = 0
        self.win_count = 0
        self.total_pnl = 0
        
        logger.info("策略初始化完成")
    
    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Submitted, order.Accepted]:
            return
        
        if order.status in [order.Completed]:
            if order.isbuy():
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            else:
                self.trade_count += 1
                pnl = order.executed.price * order.executed.size - self.buyprice * order.executed.size
                pnl -= self.buycomm + order.executed.comm
                self.total_pnl += pnl
                
                if pnl > 0:
                    self.win_count += 1
                
                logger.info(f"交易完成：{'买入' if order.isbuy() else '卖出'} "
                          f"价格：{order.executed.price:.2f} "
                          f"盈亏：{pnl:.2f}")
        
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            logger.warning(f"订单失败：{order.status}")
        
        self.order = None
    
    def notify_trade(self, trade):
        """交易通知"""
        if not trade.isclosed:
            return
        
        logger.info(f"交易关闭：总盈亏 {trade.pnl:.2f}")
    
    def get_dataframe_from_data(self) -> pd.DataFrame:
        """从 Backtrader 数据获取 DataFrame"""
        size = len(self.datas[0])
        
        df = pd.DataFrame({
            'date': pd.date_range(end=datetime.now(), periods=size),
            'open': self.datas[0].open.get(size=size),
            'high': self.datas[0].high.get(size=size),
            'low': self.datas[0].low.get(size=size),
            'close': self.datas[0].close.get(size=size),
            'volume': self.datas[0].volume.get(size=size),
        })
        
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    
    def next(self):
        """主逻辑"""
        if self.order:
            return
        
        if len(self) < 60:
            return
        
        df = self.get_dataframe_from_data()
        
        is_match, detail = self.detector.check_pattern(df)
        
        if is_match and not self.position:
            current_positions = len([p for p in self.stock_positions.values() if p])
            
            if current_positions < self.p.max_positions:
                score = self.scorer.score(detail, self.p.pattern_params)
                
                if score >= 70:
                    close_price = self.dataclose[0]
                    size = int(self.broker.getcash() * 0.2 / close_price / 100) * 100
                    
                    if size > 0:
                        self.order = self.buy(size=size)
                        self.stock_positions[self.data._name] = True
                        self.position_info[self.data._name] = {
                            'entry_price': close_price,
                            'entry_date': datetime.now(),
                            'support_level': detail.get('support_level', close_price),
                            'peak_price': detail.get('surge_high', close_price),
                            'score': score
                        }
                        logger.info(f"买入信号：评分 {score:.2f} 价格 {close_price:.2f}")
        
        if self.position:
            self.check_sell_conditions()
    
    def check_sell_conditions(self):
        """检查卖出条件"""
        current_price = self.dataclose[0]
        current_high = self.datahigh[0]
        
        pos_info = self.position_info.get(self.data._name, {})
        if not pos_info:
            return
        
        entry_price = pos_info.get('entry_price', current_price)
        support_level = pos_info.get('support_level', entry_price * 0.97)
        peak_price = max(pos_info.get('peak_price', entry_price), current_high)
        
        pos_info['peak_price'] = peak_price
        
        stop_loss_price = support_level * (1 - self.p.stop_loss_ratio)
        
        if current_price < stop_loss_price:
            self.order = self.sell(size=self.position.size)
            logger.info(f"止损卖出：支撑位 {support_level:.2f} 当前价 {current_price:.2f}")
            return
        
        trailing_stop_price = peak_price * (1 - self.p.trailing_stop)
        
        if current_price < trailing_stop_price and peak_price > entry_price * 1.05:
            self.order = self.sell(size=self.position.size)
            logger.info(f"止盈卖出：最高价 {peak_price:.2f} 当前价 {current_price:.2f}")
            return
        
        self.position_info[self.data._name] = pos_info


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cerebro = bt.Cerebro()
        self.results = None
        self.analyzers = {}
    
    def add_data(self, df: pd.DataFrame, dataname: str = 'data'):
        """添加数据到回测引擎"""
        if 'date' in df.columns:
            df.set_index('date', inplace=True)
        
        data = PandasDataWithVolume(
            dataname=df,
            fromdate=df.index.min(),
            todate=df.index.max()
        )
        
        self.cerebro.adddata(data, name=dataname)
        logger.info(f"添加回测数据：{dataname}，共 {len(df)} 条")
    
    def setup_strategy(self, pattern_params: Dict, scoring_weights: Dict,
                       trading_params: Dict):
        """设置策略"""
        self.cerebro.addstrategy(
            PatternStrategy,
            pattern_params=pattern_params,
            scoring_weights=scoring_weights,
            max_positions=trading_params.get('max_positions', 5),
            stop_loss_ratio=trading_params.get('stop_loss_ratio', 0.03),
            trailing_stop=trading_params.get('trailing_stop', 0.05),
            min_hold_days=trading_params.get('min_hold_days', 1),
            max_hold_days=trading_params.get('max_hold_days', 60),
        )
        
        logger.info("策略设置完成")
    
    def setup_broker(self, initial_cash: float = 1000000,
                     commission_rate: float = 0.00025,
                     slippage: float = 0.001):
        """设置经纪商"""
        self.cerebro.broker.setcash(initial_cash)
        self.cerebro.broker.setcommission(commission=commission_rate)
        # 滑点设置在某些 Backtrader 版本中不支持，暂时注释
        # self.cerebro.broker.setslippage_perc(slippage)
        
        logger.info(f"经纪商设置：初始资金 {initial_cash:.2f} 佣金率 {commission_rate:.4f}")
    
    def add_analyzers(self):
        """添加分析器"""
        self.cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                                 riskfreerate=0.02, annualize=True)
        self.cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        self.cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
        self.cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
        self.cerebro.addanalyzer(bt.analyzers.SQN, _name='sqn')
        
        logger.info("分析器添加完成")
    
    def run(self) -> Dict[str, Any]:
        """运行回测"""
        logger.info("=" * 60)
        logger.info("开始运行回测...")
        
        initial_cash = self.cerebro.broker.getcash()
        logger.info(f"初始资金：{initial_cash:.2f}")
        
        self.results = self.cerebro.run()
        strat = self.results[0]
        
        final_cash = self.cerebro.broker.getvalue()
        logger.info(f"最终资金：{final_cash:.2f}")
        
        pnl = final_cash - initial_cash
        pnl_pct = pnl / initial_cash * 100 if initial_cash > 0 else 0
        
        logger.info(f"总盈亏：{pnl:.2f} ({pnl_pct:.2f}%)")
        
        self.analyzers = self._extract_analyzers(strat)
        
        return {
            'initial_cash': initial_cash,
            'final_cash': final_cash,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            **self.analyzers
        }
    
    def _extract_analyzers(self, strat) -> Dict[str, Any]:
        """提取分析器结果"""
        results = {}
        
        try:
            sharpe = strat.analyzers.sharpe.get_analysis()
            results['sharpe_ratio'] = sharpe.get('sharperatio', 0)
            logger.info(f"夏普比率：{results['sharpe_ratio']:.4f}")
        except:
            results['sharpe_ratio'] = 0
        
        try:
            drawdown = strat.analyzers.drawdown.get_analysis()
            results['max_drawdown'] = drawdown['max']['drawdown']
            results['max_drawdown_len'] = drawdown['max']['len']
            logger.info(f"最大回撤：{results['max_drawdown']:.2f}%")
        except:
            results['max_drawdown'] = 0
        
        try:
            trades = strat.analyzers.trades.get_analysis()
            total_trades = trades['total']['total']
            won_trades = trades['won']['total'] if 'won' in trades else 0
            results['total_trades'] = total_trades
            results['win_rate'] = won_trades / total_trades * 100 if total_trades > 0 else 0
            logger.info(f"总交易数：{total_trades} 胜率：{results['win_rate']:.2f}%")
        except:
            results['total_trades'] = 0
            results['win_rate'] = 0
        
        try:
            returns = strat.analyzers.returns.get_analysis()
            results['avg_return'] = returns.get('avgreturn', 0)
            logger.info(f"平均收益：{results['avg_return']:.4f}%")
        except:
            results['avg_return'] = 0
        
        return results
    
    def plot(self, filename: str = 'backtest_result.png'):
        """绘制回测结果"""
        try:
            self.cerebro.plot(style='candle', volume=True)
            logger.info(f"回测图表已保存：{filename}")
        except Exception as e:
            logger.error(f"绘制图表失败：{e}")


def run_backtest(df: pd.DataFrame, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    运行回测的便捷函数
    
    Args:
        df: K 线数据
        config: 配置字典
    
    Returns:
        回测结果
    """
    engine = BacktestEngine(config)
    
    engine.add_data(df, 'test_stock')
    
    pattern_params = config.get('pattern', {})
    scoring_weights = config.get('scoring', {}).get('weights', {})
    trading_params = config.get('trading', {})
    
    engine.setup_strategy(pattern_params, scoring_weights, trading_params)
    
    backtest_config = config.get('backtest', {})
    engine.setup_broker(
        initial_cash=backtest_config.get('initial_cash', 1000000),
        commission_rate=backtest_config.get('commission_rate', 0.00025),
        slippage=backtest_config.get('slippage', 0.001)
    )
    
    engine.add_analyzers()
    
    results = engine.run()
    
    return results
