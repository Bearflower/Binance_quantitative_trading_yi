from core.binance_client import binance_client
import time

info = binance_client.get_exchange_info()
symbols = info.get("symbols", [])

# 找出最近上市的 5 个合约
recent = [s for s in symbols if s.get("onboardDate", 0) > 0]
recent.sort(key=lambda x: x.get("onboardDate"), reverse=True)

print("最近上市的 5 个合约:")
for s in recent[:5]:
    symbol = s.get("symbol", "Unknown")
    listing_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("onboardDate") / 1000))
    print(f"  {symbol}: {listing_time}")
    
    # 尝试获取资金费率
    try:
        funding_rate = binance_client.get_funding_rate(symbol)
        if funding_rate:
            annual = funding_rate * 3 * 365 * 100
            print(f"    资金费率：{funding_rate:.6f} (年化 {annual:.2f}%)")
    except:
        print(f"    资金费率：无法获取")
