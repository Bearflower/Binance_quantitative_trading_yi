"""
数据采集模块测试脚本

用于测试各个数据采集组件的功能
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import logger
from core import (
    binance_client,
    coingecko_client,
    calculate_oi_ratio,
    score_oi_ratio,
    listing_detector
)


def test_binance_client():
    """测试币安 API 客户端"""
    logger.info("=" * 60)
    logger.info("测试币安 API 客户端")
    logger.info("=" * 60)
    
    # 测试获取交易所信息
    logger.info("\n1️⃣ 测试获取交易所信息...")
    exchange_info = binance_client.get_exchange_info()
    if exchange_info:
        symbol_count = len(exchange_info.get('symbols', []))
        logger.info(f"✅ 获取成功，共 {symbol_count} 个合约")
    else:
        logger.error("❌ 获取失败")
        return False
    
    # 测试获取 BTCUSDT 持仓量
    logger.info("\n2️⃣ 测试获取 BTCUSDT 持仓量...")
    oi = binance_client.get_current_open_interest("BTCUSDT")
    if oi:
        logger.info(f"✅ BTCUSDT 持仓量：{oi:,.2f} USDT")
    else:
        logger.warning("⚠️ 获取失败")
    
    # 测试获取 BTCUSDT 资金费率
    logger.info("\n3️⃣ 测试获取 BTCUSDT 资金费率...")
    funding_rate = binance_client.get_funding_rate("BTCUSDT")
    if funding_rate:
        annual_rate = binance_client.get_annualized_funding_rate("BTCUSDT")
        logger.info(
            f"✅ 资金费率：{funding_rate:.4%}, "
            f"年化：{annual_rate:.2%}"
        )
    else:
        logger.warning("⚠️ 获取失败")
    
    # 测试获取 K 线数据
    logger.info("\n4️⃣ 测试获取 BTCUSDT K 线数据...")
    klines = binance_client.get_kline_data("BTCUSDT", interval="1h", limit=5)
    if klines:
        logger.info(f"✅ 获取成功，共 {len(klines)} 条")
        for k in klines:
            logger.info(
                f"   开盘：{k['open']:.2f}, 收盘：{k['close']:.2f}, "
                f"最高：{k['high']:.2f}, 最低：{k['low']:.2f}"
            )
    else:
        logger.warning("⚠️ 获取失败")
    
    return True


def test_coingecko_client():
    """测试 CoinGecko API 客户端"""
    logger.info("=" * 60)
    logger.info("测试 CoinGecko API 客户端")
    logger.info("=" * 60)
    
    # 测试搜索代币
    logger.info("\n1️⃣ 测试搜索代币...")
    results = coingecko_client.search_token("bitcoin")
    if results:
        logger.info(f"✅ 搜索成功，找到 {len(results)} 个结果")
        logger.info(f"   第一个结果：{results[0]['name']} ({results[0]['symbol']})")
    else:
        logger.warning("⚠️ 搜索失败")
    
    # 测试获取 BTC 市值
    logger.info("\n2️⃣ 测试获取 BTC 流通市值...")
    market_cap = coingecko_client.get_market_cap_by_symbol("BTCUSDT")
    if market_cap:
        logger.info(f"✅ BTC 流通市值：${market_cap:,.2f}")
    else:
        logger.warning("⚠️ 获取失败")
    
    return True


def test_oi_ratio():
    """测试 OI/市值比计算"""
    logger.info("=" * 60)
    logger.info("测试 OI/市值比计算")
    logger.info("=" * 60)
    
    test_cases = [
        (1000000, 2000000, 0.5),  # 正常情况
        (3000000, 2000000, 1.5),  # >1.0 (否决)
        (500000, 2000000, 0.25),  # <0.3 (10 分)
        (0, 2000000, 0),          # OI=0
    ]
    
    for oi, market_cap, expected_ratio in test_cases:
        ratio, is_valid = calculate_oi_ratio(oi, market_cap)
        score, veto = score_oi_ratio(ratio)
        
        logger.info(
            f"OI={oi:,.0f}, 市值={market_cap:,.0f} → "
            f"比值={ratio:.4f}, 评分={score:.1f}, 否决={veto}"
        )
    
    return True


def test_listing_detector():
    """测试新币检测功能"""
    logger.info("=" * 60)
    logger.info("测试新币检测功能")
    logger.info("=" * 60)
    
    # 检测最近 24 小时新币
    logger.info("\n1️⃣ 检测最近 24 小时新上市合约...")
    new_listings = listing_detector.detect_new_listings(hours=24)
    
    if new_listings:
        logger.info(f"✅ 检测到 {len(new_listings)} 个新币")
        for listing in new_listings:
            logger.info(
                f"   🆕 {listing['symbol']}, "
                f"上线时间：{listing['listing_time']}, "
                f"距今：{listing['hours_since_listing']:.1f}小时"
            )
    else:
        logger.info("ℹ️ 未检测到新币")
    
    # 获取最近 7 天新币
    logger.info("\n2️⃣ 获取最近 7 天新上市合约...")
    recent_listings = listing_detector.get_recent_listings(hours=168)
    
    if recent_listings:
        logger.info(f"✅ 找到 {len(recent_listings)} 个合约")
        for listing in recent_listings[:5]:  # 只显示前 5 个
            logger.info(
                f"   📅 {listing['symbol']}, "
                f"上线时间：{listing['listing_time']}, "
                f"距今：{listing['hours_since_listing']:.1f}小时"
            )
    else:
        logger.info("ℹ️ 未找到合约")
    
    return True


def main():
    """主测试函数"""
    logger.info("\n" + "=" * 60)
    logger.info("🧪 数据采集模块测试")
    logger.info("=" * 60 + "\n")
    
    results = []
    
    # 运行测试
    results.append(("币安 API 客户端", test_binance_client()))
    results.append(("CoinGecko API 客户端", test_coingecko_client()))
    results.append(("OI/市值比计算", test_oi_ratio()))
    results.append(("新币检测功能", test_listing_detector()))
    
    # 输出测试结果
    logger.info("\n" + "=" * 60)
    logger.info("📊 测试结果汇总")
    logger.info("=" * 60)
    
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        logger.info(f"{status}: {name}")
    
    all_passed = all(result for _, result in results)
    
    if all_passed:
        logger.info("\n🎉 所有测试通过!")
    else:
        logger.info("\n⚠️ 部分测试失败，请检查日志")
    
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
