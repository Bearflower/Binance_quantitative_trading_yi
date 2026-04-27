from core.binance_client import binance_client
import time

symbol = 'LOBSTERUSDT'

print(f"检查 {symbol} 的数据...\n")

# 1. 获取合约信息
info = binance_client.get_exchange_info()
symbols = info.get('symbols', [])

lobster_info = None
for s in symbols:
    if s.get('symbol') == symbol:
        lobster_info = s
        break

if lobster_info:
    print("✅ 找到 LOBSTERUSDT 合约")
    print(f"   状态：{lobster_info.get('status')}")
    print(f"   类型：{lobster_info.get('contractType')}")
    onboard = lobster_info.get('onboardDate', 0)
    if onboard:
        listing_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(onboard / 1000))
        print(f"   上市时间：{listing_time}")
    
    # 2. 获取资金费率
    try:
        funding_rate = binance_client.get_funding_rate(symbol)
        print(f"\n✅ 资金费率：{funding_rate}")
        if funding_rate:
            annual = funding_rate * 3 * 365 * 100
            print(f"   年化费率：{annual:.2f}%")
            if annual > 100:
                sentiment_score = 10.0
            elif annual > 50:
                sentiment_score = 7.0
            elif annual > 20:
                sentiment_score = 5.0
            else:
                sentiment_score = 3.0
            print(f"   情绪面评分：{sentiment_score}")
    except Exception as e:
        print(f"❌ 无法获取资金费率：{e}")
        print(f"   情绪面评分：5.0 (默认)")
    
    # 3. 获取 OI 数据
    try:
        oi_usd = binance_client.get_current_open_interest(symbol)
        print(f"\n✅ 持仓量 (OI): {oi_usd} USDT")
    except Exception as e:
        print(f"❌ 无法获取 OI 数据：{e}")
        print(f"   合约数据评分：5.0 (默认)")
else:
    print(f"❌ 未找到 {symbol} 合约")
    print("\n最近上市的 5 个合约:")
    recent = [s for s in symbols if s.get('onboardDate', 0) > 0]
    recent.sort(key=lambda x: x.get('onboardDate'), reverse=True)
    for s in recent[:5]:
        listing_time = time.strftime('%Y-%m-%d', time.localtime(s.get('onboardDate') / 1000))
        print(f"  {s.get('symbol')}: {listing_time}")
