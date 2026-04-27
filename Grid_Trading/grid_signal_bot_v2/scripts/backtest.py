"""
网格交易信号灯系统 V2.0 回测模块（支持本地数据）
获取历史K线数据，模拟信号生成，分析策略表现
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.market_analyzer import MarketStateAnalyzer, MarketState
from src.core.grid_calculator import GridParameterCalculator
from src.core.parameter_comparator import ParameterComparator
from src.data.kline_client import KlineServiceClient
from src.utils.indicators import TechnicalIndicators
from src.utils.config import ConfigManager

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s'
)
logger = logging.getLogger(__name__)


class BacktestEngine:
    """回测引擎"""
    
    def __init__(self, kline_service_url: str = None):
        """
        初始化回测引擎
        
        Args:
            kline_service_url: K线服务地址（可选，如果使用本地数据则不需要）
        """
        # K线服务地址
        self.kline_service_url = kline_service_url
        
        # 加载配置
        self.config = ConfigManager()
        
        # 初始化核心模块
        strategy_config = self.config.get_strategy_config()
        indicators_config = strategy_config.get('indicators', {})
        
        self.market_analyzer = MarketStateAnalyzer(
            adx_period=indicators_config.get('adx_period', 14),
            adx_weak_threshold=indicators_config.get('adx_weak_threshold', 20),
            adx_trend_threshold=indicators_config.get('adx_trend_threshold', 25),
            adx_strong_threshold=indicators_config.get('adx_strong_threshold', 40),
            ema_fast_period=indicators_config.get('ema_fast_period', 20),
            ema_slow_period=indicators_config.get('ema_slow_period', 50)
        )
        
        trading_config = self.config.get_trading_config()
        grid_config = strategy_config.get('grid', {})
        grid_width_config = strategy_config.get('grid_width', {})
        
        self.grid_calculator = GridParameterCalculator(
            base_grid_count=grid_config.get('base_grid_count', 20),
            min_grid_count=grid_config.get('min_grid_count', 5),
            max_grid_count=grid_config.get('max_grid_count', 50),
            min_profit_rate=grid_config.get('min_profit_rate', 0.01),
            leverage=trading_config.get('leverage', 10),
            total_investment=trading_config.get('total_investment', 500),
            ranging_upper=grid_width_config.get('ranging_upper', 3.0),
            ranging_lower=grid_width_config.get('ranging_lower', 3.0),
            uptrend_upper=grid_width_config.get('uptrend_upper', 4.0),
            uptrend_lower=grid_width_config.get('uptrend_lower', 1.5),
            downtrend_upper=grid_width_config.get('downtrend_upper', 1.5),
            downtrend_lower=grid_width_config.get('downtrend_lower', 4.0)
        )
        
        triggers_config = self.config.get('triggers', {})
        
        self.parameter_comparator = ParameterComparator(
            grid_width_change_threshold=triggers_config.get('grid_width_change', 0.05),
            grid_count_change_threshold=triggers_config.get('grid_count_change', 0.10),
            atr_change_threshold=triggers_config.get('atr_change', 0.20),
            profit_rate_warning_threshold=triggers_config.get('profit_rate_warning', 0.012)
        )
        
        # 回测结果
        self.signals = []
        self.market_states = []
        self.grid_params_history = []
        
    def load_local_data(self, data_file: str) -> Dict[str, List[Dict]]:
        """
        从本地文件加载历史数据
        
        Args:
            data_file: 数据文件路径
            
        Returns:
            K线数据字典
        """
        logger.info(f"📂 从本地文件加载历史数据：{data_file}")
        
        if not os.path.exists(data_file):
            logger.error(f"文件不存在：{data_file}")
            return {}
        
        with open(data_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        logger.info(f"✅ 加载了 {len(data)} 根K线数据")
        
        return {"1h": data, "4h": data}  # 简化处理，使用相同数据
    
    def fetch_historical_data(
        self,
        symbol: str = "BTCUSDT",
        limit: int = 100
    ) -> Dict[str, List[Dict]]:
        """
        获取历史K线数据
        
        Args:
            symbol: 交易对
            limit: 获取数量（最大100）
            
        Returns:
            K线数据字典 {interval: klines}
        """
        logger.info(f"📊 获取 {symbol} 最近 {limit} 根K线数据...")
        
        kline_client = KlineServiceClient(
            base_url=self.kline_service_url,
            timeout=30
        )
        
        # 获取K线数据（API限制最大100根）
        klines_1h = kline_client.get_latest_klines(
            symbol=symbol,
            interval="1h",
            limit=min(limit, 100)
        )
        
        klines_4h = kline_client.get_latest_klines(
            symbol=symbol,
            interval="4h",
            limit=min(limit, 100)
        )
        
        logger.info(f"✅ 获取到 {len(klines_1h)} 根 1H K线")
        logger.info(f"✅ 获取到 {len(klines_4h)} 根 4H K线")
        
        kline_client.close()
        
        return {
            "1h": klines_1h,
            "4h": klines_4h
        }
    
    def run_backtest(
        self,
        klines_data: Dict[str, List[Dict]],
        symbol: str = "BTCUSDT"
    ) -> Dict[str, Any]:
        """
        运行回测
        
        Args:
            klines_data: K线数据
            symbol: 交易对
            
        Returns:
            回测结果
        """
        logger.info("=" * 60)
        logger.info("🔄 开始回测...")
        logger.info("=" * 60)
        
        klines_1h = klines_data["1h"]
        klines_4h = klines_data.get("4h", klines_1h)  # 如果没有4H数据，使用1H数据
        
        if not klines_1h:
            logger.error("❌ 没有K线数据")
            return {}
        
        # 初始化状态
        prev_params = None
        prev_atr = None
        prev_state = None
        
        # 统计数据
        state_distribution = {
            MarketState.RANGING.value: 0,
            MarketState.UPTREND.value: 0,
            MarketState.DOWNTREND.value: 0,
            MarketState.STRONG_TREND.value: 0
        }
        
        signal_count = 0
        strong_trend_count = 0
        
        # 遍历每根K线（每小时检查一次）
        start_index = 50  # 从第50根开始，确保有足够的历史数据
        for i in range(start_index, len(klines_1h)):
            # 获取当前K线索引对应的4H K线
            kline_1h_slice = klines_1h[max(0, i-49):i+1]
            
            # 找到对应的4H K线
            current_time = klines_1h[i].get('open_time', 0)
            kline_4h_slice = []
            for k4h in klines_4h:
                k4h_time = k4h.get('open_time', 0)
                if k4h_time <= current_time:
                    kline_4h_slice.append(k4h)
                else:
                    break
            
            # 只保留最近的50根4H K线
            kline_4h_slice = kline_4h_slice[-50:] if len(kline_4h_slice) > 50 else kline_4h_slice
            
            try:
                # 为当前K线计算技术指标（使用历史数据）
                indicators_1h = TechnicalIndicators.calculate_all_indicators(kline_1h_slice)
                indicators_4h = TechnicalIndicators.calculate_all_indicators(kline_4h_slice) if kline_4h_slice else {}
                
                # 将技术指标添加到最后一条K线
                if kline_1h_slice:
                    kline_1h_slice[-1]['adx'] = indicators_1h.get('adx', 0)
                    kline_1h_slice[-1]['ema_fast'] = indicators_1h.get('ema_fast', 0)
                    kline_1h_slice[-1]['ema_slow'] = indicators_1h.get('ema_slow', 0)
                    kline_1h_slice[-1]['atr'] = indicators_1h.get('atr', 0)
                
                if kline_4h_slice:
                    kline_4h_slice[-1]['adx'] = indicators_4h.get('adx', 0)
                    kline_4h_slice[-1]['ema_fast'] = indicators_4h.get('ema_fast', 0)
                    kline_4h_slice[-1]['ema_slow'] = indicators_4h.get('ema_slow', 0)
                    kline_4h_slice[-1]['atr'] = indicators_4h.get('atr', 0)
                
                # 分析市场状态
                market_result = self.market_analyzer.analyze(
                    klines_1h=kline_1h_slice,
                    klines_4h=kline_4h_slice
                )
                
                # 更新状态分布
                state_distribution[market_result.state.value] += 1
                
                # 获取当前价格和ATR
                current_price = klines_1h[i].get('close_price', 0)
                atr = indicators_1h.get('atr', current_price * 0.01)
                
                # 计算网格参数
                grid_params = self.grid_calculator.calculate(
                    current_price=current_price,
                    atr_smooth=atr,
                    market_state=market_result.state,
                    trend_strength=market_result.trend_strength
                )
                
                # 判断是否需要推送信号
                market_state_changed = (
                    prev_state is not None and
                    prev_state != market_result.state
                )
                
                changes = self.parameter_comparator.compare(
                    old_params=prev_params,
                    new_params=grid_params,
                    old_atr=prev_atr,
                    new_atr=atr
                )
                
                should_notify = self.parameter_comparator.should_notify(
                    changes=changes,
                    market_state_changed=market_state_changed
                )
                
                # 记录信号
                if should_notify or prev_params is None:
                    signal_count += 1
                    
                    signal = {
                        'time': datetime.fromtimestamp(current_time / 1000).strftime('%Y-%m-%d %H:%M'),
                        'price': current_price,
                        'state': market_result.state.value,
                        'adx': market_result.adx,
                        'atr': atr,
                        'grid_params': grid_params.to_dict(),
                        'reason': '首次运行' if prev_params is None else (
                            '市场状态变化' if market_state_changed else '参数显著变化'
                        )
                    }
                    
                    self.signals.append(signal)
                    
                    if market_result.state == MarketState.STRONG_TREND:
                        strong_trend_count += 1
                
                # 更新状态
                prev_params = grid_params
                prev_atr = atr
                prev_state = market_result.state
                
                # 记录市场状态
                self.market_states.append({
                    'time': datetime.fromtimestamp(current_time / 1000).strftime('%Y-%m-%d %H:%M'),
                    'state': market_result.state.value,
                    'adx': market_result.adx,
                    'trend_strength': market_result.trend_strength
                })
                
            except Exception as e:
                logger.error(f"处理K线 {i} 时出错：{e}")
                continue
        
        # 计算统计指标
        total_hours = len(klines_1h) - start_index
        signal_frequency = signal_count / (total_hours / 24) if total_hours > 0 else 0  # 平均每天信号数
        
        # 生成回测报告
        report = {
            'symbol': symbol,
            'period': {
                'start': datetime.fromtimestamp(klines_1h[start_index].get('open_time', 0) / 1000).strftime('%Y-%m-%d'),
                'end': datetime.fromtimestamp(klines_1h[-1].get('open_time', 0) / 1000).strftime('%Y-%m-%d'),
                'days': total_hours / 24
            },
            'statistics': {
                'total_hours': total_hours,
                'signal_count': signal_count,
                'signal_frequency': signal_frequency,
                'strong_trend_count': strong_trend_count,
                'state_distribution': state_distribution
            },
            'signals': self.signals[-50:],  # 只保留最近50个信号
            'market_states': self.market_states[-100:]  # 只保留最近100个状态
        }
        
        logger.info("=" * 60)
        logger.info("✅ 回测完成")
        logger.info("=" * 60)
        
        return report
    
    def generate_report(self, report: Dict[str, Any], output_file: str = None):
        """
        生成回测报告
        
        Args:
            report: 回测结果
            output_file: 输出文件路径
        """
        if not report:
            logger.error("❌ 没有回测结果")
            return
        
        print("\n" + "=" * 80)
        print("📊 网格交易信号灯系统 V2.0 回测报告")
        print("=" * 80)
        
        # 基本信息
        print(f"\n【回测周期】")
        print(f"交易对：{report['symbol']}")
        print(f"开始时间：{report['period']['start']}")
        print(f"结束时间：{report['period']['end']}")
        print(f"回测天数：{report['period']['days']:.1f} 天")
        
        # 统计数据
        stats = report['statistics']
        print(f"\n【信号统计】")
        print(f"总小时数：{stats['total_hours']}")
        print(f"信号总数：{stats['signal_count']}")
        print(f"信号频率：{stats['signal_frequency']:.2f} 次/天")
        print(f"强趋势暂停次数：{stats['strong_trend_count']}")
        
        # 市场状态分布
        print(f"\n【市场状态分布】")
        state_dist = stats['state_distribution']
        total_states = sum(state_dist.values())
        
        for state, count in state_dist.items():
            percentage = (count / total_states * 100) if total_states > 0 else 0
            print(f"{state:20s}: {count:5d} 次 ({percentage:5.1f}%)")
        
        # 最近信号
        print(f"\n【最近10个信号】")
        for i, signal in enumerate(report['signals'][-10:], 1):
            print(f"\n{i}. 时间：{signal['time']}")
            print(f"   价格：${signal['price']:,.2f}")
            print(f"   状态：{signal['state']}")
            print(f"   ADX：{signal['adx']:.2f}")
            print(f"   原因：{signal['reason']}")
        
        # 保存报告
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            print(f"\n✅ 报告已保存到：{output_file}")
        
        print("\n" + "=" * 80)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='网格交易信号灯系统回测')
    parser.add_argument('--symbol', type=str, default='BTCUSDT', help='交易对')
    parser.add_argument('--limit', type=int, default=100, help='K线数量（最大100）')
    parser.add_argument('--output', type=str, default='backtest_report.json', help='输出文件')
    parser.add_argument('--kline-url', type=str, default='http://43.156.242.184:8765', help='K线服务地址')
    parser.add_argument('--local-data', type=str, help='本地数据文件路径（优先使用）')
    
    args = parser.parse_args()
    
    # 创建回测引擎
    engine = BacktestEngine(kline_service_url=args.kline_url)
    
    # 获取历史数据
    if args.local_data and os.path.exists(args.local_data):
        # 使用本地数据
        klines_data = engine.load_local_data(args.local_data)
    else:
        # 从服务获取数据
        klines_data = engine.fetch_historical_data(
            symbol=args.symbol,
            limit=args.limit
        )
    
    # 运行回测
    report = engine.run_backtest(
        klines_data=klines_data,
        symbol=args.symbol
    )
    
    # 生成报告
    engine.generate_report(report, args.output)


if __name__ == "__main__":
    main()
