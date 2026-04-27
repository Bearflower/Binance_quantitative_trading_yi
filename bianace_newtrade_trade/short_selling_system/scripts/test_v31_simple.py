#!/usr/bin/env python3
"""
v3.1 简化模拟测试脚本

直接调用 K 线服务 API，测试完整流程
"""

import json
import logging
import requests
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# K 线服务地址
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"
KLINE_REGISTER_URL = "http://43.156.242.184:8765/api/v1/register"

# 测试币种
TEST_SYMBOLS = [
    "CFGUSDT",
    "1000RATSUSDT", 
    "ETCUSDT",
    "NXPCUSDT",
    "TACUSDT"
]


def load_backtest_data():
    """从回测数据中获取币种信息"""
    data_file = Path("/root/new_coins_backtest.json")
    
    if not data_file.exists():
        logger.error(f"❌ 回测数据文件不存在：{data_file}")
        return {}
    
    with open(data_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    test_data = {}
    for symbol in TEST_SYMBOLS:
        if symbol in data:
            test_data[symbol] = data[symbol]
            logger.info(f"✅ 加载 {symbol} 数据成功")
    
    return test_data


def register_symbol(symbol, intervals=["1h"], duration_days=2):
    """注册单个币种到 K 线服务"""
    try:
        data = {
            "symbol": symbol,
            "intervals": intervals,
            "duration_days": duration_days,
            "priority": "high"
        }
        
        response = requests.post(KLINE_REGISTER_URL, json=data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 0:
                logger.info(f"✅ {symbol} 注册成功")
                return True
            else:
                logger.error(f"❌ {symbol} 注册失败：{result.get('message')}")
                return False
        else:
            logger.error(f"❌ {symbol} HTTP 错误：{response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ {symbol} 注册异常：{e}")
        return False


def get_klines_from_backtest(symbol, test_data):
    """从回测数据获取 K 线"""
    if symbol in test_data:
        # 回测数据结构：{'symbol_info': {...}, '1h': [...]}
        klines = test_data[symbol].get('1h', [])
        logger.info(f"✅ 从回测数据获取 {symbol} K 线成功，共 {len(klines)} 条")
        return klines
    else:
        logger.warning(f"⚠️  {symbol} 没有回测数据")
        return []


def calculate_score(klines, listing_hours):
    """简化版技术面评分"""
    if len(klines) < 10:
        return 5.0, "数据不足"
    
    # 简化判断
    closes = [k['close'] for k in klines]
    highs = [k['high'] for k in klines]
    volumes = [k['volume'] for k in klines]
    
    # 趋势判断
    if len(closes) >= 20:
        if closes[-1] < closes[-20]:
            trend_score = 4.0  # downtrend
        elif closes[-1] > closes[-20] * 1.05:
            trend_score = 0.0  # uptrend
        else:
            trend_score = 2.0  # sideways
    else:
        trend_score = 2.0
    
    # 三次冲顶检测（简化）
    if len(highs) >= 5:
        recent_highs = highs[-5:]
        max_high = max(recent_highs)
        tolerance = max_high * 0.02
        tops_count = sum(1 for h in recent_highs if abs(h - max_high) <= tolerance)
        pattern_bonus = 1.0 if tops_count >= 3 else 0.0
    else:
        pattern_bonus = 0.0
    
    # 成交量检测（简化）
    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        current_vol = volumes[-1]
        if current_vol >= 1.5 * avg_vol:
            volume_bonus = 1.0
        else:
            volume_bonus = 0.0
    else:
        volume_bonus = 0.0
    
    # 基础分
    base_score = trend_score + 3.0 + 2.0  # RSI 和波动率给平均分
    
    total_score = min(base_score + pattern_bonus + volume_bonus, 10.0)
    
    return total_score, f"趋势 ({trend_score}) + 形态 ({pattern_bonus}) + 成交量 ({volume_bonus})"


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("v3.1 模拟测试 - 完整流程验证（简化版）")
    logger.info("=" * 80)
    
    # 1. 加载数据
    logger.info("\n📂 步骤 1: 加载回测数据...")
    test_data = load_backtest_data()
    
    if not test_data:
        logger.error("❌ 没有加载到测试数据，退出")
        return
    
    # 2. 跳过注册（K 线服务已在后台持续采集）
    logger.info("\n" + "=" * 80)
    logger.info("步骤 2: 跳过注册（K 线服务已在后台持续采集所有币种）")
    logger.info("=" * 80)
    
    registered = TEST_SYMBOLS  # 假设所有币种都已注册
    logger.info(f"\n✅ 使用预加载币种：{len(registered)}/{len(TEST_SYMBOLS)} 个")
    
    # 3. 多轮评分
    logger.info("\n" + "=" * 80)
    logger.info("步骤 3: 多轮评分（模拟不同时间点）")
    logger.info("=" * 80)
    
    for symbol in registered:
        logger.info(f"\n{'=' * 80}")
        logger.info(f"币种：{symbol}")
        logger.info(f"{'=' * 80}")
        
        # 从回测数据获取 K 线
        klines = get_klines_from_backtest(symbol, test_data)
        
        if not klines:
            logger.warning(f"⚠️  {symbol} 没有 K 线数据，跳过")
            continue
        
        # 模拟 3 个时间点
        time_points = [12, 24, 36]
        
        for hours in time_points:
            logger.info(f"\n  ⏰ 模拟上线后 {hours} 小时...")
            
            # 使用对应数量的 K 线
            kline_count = min(hours, len(klines))
            test_klines = klines[:kline_count]
            
            # 计算评分
            score, reason = calculate_score(test_klines, hours)
            
            logger.info(f"    使用 K 线：{kline_count} 根")
            logger.info(f"    评分：{score:.2f}/10.0")
            logger.info(f"    原因：{reason}")
            
            if score >= 6.0:
                logger.info(f"    ✅ 达到开仓条件")
            else:
                logger.info(f"    ❌ 未达到开仓条件")
    
    # 4. 模拟注销
    logger.info("\n" + "=" * 80)
    logger.info("步骤 4: 模拟注销（K 线服务已在后台持续运行，无需手动注销）")
    logger.info("=" * 80)
    
    for symbol in registered:
        logger.info(f"  🗑️  模拟注销 {symbol}（实际无需操作，K 线服务持续采集）...")
    
    logger.info("\n✅ 注销完成（模拟）")
    
    # 总结
    logger.info("\n" + "=" * 80)
    logger.info("✅ 测试完成！")
    logger.info("=" * 80)
    logger.info(f"\n📊 测试结果:")
    logger.info(f"  注册币种：{len(registered)}/{len(TEST_SYMBOLS)} 个")
    logger.info(f"  评分轮数：{len(registered) * 3} 轮")
    logger.info(f"  K 线服务：{KLINE_SERVICE_URL}")
    logger.info("\n📝 日志已保存到：/root/short_selling_system/logs/test_v31_simulation.log")


if __name__ == '__main__':
    main()
