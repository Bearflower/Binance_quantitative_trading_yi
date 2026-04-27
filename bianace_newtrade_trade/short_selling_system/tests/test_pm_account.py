#!/usr/bin/env python3
"""
PM 账户接口测试脚本
测试所有 PM 账户专用接口（/papi/v1/um/*）
"""

from short_selling_system.core.binance_trading_api import binance_trading_api
from short_selling_system.utils.logger import logger
import json

def test_pm_account():
    """测试 PM 账户接口"""
    print('='*60)
    print('测试 PM 账户接口（使用/papi/v1/um/* 端点）')
    print('='*60)
    
    # 测试 1: 账户余额查询
    print('\n[测试 1] 查询账户余额（PM 账户）')
    try:
        balance = binance_trading_api.get_account_balance()
        if balance:
            print(f'✅ 余额查询成功，共{len(balance)}个资产')
            # 显示 USDT 余额
            usdt_balance = next((b for b in balance if b.get('asset') == 'USDT'), None)
            if usdt_balance:
                print(f'   USDT 余额：')
                for key, value in usdt_balance.items():
                    if value and str(value).strip():
                        print(f'     {key}: {value}')
        else:
            print('❌ 余额查询失败 - 可能 API 密钥权限不足或未配置')
    except Exception as e:
        print(f'❌ 异常：{e}')
    
    # 测试 2: 持仓查询
    print('\n[测试 2] 查询持仓（PM 账户）')
    try:
        positions = binance_trading_api.get_position()
        if positions is not None:
            if len(positions) > 0:
                print(f'✅ 持仓查询成功，共{len(positions)}个持仓')
                for pos in positions[:5]:  # 只显示前 5 个
                    print(f'   {pos.get("symbol")}: {pos.get("positionAmt")} @ {pos.get("entryPrice")}')
            else:
                print('✅ 无持仓')
        else:
            print('❌ 持仓查询失败 - 可能 API 密钥权限不足或未配置')
    except Exception as e:
        print(f'❌ 异常：{e}')
    
    # 测试 3: 查询挂单
    print('\n[测试 3] 查询当前挂单（PM 账户）')
    try:
        open_orders = binance_trading_api.query_open_orders()
        if open_orders is not None:
            if len(open_orders) > 0:
                print(f'✅ 查询挂单成功，共{len(open_orders)}个')
                for order in open_orders[:3]:
                    print(f'   {order.get("symbol")}: {order.get("side")} {order.get("type")} @ {order.get("price")}')
            else:
                print('✅ 无挂单')
        else:
            print('❌ 查询挂单失败 - 可能 API 密钥权限不足或未配置')
    except Exception as e:
        print(f'❌ 异常：{e}')
    
    print('\n' + '='*60)
    print('PM 账户接口测试完成')
    print('='*60)
    print('\n📝 说明:')
    print('  - 如果显示"权限不足"或"Invalid API-key"，请检查:')
    print('    1. API 密钥是否正确配置在 .env 文件中')
    print('    2. API 密钥是否开启了"读取 + 交易"权限')
    print('    3. 是否添加了服务器 IP 到白名单')
    print('  - 所有接口都使用 PM 账户专用端点：/papi/v1/um/*')
    print('='*60)

if __name__ == '__main__':
    test_pm_account()
