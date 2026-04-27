#!/usr/bin/env python3
"""
双数据源对比测试脚本

用于对比币安 API 和 K 线服务的数据差异：
- 每小时 20 分：使用币安 API 数据进行分析
- 每小时 25 分：使用 K 线服务数据进行分析

通过 5 分钟的时间差，对比两个数据源的数据一致性
"""

import logging
import sys
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any

# 导入两个数据源
from core.binance_data_fetcher import get_binance_data_fetcher
from core.data import get_data_fetcher

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('double_source_test')


def compare_data_sources(symbols=None):
    """
    对比币安 API 和 K 线服务的数据
    
    Args:
        symbols: 交易对列表，默认 ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    """
    if symbols is None:
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
    
    logger.info("=" * 80)
    logger.info("双数据源对比测试")
    logger.info(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"交易对：{symbols}")
    logger.info("=" * 80)
    
    # 1. 从币安 API 获取数据
    logger.info("\n1. 从币安 API 获取数据...")
    binance_fetcher = get_binance_data_fetcher()
    binance_data = binance_fetcher.fetch_market_data(symbols)
    logger.info(f"✅ 币安 API 成功获取 {len(binance_data)} 个交易对数据")
    
    # 2. 从 K 线服务获取数据
    logger.info("\n2. 从 K 线服务获取数据...")
    kline_fetcher = get_data_fetcher()
    kline_data = kline_fetcher.fetch_market_data(symbols)
    logger.info(f"✅ K 线服务成功获取 {len(kline_data)} 个交易对数据")
    
    # 3. 对比数据
    logger.info("\n3. 数据对比:")
    logger.info("=" * 80)
    
    all_results = {}
    
    for symbol in symbols:
        logger.info(f"\n{'='*80}")
        logger.info(f"交易对：{symbol}")
        logger.info(f"{'='*80}")
        
        if symbol not in binance_data:
            logger.error(f"❌ 币安 API 缺少 {symbol} 数据")
            continue
        
        if symbol not in kline_data:
            logger.error(f"❌ K 线服务缺少 {symbol} 数据")
            continue
        
        binance = binance_data[symbol]
        kline = kline_data[symbol]
        
        # 3.1 对比最新价格
        logger.info(f"\n3.1 价格对比:")
        binance_price = binance.get('last_price', Decimal('0'))
        kline_price = kline.get('last_price', Decimal('0'))
        price_diff = abs(binance_price - kline_price)
        price_diff_pct = (price_diff / binance_price * 100) if binance_price > 0 else Decimal('0')
        
        logger.info(f"  币安 API 价格：{binance_price}")
        logger.info(f"  K 线服务价格：{kline_price}")
        logger.info(f"  价格差异：{price_diff} ({price_diff_pct:.4f}%)")
        
        if price_diff_pct > Decimal('0.1'):
            logger.warning(f"  ⚠️ 价格差异过大！> 0.1%")
        else:
            logger.info(f"  ✅ 价格差异在合理范围内")
        
        # 3.2 对比技术指标（1h 周期）
        logger.info(f"\n3.2 技术指标对比 (1h 周期):")
        
        binance_indicators = binance.get('indicators', {}).get('1h', {})
        kline_indicators = kline.get('indicators', {}).get('1h', {})
        
        # EMA21 对比
        logger.info(f"\n  EMA21:")
        binance_ema21 = binance_indicators.get('ema21')
        kline_ema21 = kline_indicators.get('ema21')
        
        if binance_ema21 is not None and kline_ema21 is not None:
            ema_diff = abs(binance_ema21 - kline_ema21)
            ema_diff_pct = (ema_diff / binance_ema21 * 100) if binance_ema21 > 0 else Decimal('0')
            logger.info(f"    币安 API: {binance_ema21}")
            logger.info(f"    K 线服务：{kline_ema21}")
            logger.info(f"    差异：{ema_diff} ({ema_diff_pct:.4f}%)")
            
            if ema_diff_pct > Decimal('1'):
                logger.warning(f"    ⚠️ EMA 差异较大！> 1%")
            else:
                logger.info(f"    ✅ EMA 差异在合理范围内")
        else:
            logger.warning(f"    ❌ EMA 数据缺失 - 币安 API: {binance_ema21}, K 线服务：{kline_ema21}")
        
        # ATR14 对比
        logger.info(f"\n  ATR14:")
        binance_atr14 = binance_indicators.get('atr14')
        kline_atr14 = kline_indicators.get('atr14')
        
        if binance_atr14 is not None and kline_atr14 is not None:
            atr_diff = abs(binance_atr14 - kline_atr14)
            atr_diff_pct = (atr_diff / binance_atr14 * 100) if binance_atr14 > 0 else Decimal('0')
            logger.info(f"    币安 API: {binance_atr14}")
            logger.info(f"    K 线服务：{kline_atr14}")
            logger.info(f"    差异：{atr_diff} ({atr_diff_pct:.4f}%)")
            
            if atr_diff_pct > Decimal('5'):
                logger.warning(f"    ⚠️ ATR 差异较大！> 5%")
            else:
                logger.info(f"    ✅ ATR 差异在合理范围内")
        else:
            logger.warning(f"    ❌ ATR 数据缺失 - 币安 API: {binance_atr14}, K 线服务：{kline_atr14}")
        
        # RSI14 对比（重点关注）
        logger.info(f"\n  RSI14:")
        binance_rsi14 = binance_indicators.get('rsi')
        kline_rsi14 = kline_indicators.get('rsi')
        
        if binance_rsi14 is not None and kline_rsi14 is not None:
            rsi_diff = abs(binance_rsi14 - kline_rsi14)
            logger.info(f"    币安 API: {binance_rsi14}")
            logger.info(f"    K 线服务：{kline_rsi14}")
            logger.info(f"    差异：{rsi_diff}")
            
            if rsi_diff > Decimal('5'):
                logger.error(f"    ❌ RSI 差异过大！> 5")
            elif rsi_diff > Decimal('2'):
                logger.warning(f"    ⚠️ RSI 差异较大！> 2")
            else:
                logger.info(f"    ✅ RSI 差异在合理范围内")
        else:
            logger.warning(f"    ❌ RSI 数据缺失 - 币安 API: {binance_rsi14}, K 线服务：{kline_rsi14}")
        
        # 3.3 记录结果
        all_results[symbol] = {
            'price': {
                'binance': float(binance_price),
                'kline': float(kline_price),
                'diff': float(price_diff),
                'diff_pct': float(price_diff_pct)
            },
            'indicators': {
                'ema21': {
                    'binance': float(binance_ema21) if binance_ema21 else None,
                    'kline': float(kline_ema21) if kline_ema21 else None,
                },
                'atr14': {
                    'binance': float(binance_atr14) if binance_atr14 else None,
                    'kline': float(kline_atr14) if kline_atr14 else None,
                },
                'rsi14': {
                    'binance': float(binance_rsi14) if binance_rsi14 else None,
                    'kline': float(kline_rsi14) if kline_rsi14 else None,
                }
            }
        }
    
    # 4. 总结
    logger.info(f"\n{'='*80}")
    logger.info("对比总结")
    logger.info(f"{'='*80}")
    
    issues = []
    for symbol, result in all_results.items():
        symbol_issues = []
        
        # 价格差异
        if result['price']['diff_pct'] > 0.1:
            symbol_issues.append(f"价格差异过大：{result['price']['diff_pct']:.4f}%")
        
        # RSI 差异
        rsi_binance = result['indicators']['rsi14']['binance']
        rsi_kline = result['indicators']['rsi14']['kline']
        if rsi_binance is not None and rsi_kline is not None:
            rsi_diff = abs(rsi_binance - rsi_kline)
            if rsi_diff > 5:
                symbol_issues.append(f"RSI 差异过大：{rsi_diff}")
            elif rsi_diff > 2:
                symbol_issues.append(f"RSI 差异较大：{rsi_diff}")
        
        if symbol_issues:
            issues.append(f"{symbol}: {', '.join(symbol_issues)}")
    
    if issues:
        logger.error(f"\n❌ 发现问题:")
        for issue in issues:
            logger.error(f"  - {issue}")
    else:
        logger.info(f"\n✅ 数据一致性良好，所有差异在合理范围内")
    
    logger.info(f"\n{'='*80}\n")
    
    return all_results


if __name__ == "__main__":
    try:
        compare_data_sources()
    except KeyboardInterrupt:
        logger.info("\n测试被用户中断")
    except Exception as e:
        logger.error(f"测试失败：{str(e)}", exc_info=True)
        sys.exit(1)
