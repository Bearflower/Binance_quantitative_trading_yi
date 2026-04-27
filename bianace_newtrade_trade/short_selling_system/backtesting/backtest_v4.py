"""
V4.0 回测器

用于对比v3.1和v4.0策略的回测结果
"""

import json
from typing import Dict, Any, List, Optional
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scoring_engine_v4 import scoring_engine_v4, ScoringResultV4
from core.pattern_recognition_v4 import pattern_recognition_v4


class BacktesterV4:
    """V4.0 回测器"""

    def __init__(
        self,
        initial_capital: Decimal = Decimal('500'),
        max_position_size: Decimal = Decimal('0.02'),
        leverage: int = 2,
        stop_loss_atr: Decimal = Decimal('2.0'),
        tp1_atr: Decimal = Decimal('1.2'),
        tp2_atr: Decimal = Decimal('2.5'),
        max_holding_hours: int = 72
    ):
        """初始化回测器"""
        self.initial_capital = initial_capital
        self.max_position_size = max_position_size
        self.leverage = leverage
        self.stop_loss_atr = stop_loss_atr
        self.tp1_atr = tp1_atr
        self.tp2_atr = tp2_atr
        self.max_holding_hours = max_holding_hours

        self.trades = []
        self.daily_trade_count = {}
        self.consecutive_losses = 0
        self.paused_until = None

        print(f"✅ V4.0 回测器初始化完成")
        print(f"  初始资金: {initial_capital}U")
        print(f"  杠杆: {leverage}x")
        print(f"  最大持仓时间: {max_holding_hours}小时")

    def calculate_atr(self, klines: List[Dict[str, Any]], period: int = 14) -> Decimal:
        """计算ATR"""
        if len(klines) < period + 1:
            return Decimal('0')

        tr_values = []
        for i in range(1, len(klines)):
            high = Decimal(str(klines[i]['high']))
            low = Decimal(str(klines[i]['low']))
            prev_close = Decimal(str(klines[i-1]['close']))

            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)

            tr = max(tr1, tr2, tr3)
            tr_values.append(tr)

        atr = sum(tr_values[-period:]) / Decimal(period)
        return atr

    def check_daily_limit(self, date_str: str) -> bool:
        """检查每日交易次数限制"""
        if date_str not in self.daily_trade_count:
            self.daily_trade_count[date_str] = 0

        return self.daily_trade_count[date_str] < 3

    def check_pause_status(self, timestamp: datetime) -> bool:
        """检查是否处于暂停状态"""
        if self.paused_until is None:
            return False

        return timestamp < self.paused_until

    def update_consecutive_losses(self, pnl: Decimal, timestamp: datetime):
        """更新连续亏损计数"""
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 3:
                from datetime import timedelta
                self.paused_until = timestamp + timedelta(days=2)
                print(f"  ⚠️ 连续亏损{self.consecutive_losses}次，暂停交易至{self.paused_until}")
        else:
            self.consecutive_losses = 0

    def run_backtest(
        self,
        data: Dict[str, Any],
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        运行回测

        Args:
            data: K线数据
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            回测报告
        """
        print(f"\n{'='*80}")
        print(f"开始V4.0回测...")
        print(f"{'='*80}")

        symbols_data = data.get('data', {})

        if not symbols_data:
            return {'error': '无数据'}

        print(f"\n数据统计：")
        print(f"  币种数量: {len(symbols_data)}")

        capital = self.initial_capital
        total_trades = 0
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')

        for symbol, symbol_data in symbols_data.items():
            klines_1h = symbol_data.get('1h', [])

            if not klines_1h or len(klines_1h) < 10:
                continue

            print(f"\n处理 {symbol}...")

            for i in range(10, len(klines_1h)):
                current_klines = klines_1h[:i+1]
                current_kline = klines_1h[i]

                timestamp_str = current_kline.get('timestamp', '')
                try:
                    timestamp = datetime.fromisoformat(timestamp_str)
                except:
                    continue

                date_str = timestamp.strftime('%Y-%m-%d')

                if self.check_pause_status(timestamp):
                    continue

                if not self.check_daily_limit(date_str):
                    continue

                pattern_result = pattern_recognition_v4.analyze_patterns(current_klines)

                if pattern_result['data_insufficient']:
                    continue

                three_tops_score = pattern_result['three_tops']['score']
                technical_score = pattern_result['total_score']

                technical_ok, _ = scoring_engine_v4.check_technical_requirements(
                    three_tops_score,
                    technical_score
                )

                if not technical_ok:
                    continue

                contract_score = 7.0
                sentiment_score = 5.0

                total_score = scoring_engine_v4.calculate_total_score(
                    contract_score,
                    technical_score,
                    sentiment_score
                )

                if total_score < scoring_engine_v4.entry_threshold:
                    continue

                entry_price = Decimal(str(current_kline['close']))
                atr = self.calculate_atr(current_klines)

                if atr == 0:
                    stop_loss_price = entry_price * Decimal('1.05')
                else:
                    stop_loss_price = entry_price + self.stop_loss_atr * atr

                position_value = (capital * self.max_position_size) / ((stop_loss_price - entry_price) / entry_price)

                print(f"  开仓: {symbol} @ {entry_price:.4f}, 评分={total_score:.2f}")

                self.daily_trade_count[date_str] = self.daily_trade_count.get(date_str, 0) + 1

                trade = {
                    'symbol': symbol,
                    'entry_time': timestamp,
                    'entry_price': entry_price,
                    'position_value': position_value,
                    'stop_loss': stop_loss_price,
                    'score': total_score,
                    'pattern': pattern_result
                }

                for j in range(i+1, min(i+1+self.max_holding_hours, len(klines_1h))):
                    future_kline = klines_1h[j]
                    future_high = Decimal(str(future_kline['high']))
                    future_low = Decimal(str(future_kline['low']))
                    future_close = Decimal(str(future_kline['close']))

                    if future_high >= stop_loss_price:
                        pnl = -position_value * ((stop_loss_price - entry_price) / entry_price)
                        exit_reason = "止损"

                        trade['exit_time'] = datetime.fromisoformat(future_kline.get('timestamp', ''))
                        trade['exit_price'] = stop_loss_price
                        trade['pnl'] = pnl
                        trade['exit_reason'] = exit_reason
                        break

                    if atr > 0:
                        tp1_price = entry_price - self.tp1_atr * atr
                        if future_low <= tp1_price:
                            pnl = position_value * self.tp1_atr * atr / entry_price * Decimal('0.3')
                            exit_reason = "第一止盈"

                            trade['exit_time'] = datetime.fromisoformat(future_kline.get('timestamp', ''))
                            trade['exit_price'] = tp1_price
                            trade['pnl'] = pnl
                            trade['exit_reason'] = exit_reason
                            break

                else:
                    last_kline = klines_1h[-1]
                    exit_price = Decimal(str(last_kline['close']))
                    pnl = position_value * ((entry_price - exit_price) / entry_price)
                    exit_reason = "时间止损"

                    trade['exit_time'] = datetime.fromisoformat(last_kline.get('timestamp', ''))
                    trade['exit_price'] = exit_price
                    trade['pnl'] = pnl
                    trade['exit_reason'] = exit_reason

                self.trades.append(trade)
                total_trades += 1

                if trade['pnl'] > 0:
                    winning_trades += 1
                else:
                    losing_trades += 1

                total_pnl += trade['pnl']
                capital += trade['pnl']

                self.update_consecutive_losses(trade['pnl'], trade['exit_time'])

                break

        win_rate = winning_trades / total_trades if total_trades > 0 else 0

        report = {
            'version': 'v4.0',
            'summary': {
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'losing_trades': losing_trades,
                'win_rate': win_rate,
                'total_pnl': float(total_pnl),
                'final_capital': float(capital),
                'total_return': float((capital - self.initial_capital) / self.initial_capital)
            },
            'trades': self.trades
        }

        print(f"\n{'='*80}")
        print(f"V4.0回测完成")
        print(f"{'='*80}")
        print(f"\n📊 回测结果：")
        print(f"  总交易: {total_trades} 笔")
        print(f"  盈利: {winning_trades} 笔 | 亏损: {losing_trades} 笔")
        print(f"  胜率: {win_rate:.1%}")
        print(f"  总盈亏: {total_pnl:.2f}U")
        print(f"  最终资金: {capital:.2f}U")
        print(f"  收益率: {float((capital - self.initial_capital) / self.initial_capital):.1%}")

        return report


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='V4.0 回测')
    parser.add_argument('--data', type=str, required=True, help='数据文件路径')
    parser.add_argument('--output', type=str, default='backtest_report_v4.json', help='输出文件路径')

    args = parser.parse_args()

    print(f"加载数据: {args.data}")
    with open(args.data, 'r', encoding='utf-8') as f:
        data = json.load(f)

    backtester = BacktesterV4()
    report = backtester.run_backtest(data)

    print(f"\n保存报告: {args.output}")
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"✅ 完成")
