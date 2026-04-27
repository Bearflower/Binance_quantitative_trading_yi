#!/usr/bin/env python3
"""
v3.1 模拟测试脚本

模拟最近上线的 5 个币种，测试完整流程：
1. 注册到 K 线服务
2. 多轮评分
3. 注销
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
import sys

# 添加路径
script_dir = Path(__file__).parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from core.binance_client import binance_client
from core.listing_detector import NewListingDetector
from core.scoring_engine import ScoringEngine
from core.signal_manager import SignalManager
from core.technical_analyzer_v31 import technical_analyzer_v31

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 模拟的 5 个币种（使用回测数据中的）
TEST_SYMBOLS = [
    "CFGUSDT",
    "1000RATSUSDT",
    "ETCUSDT",
    "NXPCUSDT",
    "TACUSDT"
]

def load_backtest_data():
    """从回测数据中获取币种信息"""
    # 服务器路径
    data_file = Path("/root/new_coins_backtest.json")
    
    if not data_file.exists():
        logger.error(f"❌ 回测数据文件不存在：{data_file}")
        return {}
    
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 只取前 5 个币种
    test_data = {}
    for symbol in TEST_SYMBOLS:
        if symbol in data:
            test_data[symbol] = data[symbol]
            logger.info(f"✅ 加载 {symbol} 数据成功")
        else:
            logger.warning(f"⚠️  {symbol} 不在回测数据中")
    
    return test_data


def register_symbols(intervals=None):
    """注册 5 个币种到 K 线服务"""
    if intervals is None:
        intervals = ["1h"]  # 只采集 1 小时 K 线
    
    logger.info("=" * 80)
    logger.info("步骤 1: 注册币种到 K 线服务")
    logger.info("=" * 80)
    
    registered = []
    for symbol in TEST_SYMBOLS:
        logger.info(f"\n📝 注册 {symbol}...")
        
        # 模拟今天上线
        result = binance_client.register_new_symbol(
            symbol=symbol,
            intervals=intervals,
            duration_days=2,  # 采集 2 天
            priority="high"
        )
        
        if result:
            registered.append(symbol)
            logger.info(f"✅ {symbol} 注册成功")
        else:
            logger.error(f"❌ {symbol} 注册失败")
    
    logger.info(f"\n✅ 注册完成，成功 {len(registered)}/{len(TEST_SYMBOLS)} 个")
    return registered


def simulate_scoring(symbol, klines):
    """模拟多轮评分"""
    logger.info(f"\n{'=' * 80}")
    logger.info(f"步骤 2: 对 {symbol} 进行多轮评分（模拟不同时间点）")
    logger.info(f"{'=' * 80}")
    
    # 模拟 3 个时间点：上线后 12 小时、24 小时、36 小时
    time_points = [12, 24, 36]
    
    for hours in time_points:
        logger.info(f"\n⏰ 模拟上线后 {hours} 小时...")
        
        # 计算应该使用多少根 K 线
        kline_count = min(hours, len(klines))
        test_klines = klines[:kline_count]
        
        logger.info(f"  使用 K 线数量：{kline_count} 根")
        
        # 计算技术面评分
        score, details = technical_analyzer_v31.calculate_technical_score(
            symbol=symbol,
            klines=test_klines,
            listing_hours=hours
        )
        
        logger.info(f"  技术面评分：{score:.2f}/10.0")
        logger.info(f"  趋势：{details.get('trend', 'unknown')}")
        logger.info(f"  三次冲顶：{details.get('three_tops', False)}")
        logger.info(f"  量价背离：{details.get('volume_price_divergence', False)}")
        logger.info(f"  评分原因：{details.get('reason', '')}")
        
        # 判断是否应该开仓
        if score >= 6.0:
            logger.info(f"  ✅ 达到开仓条件（评分 ≥ 6.0）")
        else:
            logger.info(f"  ❌ 未达到开仓条件（评分 < 6.0）")


def unregister_symbols():
    """注销币种（模拟）"""
    logger.info(f"\n{'=' * 80}")
    logger.info("步骤 3: 注销币种（模拟）")
    logger.info(f"{'=' * 80}")
    
    logger.info("\n⚠️  K 线服务暂时无注销接口，模拟注销流程...")
    
    for symbol in TEST_SYMBOLS:
        logger.info(f"  🗑️  模拟注销 {symbol}...")
        # 实际应该调用 K 线服务的注销接口
        # binance_client.unregister_symbol(symbol)
    
    logger.info("\n✅ 注销完成（模拟）")


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("v3.1 模拟测试 - 完整流程验证")
    logger.info("=" * 80)
    
    # 1. 加载回测数据
    logger.info("\n📂 加载回测数据...")
    test_data = load_backtest_data()
    
    if not test_data:
        logger.error("❌ 没有加载到测试数据，退出")
        return
    
    # 2. 注册币种
    registered = register_symbols(intervals=["1h"])
    
    if not registered:
        logger.error("❌ 没有币种注册成功，退出")
        return
    
    # 3. 对每个币种进行多轮评分
    for symbol in registered:
        symbol_data = test_data.get(symbol, {})
        klines = symbol_data.get('1h', [])
        
        if not klines:
            logger.warning(f"⚠️  {symbol} 没有 K 线数据，跳过")
            continue
        
        simulate_scoring(symbol, klines)
    
    # 4. 注销币种
    unregister_symbols()
    
    logger.info("\n" + "=" * 80)
    logger.info("✅ 测试完成！")
    logger.info("=" * 80)


if __name__ == '__main__':
    main()
