"""
ETHUSDT网格策略回测引擎
模拟网格交易策略运行，计算回测指标，生成回测报告
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import structlog
import yaml
import os
import sys

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, project_root)

from shared.indicators import TechnicalIndicators
from strategies.grid.grid_calculator import GridCalculator, GridLevel, DynamicGridParams, GridMode
from strategies.grid.market_state import MarketStateDetector, MarketState, MarketAnalysis

logger = structlog.get_logger()


@dataclass
class GridPosition:
    """网格持仓状态"""
    entry_price: Decimal
    quantity: Decimal
    side: str  # 'BUY' or 'SELL'
    entry_time: datetime
    grid_level: int


@dataclass
class Trade:
    """交易记录"""
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    side: str
    quantity: Decimal
    pnl: Decimal
    pnl_percent: Decimal
    grid_level: int


class GridBacktestEngine:
    """网格策略回测引擎"""

    def __init__(self, config: Dict):
        """
        初始化回测引擎

        Args:
            config: 策略配置字典
        """
        self.config = config

        # 回测参数
        self.initial_capital = Decimal(str(config.get('trading', {}).get('margin', 500)))
        self.current_capital = self.initial_capital
        self.highest_capital = self.initial_capital

        # 手续费率（币安合约手续费率）
        self.maker_fee = Decimal('0.0002')  # 0.02%
        self.taker_fee = Decimal('0.0004')  # 0.04%
        self.slippage = Decimal('0.0001')   # 0.01% 滑点

        # 持仓和交易记录
        self.positions: List[GridPosition] = []
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []

        # 网格状态
        self.grid_levels: List[GridLevel] = []
        self.grid_params: Optional[DynamicGridParams] = None
        self.market_state: Optional[MarketState] = None

        # 网格计算器
        self.grid_calculator = GridCalculator(config)

        # 统计数据
        self.total_trades = 0
        self.win_trades = 0
        self.loss_trades = 0
        self.total_pnl = Decimal('0')
        self.max_drawdown = Decimal('0')
        self.max_drawdown_percent = Decimal('0')

        logger.info(
            "回测引擎初始化完成",
            initial_capital=float(self.initial_capital),
            maker_fee=float(self.maker_fee),
            taker_fee=float(self.taker_fee)
        )

    def load_klines(self, symbol: str, interval: str) -> pd.DataFrame:
        """
        从CSV文件加载K线数据

        Args:
            symbol: 交易对
            interval: 时间周期

        Returns:
            K线数据DataFrame
        """
        # 构建文件路径
        data_path = os.path.join(
            project_root,
            'data', 'klines', f'{symbol.lower()}_{interval}.csv'
        )

        # 读取CSV文件
        df = pd.read_csv(data_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)

        logger.info(
            f"加载{interval}数据成功",
            symbol=symbol,
            count=len(df),
            start=df.index[0],
            end=df.index[-1]
        )

        return df

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算技术指标

        Args:
            df: K线数据

        Returns:
            添加了技术指标的DataFrame
        """
        # 计算ADX
        df['ADX'] = TechnicalIndicators.calculate_adx(df, period=14)

        # 计算EMA
        df['EMA20'] = TechnicalIndicators.calculate_ema(df, period=20)
        df['EMA50'] = TechnicalIndicators.calculate_ema(df, period=50)

        # 计算ATR
        df['ATR'] = TechnicalIndicators.calculate_atr(df, period=14)

        # 计算ATR平滑值
        df['ATR_Smooth'] = df['ATR'].ewm(span=14, adjust=False).mean()

        return df

    def detect_market_state(self, row: pd.Series, df_4h: pd.DataFrame, idx: int) -> Tuple[MarketState, Decimal]:
        """
        检测市场状态

        Args:
            row: 当前K线数据
            df_4h: 4小时K线数据
            idx: 当前索引

        Returns:
            (市场状态, 趋势强度)
        """
        adx_1h = Decimal(str(row['ADX'])) if pd.notna(row['ADX']) else Decimal('0')
        adx_4h = Decimal(str(df_4h['ADX'].iloc[idx])) if idx < len(df_4h) and pd.notna(df_4h['ADX'].iloc[idx]) else Decimal('0')
        ema20 = Decimal(str(row['EMA20'])) if pd.notna(row['EMA20']) else Decimal('0')
        ema50 = Decimal(str(row['EMA50'])) if pd.notna(row['EMA50']) else Decimal('0')
        current_price = Decimal(str(row['close']))

        # 获取配置参数
        adx_oscillation = self.config.get('market', {}).get('adx_oscillation', 20)
        adx_trend = self.config.get('market', {}).get('adx_trend', 25)
        adx_strong = self.config.get('market', {}).get('adx_strong', 40)

        # 判断市场状态
        if adx_4h >= adx_strong:
            return MarketState.STRONG_TREND_PAUSE, Decimal('0')

        if adx_1h < adx_oscillation:
            return MarketState.OSCILLATION, Decimal('0')

        if adx_1h >= adx_trend:
            is_uptrend = ema20 > ema50 and current_price > ema20
            is_downtrend = ema20 < ema50 and current_price < ema20

            if is_uptrend and adx_4h >= adx_trend:
                # 计算趋势强度
                trend_strength = min(Decimal('0.5'), max(Decimal('0'), (adx_1h - Decimal(str(adx_trend))) / Decimal('30')))
                return MarketState.UPTREND, trend_strength
            elif is_downtrend and adx_4h >= adx_trend:
                trend_strength = min(Decimal('0.5'), max(Decimal('0'), (adx_1h - Decimal(str(adx_trend))) / Decimal('30')))
                return MarketState.DOWNTREND, trend_strength

        return MarketState.OSCILLATION, Decimal('0')

    def calculate_dynamic_grid_params(
        self,
        current_price: Decimal,
        atr_smooth: Decimal,
        market_state: MarketState,
        trend_strength: Decimal
    ) -> DynamicGridParams:
        """
        计算动态网格参数

        Args:
            current_price: 当前价格
            atr_smooth: 平滑ATR
            market_state: 市场状态
            trend_strength: 趋势强度

        Returns:
            动态网格参数
        """
        # 使用固定的基准ATR（简化处理）
        atr_baseline = atr_smooth * Decimal('1.2')

        # 调用网格计算器
        params = self.grid_calculator.calculate_dynamic_grid_params(
            current_price=current_price,
            atr_smooth=atr_smooth,
            atr_baseline=atr_baseline,
            market_state=market_state.value,
            trend_strength=trend_strength
        )

        return params

    def initialize_grid(self, current_price: Decimal, atr_smooth: Decimal, market_state: MarketState, trend_strength: Decimal):
        """
        初始化网格

        Args:
            current_price: 当前价格
            atr_smooth: 平滑ATR
            market_state: 市场状态
            trend_strength: 趋势强度
        """
        # 计算动态网格参数
        self.grid_params = self.calculate_dynamic_grid_params(
            current_price=current_price,
            atr_smooth=atr_smooth,
            market_state=market_state,
            trend_strength=trend_strength
        )

        # 计算网格层级
        self.grid_levels = []

        for i in range(self.grid_params.grid_count):
            if self.grid_params.grid_mode == GridMode.ARITHMETIC:
                # 等差网格
                price = self.grid_params.lower_boundary + self.grid_params.grid_spacing * i
            else:
                # 等比网格
                ratio = (self.grid_params.upper_boundary / self.grid_params.lower_boundary) ** (Decimal('1') / Decimal(str(self.grid_params.grid_count)))
                price = self.grid_params.lower_boundary * (ratio ** i)

            # 确定交易方向
            if price < current_price:
                side = 'BUY'
            elif price > current_price:
                side = 'SELL'
            else:
                side = 'HOLD'

            # 计算数量
            quantity = self.current_capital / (self.grid_params.grid_count * price)

            level = GridLevel(
                price=price,
                side=side,
                quantity=quantity,
                level=i
            )
            self.grid_levels.append(level)

        logger.info(
            "网格初始化完成",
            grid_count=len(self.grid_levels),
            lower_boundary=float(self.grid_params.lower_boundary),
            upper_boundary=float(self.grid_params.upper_boundary),
            market_state=market_state.value
        )

    def check_grid_orders(self, current_time: datetime, current_price: Decimal, current_high: Decimal, current_low: Decimal):
        """
        检查网格订单成交情况

        Args:
            current_time: 当前时间
            current_price: 当前价格
            current_high: 当前最高价
            current_low: 当前最低价
        """
        # 检查每个网格层级
        for level in self.grid_levels[:]:  # 使用切片创建副本
            if level.side == 'BUY' and current_low <= level.price:
                # 买单成交
                self._execute_buy(level, current_time, level.price)

            elif level.side == 'SELL' and current_high >= level.price:
                # 卖单成交
                self._execute_sell(level, current_time, level.price)

    def _execute_buy(self, level: GridLevel, current_time: datetime, execution_price: Decimal):
        """
        执行买入

        Args:
            level: 网格层级
            current_time: 当前时间
            execution_price: 执行价格
        """
        # 计算实际价格（包含滑点）
        actual_price = execution_price * (Decimal('1') + self.slippage)

        # 计算手续费
        fee = level.quantity * actual_price * self.taker_fee

        # 扣除资金
        cost = level.quantity * actual_price + fee
        self.current_capital -= cost

        # 创建持仓
        position = GridPosition(
            entry_price=actual_price,
            quantity=level.quantity,
            side='BUY',
            entry_time=current_time,
            grid_level=level.level
        )
        self.positions.append(position)

        # 更新网格层级（挂反向卖单）
        level.side = 'SELL'
        level.price = actual_price * (Decimal('1') + self.grid_params.profit_rate)

        logger.debug(
            "买入成交",
            price=float(actual_price),
            quantity=float(level.quantity),
            cost=float(cost),
            capital=float(self.current_capital)
        )

    def _execute_sell(self, level: GridLevel, current_time: datetime, execution_price: Decimal):
        """
        执行卖出

        Args:
            level: 网格层级
            current_time: 当前时间
            execution_price: 执行价格
        """
        # 查找对应的持仓
        matching_positions = [p for p in self.positions if p.grid_level == level.level and p.side == 'BUY']

        if not matching_positions:
            return

        position = matching_positions[0]

        # 计算实际价格（包含滑点）
        actual_price = execution_price * (Decimal('1') - self.slippage)

        # 计算手续费
        fee = level.quantity * actual_price * self.taker_fee

        # 计算盈亏
        revenue = level.quantity * actual_price - fee
        cost = position.quantity * position.entry_price
        pnl = revenue - cost
        pnl_percent = pnl / cost

        # 更新资金
        self.current_capital += revenue

        # 记录交易
        trade = Trade(
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=current_time,
            exit_price=actual_price,
            side='SELL',
            quantity=level.quantity,
            pnl=pnl,
            pnl_percent=pnl_percent,
            grid_level=level.level
        )
        self.trades.append(trade)

        # 更新统计
        self.total_trades += 1
        if pnl > 0:
            self.win_trades += 1
        else:
            self.loss_trades += 1
        self.total_pnl += pnl

        # 移除持仓
        self.positions.remove(position)

        # 更新网格层级（挂反向买单）
        level.side = 'BUY'
        level.price = actual_price * (Decimal('1') - self.grid_params.profit_rate)

        logger.debug(
            "卖出成交",
            price=float(actual_price),
            quantity=float(level.quantity),
            pnl=float(pnl),
            pnl_percent=float(pnl_percent) * 100,
            capital=float(self.current_capital)
        )

    def check_risk(self, current_price: Decimal):
        """
        检查风险控制

        Args:
            current_price: 当前价格
        """
        # 计算当前权益
        equity = self.current_capital
        for position in self.positions:
            unrealized_pnl = (current_price - position.entry_price) * position.quantity
            equity += unrealized_pnl

        # 更新最高权益
        if equity > self.highest_capital:
            self.highest_capital = equity

        # 计算回撤
        drawdown = self.highest_capital - equity
        drawdown_percent = drawdown / self.highest_capital

        # 更新最大回撤
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
            self.max_drawdown_percent = drawdown_percent

        # 检查是否触发止损
        max_drawdown_threshold = Decimal(str(self.config.get('risk', {}).get('max_drawdown', 0.1)))
        if drawdown_percent >= max_drawdown_threshold:
            logger.warning(
                "触发最大回撤止损",
                drawdown_percent=float(drawdown_percent) * 100,
                threshold=float(max_drawdown_threshold) * 100
            )

    def check_grid_reset(self, current_price: Decimal, current_time: datetime, atr_smooth: Decimal, market_state: MarketState, trend_strength: Decimal):
        """
        检查是否需要重置网格

        Args:
            current_price: 当前价格
            current_time: 当前时间
            atr_smooth: 平滑ATR
            market_state: 市场状态
            trend_strength: 趋势强度
        """
        if not self.grid_params:
            return

        # 计算网格中心价格
        center_price = (self.grid_params.lower_boundary + self.grid_params.upper_boundary) / 2

        # 计算价格偏离比例
        deviation = abs(current_price - center_price) / center_price

        # 获取重置阈值
        grid_reset_threshold = Decimal(str(self.config.get('risk', {}).get('grid_reset_threshold', 0.15)))

        # 如果价格偏离过大，重置网格
        if deviation > grid_reset_threshold:
            logger.info(
                "价格偏离网格中心过大，重置网格",
                current_price=float(current_price),
                center_price=float(center_price),
                deviation=float(deviation) * 100
            )

            # 清空持仓
            self.positions.clear()

            # 重新初始化网格
            self.initialize_grid(current_price, atr_smooth, market_state, trend_strength)

    def run_backtest(self, symbol: str) -> Dict:
        """
        运行回测

        Args:
            symbol: 交易对

        Returns:
            回测结果字典
        """
        logger.info(f"开始回测 {symbol}")

        # 加载K线数据
        df_15m = self.load_klines(symbol, '15m')
        df_1h = self.load_klines(symbol, '1h')
        df_4h = self.load_klines(symbol, '4h')

        # 计算技术指标
        df_1h = self.calculate_indicators(df_1h)
        df_4h = self.calculate_indicators(df_4h)

        # 遍历1小时K线
        for i in range(100, len(df_1h)):
            current_time = df_1h.index[i]
            current_price = Decimal(str(df_1h['close'].iloc[i]))
            current_high = Decimal(str(df_1h['high'].iloc[i]))
            current_low = Decimal(str(df_1h['low'].iloc[i]))

            # 获取ATR平滑值
            atr_smooth = Decimal(str(df_1h['ATR_Smooth'].iloc[i])) if pd.notna(df_1h['ATR_Smooth'].iloc[i]) else Decimal('0')

            # 检测市场状态
            market_state, trend_strength = self.detect_market_state(df_1h.iloc[i], df_4h, i)

            # 如果是强趋势暂停，跳过
            if market_state == MarketState.STRONG_TREND_PAUSE:
                continue

            # 如果网格未初始化或需要重置，初始化网格
            if not self.grid_levels or self.market_state != market_state:
                self.initialize_grid(current_price, atr_smooth, market_state, trend_strength)
                self.market_state = market_state

            # 检查网格订单
            self.check_grid_orders(current_time, current_price, current_high, current_low)

            # 检查风险
            self.check_risk(current_price)

            # 检查网格重置
            self.check_grid_reset(current_price, current_time, atr_smooth, market_state, trend_strength)

            # 记录权益曲线
            equity = float(self.current_capital)
            for position in self.positions:
                unrealized_pnl = float((current_price - position.entry_price) * position.quantity)
                equity += unrealized_pnl

            self.equity_curve.append({
                'timestamp': current_time,
                'equity': equity
            })

        # 强制平仓最后的持仓
        if self.positions:
            final_price = Decimal(str(df_1h['close'].iloc[-1]))
            for position in self.positions[:]:
                # 计算盈亏
                pnl = (final_price - position.entry_price) * position.quantity
                pnl_percent = pnl / (position.quantity * position.entry_price)

                # 更新资金
                self.current_capital += position.quantity * final_price

                # 记录交易
                trade = Trade(
                    entry_time=position.entry_time,
                    entry_price=position.entry_price,
                    exit_time=df_1h.index[-1],
                    exit_price=final_price,
                    side='SELL',
                    quantity=position.quantity,
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                    grid_level=position.grid_level
                )
                self.trades.append(trade)

                # 更新统计
                self.total_trades += 1
                if pnl > 0:
                    self.win_trades += 1
                else:
                    self.loss_trades += 1
                self.total_pnl += pnl

        # 计算回测指标
        results = self._calculate_metrics()

        logger.info(
            "回测完成",
            total_return=float(results['total_return']),
            total_trades=results['total_trades'],
            win_rate=float(results['win_rate'])
        )

        return results

    def _calculate_metrics(self) -> Dict:
        """
        计算回测指标

        Returns:
            回测指标字典
        """
        # 总收益率
        total_return = (self.current_capital - self.initial_capital) / self.initial_capital

        # 胜率
        win_rate = Decimal(str(self.win_trades / self.total_trades)) if self.total_trades > 0 else Decimal('0')

        # 平均盈利和平均亏损
        win_pnls = [t.pnl for t in self.trades if t.pnl > 0]
        loss_pnls = [t.pnl for t in self.trades if t.pnl < 0]

        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else Decimal('0')
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else Decimal('0')

        # 盈亏比
        profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else Decimal('0')

        # 夏普比率（简化计算）
        if len(self.equity_curve) > 1:
            returns = []
            for i in range(1, len(self.equity_curve)):
                prev_equity = Decimal(str(self.equity_curve[i-1]['equity']))
                curr_equity = Decimal(str(self.equity_curve[i]['equity']))
                ret = (curr_equity - prev_equity) / prev_equity
                returns.append(float(ret))

            if returns:
                avg_return = np.mean(returns)
                std_return = np.std(returns)
                sharpe_ratio = Decimal(str(avg_return / std_return * np.sqrt(365 * 24))) if std_return > 0 else Decimal('0')
            else:
                sharpe_ratio = Decimal('0')
        else:
            sharpe_ratio = Decimal('0')

        return {
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_return': float(total_return * 100),
            'total_trades': self.total_trades,
            'win_trades': self.win_trades,
            'loss_trades': self.loss_trades,
            'win_rate': float(win_rate * 100),
            'avg_win': float(avg_win),
            'avg_loss': float(avg_loss),
            'profit_loss_ratio': float(profit_loss_ratio),
            'max_drawdown': float(self.max_drawdown),
            'max_drawdown_percent': float(self.max_drawdown_percent * 100),
            'sharpe_ratio': float(sharpe_ratio),
            'trades': self.trades,
            'equity_curve': self.equity_curve
        }

    def generate_report(self, results: Dict, symbol: str) -> str:
        """
        生成回测报告

        Args:
            results: 回测结果
            symbol: 交易对

        Returns:
            回测报告Markdown文本
        """
        report = f"""# {symbol}网格策略回测报告

## 📊 回测概览

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **交易对**: {symbol}
- **初始资金**: {results['initial_capital']:.2f} USDT
- **最终资金**: {results['final_capital']:.2f} USDT
- **总收益率**: {results['total_return']:.2f}%
- **最大回撤**: {results['max_drawdown']:.2f} USDT ({results['max_drawdown_percent']:.2f}%)
- **夏普比率**: {results['sharpe_ratio']:.2f}

## 📈 交易统计

- **总交易次数**: {results['total_trades']}
- **盈利次数**: {results['win_trades']}
- **亏损次数**: {results['loss_trades']}
- **胜率**: {results['win_rate']:.2f}%
- **平均盈利**: {results['avg_win']:.2f} USDT
- **平均亏损**: {results['avg_loss']:.2f} USDT
- **盈亏比**: {results['profit_loss_ratio']:.2f}

## 📋 交易明细

"""

        # 添加前20笔交易明细
        for i, trade in enumerate(results['trades'][:20], 1):
            pnl_emoji = "✅" if trade.pnl > 0 else "❌"
            report += f"""### 交易 #{i} {pnl_emoji}
- **时间**: {trade.entry_time} → {trade.exit_time}
- **方向**: {trade.side}
- **入场价**: {float(trade.entry_price):.2f}
- **出场价**: {float(trade.exit_price):.2f}
- **数量**: {float(trade.quantity):.4f}
- **盈亏**: {float(trade.pnl):.2f} USDT ({float(trade.pnl_percent) * 100:.2f}%)
- **网格层级**: {trade.grid_level}

"""

        if len(results['trades']) > 20:
            report += f"\n*注：仅显示前20笔交易，共{len(results['trades'])}笔交易*\n"

        # 添加风险分析
        report += f"""
## ⚠️ 风险分析

### 回撤分析
- **最大回撤金额**: {results['max_drawdown']:.2f} USDT
- **最大回撤比例**: {results['max_drawdown_percent']:.2f}%

### 风险提示
1. 网格策略在震荡市场中表现较好，但在趋势市场中可能面临较大回撤
2. 需要合理设置网格参数和止损阈值
3. 建议结合市场状态检测动态调整网格参数

## 💡 优化建议

1. **参数优化**:
   - 调整网格数量和间距
   - 优化市场状态检测参数
   - 设置合理的止损止盈阈值

2. **风险控制**:
   - 设置最大回撤止损
   - 控制单次交易仓位
   - 避免在强趋势市场运行网格策略

3. **策略改进**:
   - 结合多时间框架分析
   - 添加移动止盈功能
   - 实现动态网格调整

---
*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

        return report


def main():
    """主函数"""
    # 加载配置
    config_path = os.path.join(project_root, 'strategies/grid/config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 创建回测引擎
    engine = GridBacktestEngine(config)

    # 运行回测
    symbol = 'ETHUSDT'
    results = engine.run_backtest(symbol)

    # 生成报告
    report = engine.generate_report(results, symbol)

    # 保存报告
    report_dir = os.path.join(project_root, 'backtest/grid/reports')
    os.makedirs(report_dir, exist_ok=True)

    report_path = os.path.join(report_dir, f'{symbol}_backtest_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    logger.info(f"回测报告已保存到 {report_path}")

    # 打印报告
    print(report)

    # 生成可视化报告
    try:
        from backtest_visualization import generate_detailed_report
        generate_detailed_report(results, symbol, report_dir)
        logger.info("可视化报告生成完成")
    except Exception as e:
        logger.error(f"生成可视化报告失败: {e}", exc_info=True)


if __name__ == "__main__":
    main()
