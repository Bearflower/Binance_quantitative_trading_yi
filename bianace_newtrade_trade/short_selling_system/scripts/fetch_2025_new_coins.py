#!/usr/bin/env python3
"""
获取 2025 年所有新上线的永续合约
从币安获取合约信息，筛选出 2025 年上线的币种
"""

import requests
import json
from datetime import datetime
from pathlib import Path


def fetch_all_contracts() -> list:
    """从币安获取所有永续合约信息"""
    url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
    
    try:
        response = requests.get(url, timeout=30)
        data = response.json()
        
        if 'symbols' not in data:
            print("❌ 获取合约信息失败")
            return []
        
        contracts = []
        for symbol in data['symbols']:
            # 只获取永续合约
            if symbol.get('contractType') == 'PERPETUAL':
                contracts.append(symbol)
        
        print(f"✅ 获取到 {len(contracts)} 个永续合约")
        return contracts
    
    except Exception as e:
        print(f"❌ 请求失败：{e}")
        return []


def filter_2025_new_coins(contracts: list) -> list:
    """筛选 2025 年新上线的合约"""
    
    # 2025 年 1 月 1 日的 timestamp（毫秒）
    start_2025 = int(datetime(2025, 1, 1).timestamp() * 1000)
    # 当前时间
    now = int(datetime.now().timestamp() * 1000)
    
    new_coins = []
    
    for contract in contracts:
        # 优先使用 onboardDate，如果没有则使用 listTime
        onboard_date = contract.get('onboardDate', 0)
        list_time = contract.get('listTime', 0)
        
        # 使用可用的时间字段
        coin_time = onboard_date if onboard_date else list_time
        
        # 筛选 2025 年上线的
        if coin_time and coin_time >= start_2025 and coin_time <= now:
            # 计算上线天数
            days_online = (now - coin_time) / (1000 * 3600 * 24)
            
            new_coin = {
                'symbol': contract['symbol'],
                'baseAsset': contract.get('baseAsset', ''),
                'quoteAsset': contract.get('quoteAsset', ''),
                'onboardDate': onboard_date,
                'listTime': list_time,
                'listDate': datetime.fromtimestamp(coin_time / 1000).strftime('%Y-%m-%d'),
                'daysOnline': int(days_online),
                'status': contract.get('status', 'TRADING')
            }
            
            # 只保留交易中的
            if new_coin['status'] == 'TRADING':
                new_coins.append(new_coin)
    
    # 按上线时间排序（最新的在前）
    new_coins.sort(key=lambda x: x['onboardDate'] if x['onboardDate'] else x['listTime'], reverse=True)
    
    return new_coins


def save_new_coins_list(new_coins: list, output_path: str):
    """保存新币列表"""
    
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(new_coins, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 新币列表已保存到：{output}")


def main():
    print("=" * 80)
    print("获取 2025 年新上线永续合约")
    print("=" * 80)
    
    # 1. 获取所有合约
    print("\n正在获取所有永续合约...")
    all_contracts = fetch_all_contracts()
    
    if not all_contracts:
        return
    
    # 2. 筛选 2025 年新币
    print("\n筛选 2025 年新上线的合约...")
    new_coins = filter_2025_new_coins(all_contracts)
    
    print(f"\n✅ 找到 {len(new_coins)} 个 2025 年新上线的永续合约")
    
    # 3. 显示统计
    if new_coins:
        print("\n" + "=" * 80)
        print("新币统计")
        print("=" * 80)
        
        # 最早和最新
        earliest = new_coins[-1] if new_coins else None
        latest = new_coins[0] if new_coins else None
        
        if earliest:
            print(f"最早上线：{earliest['listDate']} ({earliest['symbol']}, {earliest['daysOnline']}天前)")
        if latest:
            print(f"最新上线：{latest['listDate']} ({latest['symbol']}, {latest['daysOnline']}天前)")
        
        # 按月统计
        month_stats = {}
        for coin in new_coins:
            month = coin['listDate'][:7]  # YYYY-MM
            month_stats[month] = month_stats.get(month, 0) + 1
        
        print(f"\n按月分布:")
        for month in sorted(month_stats.keys()):
            print(f"  {month}: {month_stats[month]} 个")
        
        # 显示前 20 个
        print(f"\n最新上线的 20 个币种:")
        for i, coin in enumerate(new_coins[:20], 1):
            print(f"  {i:2d}. {coin['symbol']:15} - {coin['listDate']} ({coin['daysOnline']}天前)")
        
        if len(new_coins) > 20:
            print(f"  ... 还有 {len(new_coins) - 20} 个")
    
    # 4. 保存列表
    save_new_coins_list(new_coins, 'data/2025_new_coins.json')
    
    # 5. 生成符号列表用于数据获取
    symbols = [coin['symbol'] for coin in new_coins]
    symbols_str = ','.join(symbols)
    
    print(f"\n" + "=" * 80)
    print(f"下一步：获取这 {len(symbols)} 个币种的历史数据")
    print("=" * 80)
    print(f"\n在服务器上运行:")
    print(f"cd /root/short_selling_system")
    print(f"python3 scripts/batch_fetch_data.py --symbols '{symbols_str}' --days 365")
    
    print(f"\n或者本地运行:")
    print(f"python3 scripts/fetch_data_from_server.py --key ~/.ssh/id_ed25519")
    
    print("\n" + "=" * 80)


if __name__ == '__main__':
    main()
