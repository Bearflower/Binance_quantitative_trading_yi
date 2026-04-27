#!/usr/bin/env python3
"""
获取真实的OI/市值比和资金费率数据（修正版v2）
"""

import json
import requests
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional


class RealDataFetcher:
    """真实数据获取器"""

    def __init__(self):
        self.binance_api_base = "https://fapi.binance.com"
        self.coingecko_api_base = "https://api.coingecko.com/api/v3"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        print("✅ 真实数据获取器初始化完成")

    def get_open_interest(self, symbol: str) -> Optional[Dict[str, float]]:
        """获取持仓量（OI）和价格"""
        try:
            # 获取OI
            url = f"{self.binance_api_base}/fapi/v1/openInterest"
            params = {'symbol': symbol}

            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            oi = float(data.get('openInterest', 0))

            # 获取当前价格
            url2 = f"{self.binance_api_base}/fapi/v1/ticker/price"
            params2 = {'symbol': symbol}

            response2 = self.session.get(url2, params=params2, timeout=10)
            response2.raise_for_status()

            data2 = response2.json()
            price = float(data2.get('price', 0))

            # 计算OI的美元价值
            oi_usd = oi * price

            return {
                'oi': oi,
                'price': price,
                'oi_usd': oi_usd
            }
        except Exception as e:
            print(f"  ❌ 获取 {symbol} OI失败: {e}")
            return None

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """获取资金费率"""
        try:
            url = f"{self.binance_api_base}/fapi/v1/fundingRate"
            params = {
                'symbol': symbol,
                'limit': 1
            }

            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            if data:
                funding_rate = float(data[0].get('fundingRate', 0))
                # 转换为年化费率（每8小时结算一次，一年365*24/8=1095次）
                annual_rate = funding_rate * 1095 * 100  # 转换为百分比
                return annual_rate

            return None
        except Exception as e:
            print(f"  ❌ 获取 {symbol} 资金费率失败: {e}")
            return None

    def get_market_cap(self, coin_id: str) -> Optional[float]:
        """从CoinGecko获取市值"""
        try:
            url = f"{self.coingecko_api_base}/coins/{coin_id}"
            params = {
                'localization': 'false',
                'tickers': 'false',
                'market_data': 'true',
                'community_data': 'false',
                'developer_data': 'false'
            }

            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()
            market_data = data.get('market_data', {})
            market_cap = market_data.get('market_cap', {}).get('usd', 0)

            return market_cap
        except Exception as e:
            print(f"  ❌ 获取 {coin_id} 市值失败: {e}")
            return None

    def get_coin_id_from_symbol(self, symbol: str) -> Optional[str]:
        """从交易对获取CoinGecko的coin_id"""
        # 移除USDT后缀
        coin_symbol = symbol.replace('USDT', '').replace('USDC', '').lower()

        # 常见的coin_id映射
        common_mappings = {
            'btc': 'bitcoin',
            'eth': 'ethereum',
            'bnb': 'binancecoin',
            'sol': 'solana',
            'xrp': 'ripple',
            'ada': 'cardano',
            'doge': 'dogecoin',
            'avax': 'avalanche-2',
            'dot': 'polkadot',
            'matic': 'matic-network',
            'link': 'chainlink',
            'uni': 'uniswap',
            'ltc': 'litecoin',
            'atom': 'cosmos',
            'etc': 'ethereum-classic',
            'near': 'near',
            'ftm': 'fantom',
            'algo': 'algorand',
            'xlm': 'stellar',
            'hbar': 'hedera-hashgraph',
            'fil': 'filecoin',
            'apt': 'aptos',
            'arb': 'arbitrum',
            'op': 'optimism',
            'sui': 'sui',
            'sei': 'sei-network',
            'tia': 'celestia',
            'wld': 'worldcoin-wld',
            'pepe': 'pepe',
            'bonk': 'bonk',
            'wif': 'dogwifcoin',
            'floki': 'floki',
            'shib': 'shiba-inu'
        }

        return common_mappings.get(coin_symbol, coin_symbol)

    def fetch_real_data_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """获取单个币种的真实数据"""
        print(f"\n获取 {symbol} 的真实数据...")

        result = {
            'symbol': symbol,
            'timestamp': datetime.now().isoformat(),
            'oi': None,
            'price': None,
            'oi_usd': None,
            'market_cap': None,
            'oi_ratio': None,
            'funding_rate': None
        }

        # 获取OI和价格
        oi_data = self.get_open_interest(symbol)
        if oi_data is not None:
            result['oi'] = oi_data['oi']
            result['price'] = oi_data['price']
            result['oi_usd'] = oi_data['oi_usd']
            print(f"  ✅ OI: {oi_data['oi']:,.2f} ({symbol.replace('USDT', '').replace('USDC', '')})")
            print(f"  ✅ 价格: ${oi_data['price']:,.4f}")
            print(f"  ✅ OI价值: ${oi_data['oi_usd']:,.2f}")

        # 获取资金费率
        funding_rate = self.get_funding_rate(symbol)
        if funding_rate is not None:
            result['funding_rate'] = funding_rate
            print(f"  ✅ 资金费率: {funding_rate:.2f}%")

        # 获取市值
        coin_id = self.get_coin_id_from_symbol(symbol)
        market_cap = self.get_market_cap(coin_id)
        if market_cap is not None:
            result['market_cap'] = market_cap
            print(f"  ✅ 市值: ${market_cap:,.2f}")

            # 计算OI/市值比
            if oi_data is not None and market_cap > 0:
                oi_ratio = oi_data['oi_usd'] / market_cap
                result['oi_ratio'] = oi_ratio
                print(f"  ✅ OI/市值比: {oi_ratio:.4f}")

        # 延迟，避免请求过快
        time.sleep(0.5)

        return result

    def fetch_real_data_for_symbols(self, symbols: list, output_file: str):
        """批量获取真实数据"""
        print(f"\n{'='*80}")
        print(f"开始获取真实数据")
        print(f"{'='*80}")
        print(f"\n币种数量: {len(symbols)}")

        results = {}

        for i, symbol in enumerate(symbols, 1):
            print(f"\n[{i}/{len(symbols)}] 处理 {symbol}")
            result = self.fetch_real_data_for_symbol(symbol)
            results[symbol] = result

            # 每10个币种保存一次
            if i % 10 == 0:
                print(f"\n保存中间结果...")
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)

        # 最终保存
        print(f"\n保存最终结果: {output_file}")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        print(f"\n✅ 完成")

        return results


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='获取真实数据')
    parser.add_argument('--symbols', type=str, help='币种列表（逗号分隔）')
    parser.add_argument('--input', type=str, help='输入文件（包含币种列表）')
    parser.add_argument('--output', type=str, default='data/real_data.json', help='输出文件')

    args = parser.parse_args()

    fetcher = RealDataFetcher()

    # 获取币种列表
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',')]
    elif args.input:
        with open(args.input, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # 处理嵌套的数据结构
            if isinstance(data, dict):
                if 'data' in data:
                    symbols = list(data['data'].keys())
                else:
                    symbols = list(data.keys())
            else:
                symbols = data
    else:
        print("❌ 请提供币种列表或输入文件")
        exit(1)

    # 过滤掉非交易对符号（如metadata）
    symbols = [s for s in symbols if 'USDT' in s or 'USDC' in s]

    print(f"\n过滤后的币种数量: {len(symbols)}")

    # 获取真实数据
    fetcher.fetch_real_data_for_symbols(symbols, args.output)
