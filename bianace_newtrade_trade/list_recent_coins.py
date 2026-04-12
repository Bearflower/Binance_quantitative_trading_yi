from core.binance_client import binance_client

# 获取所有合约
info = binance_client.get_exchange_info()
symbols = info.get('symbols', [])

# 找出最近上市的 10 个币种
recent = [s for s in symbols if s.get('onboardDate', 0) > 0]
recent.sort(key=lambda x: x.get('onboardDate'), reverse=True)

print("最近上市的 10 个币种:")
for i, s in enumerate(recent[:10]):
    from datetime import datetime
    timestamp = s.get('onboardDate') / 1000
    listing_time = datetime.fromtimestamp(timestamp)
    print(f"{i+1}. {s.get('symbol')}: {listing_time}")