#!/usr/bin/env python3
"""PM 账户余额查询测试"""

from core.binance_trading_api import binance_trading_api

print("="*60)
print("PM 账户余额查询测试")
print("="*60)

print(f"\nPM 账户模式：{binance_trading_api.is_pm_account}")

print("\n查询余额...")
balance = binance_trading_api.get_account_balance()

print(f"获取到 {len(balance)} 个资产")

# 显示 USDT 余额
for b in balance:
    if b['asset'] == 'USDT':
        print(f"\nUSDT 余额详情:")
        print(f"  钱包余额：{b['walletBalance']}")
        print(f"  可用余额：{b['availableBalance']}")
        print(f"  未实现盈亏：{b['unrealizedProfit']}")
        print(f"  交叉钱包余额：{b.get('crossWalletBalance', 'N/A')}")
        break

print("\n" + "="*60)
print("测试完成")
print("="*60)
