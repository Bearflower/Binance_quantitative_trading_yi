#!/usr/bin/env python3
"""
测试账户余额修复脚本

验证统一交易账户（Portfolio Margin）的可用保证金和净资产是否正确区分
"""

import sys
from pathlib import Path
from decimal import Decimal

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from utils.binance_trade_api import BinanceTradeAPI

def test_balance_query():
    """测试余额查询"""
    print("=" * 80)
    print("测试账户余额查询")
    print("=" * 80)
    
    # 初始化 API
    api = BinanceTradeAPI()
    
    try:
        # 1. 测试 get_umfut_balance
        print("\n1. 测试 get_umfut_balance('USDT'):")
        available_balance = api.get_umfut_balance('USDT')
        print(f"   可用保证金：{available_balance} USDT")
        
        # 2. 测试 futures_account
        print("\n2. 测试 futures_account():")
        account_info = api.futures_account()
        
        for asset in account_info.get('assets', []):
            if asset.get('asset') == 'USDT':
                wallet_balance = Decimal(asset.get('walletBalance', '0'))
                available_from_account = Decimal(asset.get('availableBalance', '0'))
                cross_margin_free = Decimal(asset.get('crossMarginFree', '0'))
                cross_wallet_balance = Decimal(asset.get('crossWalletBalance', '0'))
                
                print(f"   钱包余额 (walletBalance): {wallet_balance} USDT")
                print(f"   可用余额 (availableBalance): {available_from_account} USDT")
                print(f"   跨仓可用 (crossMarginFree): {cross_margin_free} USDT")
                print(f"   跨仓钱包 (crossWalletBalance): {cross_wallet_balance} USDT")
                break
        
        # 3. 对比分析
        print("\n3. 对比分析:")
        print(f"   get_umfut_balance 返回：{available_balance} USDT")
        print(f"   差异：{abs(available_balance - available_from_account)} USDT")
        
        # 4. 判断是否正确
        print("\n4. 验证结果:")
        if available_balance <= wallet_balance:
            print(f"   ✅ 可用保证金 ({available_balance}) <= 净资产 ({wallet_balance})，逻辑正确")
        else:
            print(f"   ❌ 可用保证金 ({available_balance}) > 净资产 ({wallet_balance})，逻辑错误！")
        
        # 5. 检查持仓
        print("\n5. 当前持仓:")
        positions = account_info.get('positions', [])
        active_positions = [p for p in positions if Decimal(p.get('positionAmt', '0')) != 0]
        
        if active_positions:
            print(f"   共有 {len(active_positions)} 个持仓")
            total_margin = Decimal('0')
            for pos in active_positions[:5]:  # 只显示前 5 个
                symbol = pos.get('symbol')
                margin = Decimal(pos.get('positionInitialMargin', '0'))
                pnl = Decimal(pos.get('unrealizedProfit', '0'))
                print(f"   - {symbol}: 占用保证金 {margin} USDT, 未实现盈亏 {pnl} USDT")
                total_margin += margin
            
            if len(active_positions) > 5:
                print(f"   ... 还有 {len(active_positions) - 5} 个持仓")
            
            print(f"   总占用保证金：{total_margin} USDT")
            print(f"   验证：净资产 - 总占用 ≈ {wallet_balance - total_margin} USDT")
        else:
            print("   无持仓")
        
        print("\n" + "=" * 80)
        print("测试完成")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ 测试失败：{str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_balance_query()
