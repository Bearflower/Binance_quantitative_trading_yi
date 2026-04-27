#!/usr/bin/env python3
"""
V4.0 完整回测脚本（使用真实资金费率 + 模拟OI/市值比）
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import random

sys.path.insert(0, str(Path(__file__).parent))

from core.scoring_engine_v4 import scoring_engine_v4
from core.pattern_recognition_v4 import pattern_recognition_v4


class CompleteBacktesterV4Hybrid:
    """V4.0 完整回测器（混合数据）"""

    def __init__(
        self,
        initial_capital: Decimal = Decimal('500'),
        max_position_size: Decimal = Decimal('0.02'),
        leverage: int = 2,
        stop_loss_atr: Decimal = Decimal('2.0'),
        tp1_atr: Decimal = Decimal('1.2'),
        tp2_atr: Decimal = Decimal('2.5'),
        max_holding_hours: int = 72,
        trading_fee: Decimal = Decimal('0.0004'),
        entry_threshold: float = 6.0,
        max_klines_per_symbol: int = 100
    ):
        self.initial_capital = initial_capital
        self.max_position_size = max_position_size
        self.leverage = leverage
        self.stop_loss_atr = stop_loss_atr
        self.tp1_atr = tp1_atr
        self.tp2_atr = tp2_atr
        self.max_holding_hours = max_holding_hours
        self.trading_fee = trading_fee
        self.entry_threshold = entry_threshold
        self.max_klines_per_symbol = max_klines_per_symbol

        self.trades = []
        self.daily_trade_count = {}

        self.capital = initial_capital
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = Decimal('0')
        self.total_fees = Decimal('0')

        print(f"✅ V4.0 完整回测器初始化完成（混合数据）")
        print(f"  初始资金: {initial_capital}U")
        print(f"  杠杆: {leverage}x")
        print(f"  最大持仓时间: {max_holding_hours}小时")
        print(f"  交易手续费: {float(trading_fee)*100:.2f}%")
        print(f"  开仓阈值: {entry_threshold}分")
        print(f"  每个币种最多检查K线: {max_klines_per_symbol}根")

    def calculate_atr(self, klines: list, period: int = 14) -> Decimal:
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

    def simulate_oi_ratio(self) -> Decimal:
        """模拟OI/市值比（基于合理分布）"""
        # 根据V4.0文档，新币的OI/市值比通常在0.1-0.6之间
        # 使用正态分布，均值0.3，标准差0.15
        oi_ratio = random.gauss(0.3, 0.15)
        # 限制在合理范围内
        oi_ratio = max(0.1, min(0.8, oi_ratio))
        return Decimal(str(oi_ratio))

    def run_backtest(self, kline_data_file: str, real_data_file: str, output_file: str):
        """运行完整回测"""

        print(f"\n{'='*80}")
        print(f"V4.0 完整回测（混合数据）")
        print(f"{'='*80}")

        print(f"\n加载K线数据: {kline_data_file}")
        with open(kline_data_file, 'r', encoding='utf-8') as f:
            kline_data = json.load(f)

        print(f"加载真实数据: {real_data_file}")
        with open(real_data_file, 'r', encoding='utf-8') as f:
            real_data = json.load(f)

        symbols_data = kline_data.get('data', {})
        print(f"币种数量: {len(symbols_data)}")
        print(f"真实数据数量: {len(real_data)}")

        for symbol, symbol_data in symbols_data.items():
            klines_1h_dict = symbol_data.get('1h', {})

            if isinstance(klines_1h_dict, dict):
                klines_1h = klines_1h_dict.get('data', [])
            else:
                klines_1h = klines_1h_dict

            if not klines_1h or len(klines_1h) < 10:
                continue

            # 获取真实资金费率
            symbol_real_data = real_data.get(symbol, {})
            funding_rate = symbol_real_data.get('funding_rate')

            # 如果没有资金费率数据，跳过
            if funding_rate is None:
                continue

            # 使用模拟的OI/市值比
            oi_ratio = self.simulate_oi_ratio()

            signal_found = False

            for i in range(10, min(len(klines_1h), self.max_klines_per_symbol)):
                if signal_found:
                    break

                current_klines = klines_1h[:i+1]
                current_kline = klines_1h[i]

                timestamp_str = current_kline.get('timestamp', '')
                try:
                    timestamp = datetime.fromisoformat(timestamp_str) if isinstance(timestamp_str, str) else datetime.fromtimestamp(timestamp_str / 1000)
                except:
                    continue

                date_str = timestamp.strftime('%Y-%m-%d')

                if date_str not in self.daily_trade_count:
                    self.daily_trade_count[date_str] = 0

                if self.daily_trade_count[date_str] >= 3:
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

                # 使用模拟的OI/市值比和真实的资金费率
                contract_score, _ = scoring_engine_v4.calculate_contract_score(float(oi_ratio))
                sentiment_score, _ = scoring_engine_v4.calculate_sentiment_score(funding_rate)

                total_score = scoring_engine_v4.calculate_total_score(
                    contract_score,
                    technical_score,
                    sentiment_score
                )

                if total_score < self.entry_threshold:
                    continue

                entry_price = Decimal(str(current_kline['close']))
                atr = self.calculate_atr(current_klines)

                if atr == 0:
                    stop_loss_price = entry_price * Decimal('1.05')
                else:
                    stop_loss_price = entry_price + self.stop_loss_atr * atr

                position_value = (self.capital * self.max_position_size) / ((stop_loss_price - entry_price) / entry_price)
                position_value = min(position_value, self.capital * Decimal('0.2'))

                print(f"\n  ✅ 开仓 {symbol} @ {entry_price:.6f}, 评分={total_score:.2f}")
                print(f"     OI/市值比: {oi_ratio:.4f} (模拟), 资金费率: {funding_rate:.2f}% (真实)")
                print(f"     止损: {stop_loss_price:.6f}, 仓位: {position_value:.2f}U")

                self.daily_trade_count[date_str] += 1

                entry_fee = position_value * self.trading_fee
                self.total_fees += entry_fee

                trade_exit = None

                for j in range(i+1, min(i+1+self.max_holding_hours, len(klines_1h))):
                    future_kline = klines_1h[j]
                    future_high = Decimal(str(future_kline['high']))
                    future_low = Decimal(str(future_kline['low']))
                    future_close = Decimal(str(future_kline['close']))

                    if future_high >= stop_loss_price:
                        exit_price = stop_loss_price
                        pnl = -position_value * ((stop_loss_price - entry_price) / entry_price)
                        exit_reason = "止损"
                        trade_exit = {
                            'exit_time': datetime.fromisoformat(future_kline.get('timestamp', '')) if isinstance(future_kline.get('timestamp', ''), str) else datetime.fromtimestamp(future_kline.get('timestamp', 0) / 1000),
                            'exit_price': exit_price,
                            'pnl': pnl,
                            'exit_reason': exit_reason
                        }
                        break

                    if atr > 0:
                        tp1_price = entry_price - self.tp1_atr * atr
                        if future_low <= tp1_price:
                            exit_price = tp1_price
                            pnl = position_value * self.tp1_atr * atr / entry_price * Decimal('0.3')
                            exit_reason = "第一止盈"
                            trade_exit = {
                                'exit_time': datetime.fromisoformat(future_kline.get('timestamp', '')) if isinstance(future_kline.get('timestamp', ''), str) else datetime.fromtimestamp(future_kline.get('timestamp', 0) / 1000),
                                'exit_price': exit_price,
                                'pnl': pnl,
                                'exit_reason': exit_reason
                            }
                            break

                        tp2_price = entry_price - self.tp2_atr * atr
                        if future_low <= tp2_price:
                            exit_price = tp2_price
                            pnl = position_value * self.tp2_atr * atr / entry_price * Decimal('0.7')
                            exit_reason = "第二止盈"
                            trade_exit = {
                                'exit_time': datetime.fromisoformat(future_kline.get('timestamp', '')) if isinstance(future_kline.get('timestamp', ''), str) else datetime.fromtimestamp(future_kline.get('timestamp', 0) / 1000),
                                'exit_price': exit_price,
                                'pnl': pnl,
                                'exit_reason': exit_reason
                            }
                            break

                if trade_exit is None:
                    last_kline = klines_1h[-1]
                    exit_price = Decimal(str(last_kline['close']))
                    pnl = position_value * ((entry_price - exit_price) / entry_price)
                    exit_reason = "时间止损"
                    trade_exit = {
                        'exit_time': datetime.fromisoformat(last_kline.get('timestamp', '')) if isinstance(last_kline.get('timestamp', ''), str) else datetime.fromtimestamp(last_kline.get('timestamp', 0) / 1000),
                        'exit_price': exit_price,
                        'pnl': pnl,
                        'exit_reason': exit_reason
                    }

                exit_fee = position_value * self.trading_fee
                self.total_fees += exit_fee

                net_pnl = trade_exit['pnl'] - entry_fee - exit_fee

                trade = {
                    'symbol': symbol,
                    'entry_time': timestamp,
                    'entry_price': float(entry_price),
                    'exit_time': trade_exit['exit_time'],
                    'exit_price': float(trade_exit['exit_price']),
                    'position_value': float(position_value),
                    'pnl': float(trade_exit['pnl']),
                    'fees': float(entry_fee + exit_fee),
                    'net_pnl': float(net_pnl),
                    'exit_reason': trade_exit['exit_reason'],
                    'score': float(total_score),
                    'oi_ratio': float(oi_ratio),
                    'funding_rate': funding_rate
                }

                self.trades.append(trade)
                self.total_trades += 1

                if net_pnl > 0:
                    self.winning_trades += 1
                else:
                    self.losing_trades += 1

                self.total_pnl += net_pnl
                self.capital += net_pnl

                print(f"     平仓 @ {trade_exit['exit_price']:.6f}, 原因: {exit_reason}")
                print(f"     盈亏: {net_pnl:.2f}U (手续费: {entry_fee + exit_fee:.2f}U)")

                signal_found = True

        win_rate = self.winning_trades / self.total_trades if self.total_trades > 0 else 0
        avg_pnl = self.total_pnl / self.total_trades if self.total_trades > 0 else Decimal('0')

        report = {
            'version': 'v4.0-hybrid',
            'timestamp': datetime.now().isoformat(),
            'entry_threshold': self.entry_threshold,
            'max_klines_per_symbol': self.max_klines_per_symbol,
            'data_source': {
                'oi_ratio': 'simulated',
                'funding_rate': 'real'
            },
            'summary': {
                'total_trades': self.total_trades,
                'winning_trades': self.winning_trades,
                'losing_trades': self.losing_trades,
                'win_rate': float(win_rate),
                'total_pnl': float(self.total_pnl),
                'total_fees': float(self.total_fees),
                'avg_pnl': float(avg_pnl),
                'final_capital': float(self.capital),
                'total_return': float((self.capital - self.initial_capital) / self.initial_capital)
            },
            'trades': self.trades
        }

        print(f"\n{'='*80}")
        print(f"V4.0 完整回测完成（混合数据）")
        print(f"{'='*80}")
        print(f"\n📊 回测结果：")
        print(f"  总交易: {self.total_trades} 笔")
        print(f"  盈利: {self.winning_trades} 笔 | 亏损: {self.losing_trades} 笔")
        print(f"  胜率: {win_rate:.1%}")
        print(f"  总盈亏: {self.total_pnl:.2f}U")
        print(f"  总手续费: {self.total_fees:.2f}U")
        print(f"  平均盈亏: {avg_pnl:.2f}U")
        print(f"  最终资金: {self.capital:.2f}U")
        print(f"  收益率: {float((self.capital - self.initial_capital) / self.initial_capital):.1%}")

        print(f"\n保存报告: {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        print(f"✅ 完成")

        return report


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='V4.0 完整回测（混合数据）')
    parser.add_argument('--kline-data', type=str, default='data/2025_new_coins_data.json')
    parser.add_argument('--real-data', type=str, default='data/real_data_all.json')
    parser.add_argument('--output', type=str, default='data/backtest_v4_hybrid.json')
    parser.add_argument('--threshold', type=float, default=6.0)
    parser.add_argument('--max-klines', type=int, default=100)

    args = parser.parse_args()

    backtester = CompleteBacktesterV4Hybrid(
        entry_threshold=args.threshold,
        max_klines_per_symbol=args.max_klines
    )
    backtester.run_backtest(args.kline_data, args.real_data, args.output)
