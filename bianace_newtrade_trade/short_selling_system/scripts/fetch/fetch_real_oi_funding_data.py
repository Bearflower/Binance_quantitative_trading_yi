#!/usr/bin/env python3
"""
获取真实的OI/市值比和资金费率数据
从Binance API获取OI、价格、资金费率
从CoinGecko/CoinMarketCap获取市值数据
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import os

BASE_URL = "https://fapi.binance.com"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

async def fetch_with_retry(session: aiohttp.ClientSession, url: str, params: dict = None, max_retries: int = 3) -> Optional[dict]:
    for attempt in range(max_retries):
        try:
            async with session.get(url, params=params, timeout=30) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"请求失败 {url}: {e}")
                return None
            await asyncio.sleep(1)
    return None

async def get_exchange_info(session: aiohttp.ClientSession) -> Dict:
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    data = await fetch_with_retry(session, url)
    return data if data else {}

async def get_ticker_price(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/ticker/price"
    params = {"symbol": symbol}
    data = await fetch_with_retry(session, url, params)
    if data and "price" in data:
        return float(data["price"])
    return None

async def get_open_interest(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    data = await fetch_with_retry(session, url, params)
    if data and "openInterest" in data:
        return float(data["openInterest"])
    return None

async def get_funding_rate(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict]:
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    data = await fetch_with_retry(session, url, params)
    if data and len(data) > 0:
        return {
            "fundingRate": float(data[0]["fundingRate"]),
            "fundingTime": data[0]["fundingTime"]
        }
    return None

async def get_market_cap_coingecko(session: aiohttp.ClientSession, coin_id: str) -> Optional[float]:
    url = f"{COINGECKO_URL}/coins/{coin_id}"
    params = {"localization": "false", "tickers": "false", "market_data": "true", "community_data": "false"}
    data = await fetch_with_retry(session, url, params)
    if data and "market_data" in data:
        return data["market_data"].get("market_cap", {}).get("usd")
    return None

async def search_coin_coingecko(session: aiohttp.ClientSession, symbol: str) -> Optional[str]:
    url = f"{COINGECKO_URL}/search"
    params = {"query": symbol}
    data = await fetch_with_retry(session, url, params)
    if data and "coins" in data and len(data["coins"]) > 0:
        for coin in data["coins"]:
            if coin["symbol"].upper() == symbol.upper():
                return coin["id"]
        return data["coins"][0]["id"]
    return None

async def get_24h_volume(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    params = {"symbol": symbol}
    data = await fetch_with_retry(session, url, params)
    if data and "quoteVolume" in data:
        return float(data["quoteVolume"])
    return None

async def fetch_coin_data(session: aiohttp.ClientSession, symbol: str, base_asset: str) -> Dict:
    result = {
        "symbol": symbol,
        "base_asset": base_asset,
        "price": None,
        "oi": None,
        "oi_usd": None,
        "funding_rate": None,
        "market_cap": None,
        "oi_market_cap_ratio": None,
        "volume_24h": None,
        "coingecko_id": None,
        "fetch_time": datetime.now().isoformat()
    }
    
    price_task = get_ticker_price(session, symbol)
    oi_task = get_open_interest(session, symbol)
    funding_task = get_funding_rate(session, symbol)
    volume_task = get_24h_volume(session, symbol)
    
    price, oi, funding, volume = await asyncio.gather(
        price_task, oi_task, funding_task, volume_task
    )
    
    result["price"] = price
    result["oi"] = oi
    result["funding_rate"] = funding["fundingRate"] if funding else None
    result["volume_24h"] = volume
    
    if price and oi:
        result["oi_usd"] = oi * price
    
    coin_id = await search_coin_coingecko(session, base_asset)
    if coin_id:
        result["coingecko_id"] = coin_id
        await asyncio.sleep(0.5)
        market_cap = await get_market_cap_coingecko(session, coin_id)
        result["market_cap"] = market_cap
        
        if market_cap and result["oi_usd"]:
            result["oi_market_cap_ratio"] = result["oi_usd"] / market_cap
    
    return result

async def main():
    new_coins_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/2025_new_coins.json"
    with open(new_coins_file, "r") as f:
        new_coins = json.load(f)
    
    print(f"共有 {len(new_coins)} 个新币需要获取数据")
    
    results = []
    success_count = 0
    partial_count = 0
    failed_count = 0
    
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i, coin in enumerate(new_coins):
            symbol = coin["symbol"]
            base_asset = coin["baseAsset"]
            
            print(f"[{i+1}/{len(new_coins)}] 正在获取 {symbol} 数据...")
            
            try:
                data = await fetch_coin_data(session, symbol, base_asset)
                results.append(data)
                
                if data["market_cap"] and data["oi_market_cap_ratio"]:
                    success_count += 1
                    print(f"  ✓ 完整数据: 价格={data['price']}, OI={data['oi']}, 资金费率={data['funding_rate']}, 市值={data['market_cap']}, OI/市值={data['oi_market_cap_ratio']:.4f}")
                elif data["price"] or data["oi"] or data["funding_rate"]:
                    partial_count += 1
                    print(f"  △ 部分数据: 价格={data['price']}, OI={data['oi']}, 资金费率={data['funding_rate']}, 市值={data['market_cap']}")
                else:
                    failed_count += 1
                    print(f"  ✗ 获取失败")
                
                await asyncio.sleep(0.3)
                
            except Exception as e:
                print(f"  ✗ 错误: {e}")
                failed_count += 1
                results.append({
                    "symbol": symbol,
                    "base_asset": base_asset,
                    "error": str(e),
                    "fetch_time": datetime.now().isoformat()
                })
    
    output = {
        "metadata": {
            "fetch_time": datetime.now().isoformat(),
            "total_coins": len(new_coins),
            "success_count": success_count,
            "partial_count": partial_count,
            "failed_count": failed_count
        },
        "data": results
    }
    
    output_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n=== 数据获取完成 ===")
    print(f"完整数据: {success_count} 个")
    print(f"部分数据: {partial_count} 个")
    print(f"获取失败: {failed_count} 个")
    print(f"数据已保存到: {output_file}")
    
    valid_data = [r for r in results if r.get("oi_market_cap_ratio") is not None]
    if valid_data:
        ratios = [r["oi_market_cap_ratio"] for r in valid_data]
        funding_rates = [r["funding_rate"] for r in valid_data if r.get("funding_rate")]
        
        print(f"\n=== OI/市值比统计 ===")
        print(f"有效数据: {len(valid_data)} 个")
        print(f"最小值: {min(ratios):.4f}")
        print(f"最大值: {max(ratios):.4f}")
        print(f"平均值: {sum(ratios)/len(ratios):.4f}")
        print(f"中位数: {sorted(ratios)[len(ratios)//2]:.4f}")
        
        if funding_rates:
            print(f"\n=== 资金费率统计 ===")
            print(f"最小值: {min(funding_rates):.6f}")
            print(f"最大值: {max(funding_rates):.6f}")
            print(f"平均值: {sum(funding_rates)/len(funding_rates):.6f}")

if __name__ == "__main__":
    asyncio.run(main())
