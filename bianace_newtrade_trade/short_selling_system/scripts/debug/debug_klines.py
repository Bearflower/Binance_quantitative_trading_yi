from core.binance_client import binance_client

symbol = 'COPPERUSDT'

print(f"\n测试获取 {symbol} 的 K 线数据...\n")

# 测试 1：直接调用 API
print("1️⃣  直接调用币安 API...")
url = f"{binance_client.futures_base_url}/fapi/v1/klines"
params = {
    "symbol": symbol,
    "interval": "1h",
    "limit": 200
}
print(f"   URL: {url}")
print(f"   参数：{params}")

import requests
try:
    response = requests.get(url, params=params, timeout=10)
    print(f"   状态码：{response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"   ✅ 成功获取 {len(data)} 条 K 线数据")
        if data:
            print(f"   第一条：{data[0]}")
            print(f"   最后一条：{data[-1]}")
    else:
        print(f"   ❌ 失败：{response.text}")
except Exception as e:
    print(f"   ❌ 错误：{e}")

# 测试 2：使用 binance_client 方法
print(f"\n2️⃣  使用 binance_client.get_kline_data()...")
klines = binance_client.get_kline_data(symbol, interval='1h', limit=200)
if klines:
    print(f"   ✅ 成功获取 {len(klines)} 条 K 线数据")
    if klines:
        print(f"   第一条：{klines[0]}")
        print(f"   最后一条：{klines[-1]}")
else:
    print(f"   ❌ 获取失败")

# 测试 3：检查币种是否有效
print(f"\n3️⃣  检查 {symbol} 是否有效...")
info = binance_client.get_exchange_info()
if info:
    symbols = info.get('symbols', [])
    valid_symbols = [s for s in symbols if s.get('symbol') == symbol]
    if valid_symbols:
        print(f"   ✅ {symbol} 是有效的交易对")
        print(f"   状态：{valid_symbols[0].get('status')}")
        print(f"   类型：{valid_symbols[0].get('contractType')}")
    else:
        print(f"   ❌ {symbol} 不是有效的交易对")
        # 显示所有包含 COPPER 的币种
        copper_symbols = [s for s in symbols if 'COPPER' in s.get('symbol', '')]
        if copper_symbols:
            print(f"   找到包含 COPPER 的交易对：")
            for s in copper_symbols:
                print(f"     - {s.get('symbol')}")
