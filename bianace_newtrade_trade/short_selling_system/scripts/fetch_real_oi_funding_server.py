#!/usr/bin/env python3
"""
获取真实的OI/市值比和资金费率数据
在服务器上运行，避免本地网络问题
"""

import requests
import json
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import sys

BASE_URL = "https://fapi.binance.com"
COINGECKO_URL = "https://api.coingecko.com/api/v3"

def log(msg):
    print(msg, flush=True)

def make_request(url: str, params: dict = None, max_retries: int = 3) -> Optional[dict]:
    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                time.sleep(2 ** attempt)
            else:
                return None
        except Exception as e:
            if attempt == max_retries - 1:
                return None
            time.sleep(1)
    return None

def get_ticker_price(symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/ticker/price"
    params = {"symbol": symbol}
    data = make_request(url, params)
    if data and "price" in data:
        return float(data["price"])
    return None

def get_open_interest(symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    data = make_request(url, params)
    if data and "openInterest" in data:
        return float(data["openInterest"])
    return None

def get_funding_rate(symbol: str) -> Optional[Dict]:
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    data = make_request(url, params)
    if data and len(data) > 0:
        return {
            "fundingRate": float(data[0]["fundingRate"]),
            "fundingTime": data[0]["fundingTime"]
        }
    return None

def get_24h_volume(symbol: str) -> Optional[float]:
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    params = {"symbol": symbol}
    data = make_request(url, params)
    if data and "quoteVolume" in data:
        return float(data["quoteVolume"])
    return None

def search_coin_coingecko(symbol: str) -> Optional[str]:
    url = f"{COINGECKO_URL}/search"
    params = {"query": symbol}
    data = make_request(url, params)
    if data and "coins" in data and len(data["coins"]) > 0:
        for coin in data["coins"]:
            if coin["symbol"].upper() == symbol.upper():
                return coin["id"]
        return data["coins"][0]["id"]
    return None

def get_market_cap_coingecko(coin_id: str) -> Optional[float]:
    url = f"{COINGECKO_URL}/coins/{coin_id}"
    params = {"localization": "false", "tickers": "false", "market_data": "true", "community_data": "false"}
    data = make_request(url, params)
    if data and "market_data" in data:
        return data["market_data"].get("market_cap", {}).get("usd")
    return None

def fetch_coin_data(symbol: str, base_asset: str) -> Dict:
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
    
    result["price"] = get_ticker_price(symbol)
    result["oi"] = get_open_interest(symbol)
    
    funding = get_funding_rate(symbol)
    result["funding_rate"] = funding["fundingRate"] if funding else None
    
    result["volume_24h"] = get_24h_volume(symbol)
    
    if result["price"] and result["oi"]:
        result["oi_usd"] = result["oi"] * result["price"]
    
    coin_id = search_coin_coingecko(base_asset)
    if coin_id:
        result["coingecko_id"] = coin_id
        time.sleep(0.5)
        market_cap = get_market_cap_coingecko(coin_id)
        result["market_cap"] = market_cap
        
        if market_cap and result["oi_usd"]:
            result["oi_market_cap_ratio"] = result["oi_usd"] / market_cap
    
    return result

def main():
    new_coins_file = "/root/bianace_newtrade_trade/short_selling_system/data/2025_new_coins.json"
    
    with open(new_coins_file, "r") as f:
        new_coins = json.load(f)
    
    log(f"共有 {len(new_coins)} 个新币需要获取数据")
    log(f"开始时间: {datetime.now().isoformat()}")
    
    results = []
    success_count = 0
    partial_count = 0
    failed_count = 0
    
    for i, coin in enumerate(new_coins):
        symbol = coin["symbol"]
        base_asset = coin["baseAsset"]
        
        log(f"[{i+1}/{len(new_coins)}] 正在获取 {symbol} 数据...")
        
        try:
            data = fetch_coin_data(symbol, base_asset)
            results.append(data)
            
            if data["market_cap"] and data["oi_market_cap_ratio"]:
                success_count += 1
                log(f"  ✓ 完整数据: 价格={data['price']}, OI={data['oi']}, 资金费率={data['funding_rate']}, 市值={data['market_cap']}, OI/市值={data['oi_market_cap_ratio']:.4f}")
            elif data["price"] or data["oi"] or data["funding_rate"]:
                partial_count += 1
                log(f"  △ 部分数据: 价格={data['price']}, OI={data['oi']}, 资金费率={data['funding_rate']}, 市值={data['market_cap']}")
            else:
                failed_count += 1
                log(f"  ✗ 获取失败")
            
            time.sleep(0.3)
            
            if (i + 1) % 50 == 0:
                log(f"=== 进度: {i+1}/{len(new_coins)}, 完整={success_count}, 部分={partial_count}, 失败={failed_count} ===")
            
        except Exception as e:
            log(f"  ✗ 错误: {e}")
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
    
    output_file = "/root/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    log(f"\n=== 数据获取完成 ===")
    log(f"结束时间: {datetime.now().isoformat()}")
    log(f"完整数据: {success_count} 个")
    log(f"部分数据: {partial_count} 个")
    log(f"获取失败: {failed_count} 个")
    log(f"数据已保存到: {output_file}")
    
    valid_data = [r for r in results if r.get("oi_market_cap_ratio") is not None]
    if valid_data:
        ratios = [r["oi_market_cap_ratio"] for r in valid_data]
        funding_rates = [r["funding_rate"] for r in valid_data if r.get("funding_rate")]
        
        log(f"\n=== OI/市值比统计 ===")
        log(f"有效数据: {len(valid_data)} 个")
        log(f"最小值: {min(ratios):.4f}")
        log(f"最大值: {max(ratios):.4f}")
        log(f"平均值: {sum(ratios)/len(ratios):.4f}")
        log(f"中位数: {sorted(ratios)[len(ratios)//2]:.4f}")
        
        if funding_rates:
            log(f"\n=== 资金费率统计 ===")
            log(f"最小值: {min(funding_rates):.6f}")
            log(f"最大值: {max(funding_rates):.6f}")
            log(f"平均值: {sum(funding_rates)/len(funding_rates):.6f}")

if __name__ == "__main__":
    main()
