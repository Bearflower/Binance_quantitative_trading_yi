#!/usr/bin/env python3
"""查询币安合约账户收益"""

import asyncio
import os
from binance.client import AsyncClient
from decimal import Decimal
from datetime import datetime


async def main():
    try:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        
        if not api_key or not api_secret:
            print("错误：未找到API密钥")
            return
        
        client = AsyncClient(api_key, api_secret)
        
        # 获取账户信息
        account = await client.futures_account()
        
        print("\n" + "="*60)
        print("币安合约账户收益统计")
        print("="*60)
        
        # 账户总览
        total_balance = float(account['totalWalletBalance'])
        available_balance = float(account['availableBalance'])
        unrealized_pnl = float(account['totalUnrealizedProfit'])
        
        print(f"\n【账户总览】")
        print(f"账户余额: {total_balance:.2f} USDT")
        print(f"可用余额: {available_balance:.2f} USDT")
        print(f"未实现盈亏: {unrealized_pnl:.4f} USDT")
        
        # 获取收入历史
        print(f"\n【最近收益统计】")
        income_history = await client.futures_income_history(limit=100)
        
        # 按类型统计
        total_realized = Decimal('0')
        total_commission = Decimal('0')
        total_funding = Decimal('0')
        
        # 按日期统计
        daily_pnl = {}
        
        for income in income_history:
            income_type = income['incomeType']
            amount = Decimal(income['income'])
            timestamp = int(income['time']) / 1000
            date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
            
            if income_type == 'REALIZED_PNL':
                total_realized += amount
                if date_str not in daily_pnl:
                    daily_pnl[date_str] = Decimal('0')
                daily_pnl[date_str] += amount
            elif income_type == 'COMMISSION':
                total_commission += amount
            elif income_type == 'FUNDING_FEE':
                total_funding += amount
        
        print(f"已实现盈亏: {float(total_realized):.4f} USDT")
        print(f"总手续费: {float(total_commission):.4f} USDT")
        print(f"资金费率: {float(total_funding):.4f} USDT")
        print(f"净盈亏: {float(total_realized + total_commission + total_funding):.4f} USDT")
        
        if total_realized != 0:
            fee_ratio = abs(total_commission / total_realized) * 100
            print(f"手续费占比: {float(fee_ratio):.2f}%")
        
        # 按日期显示
        if daily_pnl:
            print(f"\n【每日盈亏】")
            for date in sorted(daily_pnl.keys(), reverse=True)[:7]:
                pnl = daily_pnl[date]
                print(f"{date}: {float(pnl):.4f} USDT")
        
        # 获取当前持仓
        positions = await client.futures_position_information()
        
        print(f"\n【当前持仓】")
        has_position = False
        for pos in positions:
            amt = float(pos['positionAmt'])
            if amt != 0:
                has_position = True
                symbol = pos['symbol']
                entry_price = float(pos['entryPrice'])
                unrealized = float(pos['unRealizedProfit'])
                leverage = pos['leverage']
                
                print(f"{symbol}: {'做多' if amt > 0 else '做空'} {abs(amt)} @ {entry_price:.4f}")
                print(f"  未实现盈亏: {unrealized:.4f} USDT, 杠杆: {leverage}x")
        
        if not has_position:
            print("无持仓")
        
        # 总结
        print(f"\n【总结】")
        net_pnl = total_realized + total_commission + total_funding
        if net_pnl > 0:
            print(f"✅ 策略盈利: {float(net_pnl):.4f} USDT")
        elif net_pnl < 0:
            print(f"❌ 策略亏损: {float(net_pnl):.4f} USDT")
        else:
            print(f"➖ 盈亏平衡: {float(net_pnl):.4f} USDT")
        
        print("="*60 + "\n")
        
        await client.close_connection()
        
    except Exception as e:
        print(f"错误: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())
