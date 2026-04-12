#!/usr/bin/env python3
"""
服务器环境完整功能测试脚本

注意：需要在 short_selling_system 目录下运行，或设置 PYTHONPATH
"""

import sys
import os
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

"""

测试阶段：
1. API 连接测试
2. 精度信息获取
3. 精度调整验证
4. 账户信息查询
5. 综合测试报告
"""

import sys
from datetime import datetime
from decimal import Decimal

# 添加测试统计
test_results = {
    'passed': 0,
    'failed': 0,
    'errors': []
}

def log_test(name, passed, details=""):
    """记录测试结果"""
    if passed:
        print(f"  ✅ {name}")
        test_results['passed'] += 1
    else:
        print(f"  ❌ {name}: {details}")
        test_results['failed'] += 1
        test_results['errors'].append(f"{name}: {details}")

print("="*80)
print("服务器环境完整功能测试")
print("="*80)
print(f"测试时间：{datetime.now()}")
print(f"Python 版本：{sys.version}")
print("="*80)

# ============================================================================
# 第一阶段：API 连接测试
# ============================================================================
print("\n" + "="*80)
print("第一阶段：API 连接测试")
print("="*80)

try:
    from core.binance_trading_api import BinanceTradingAPI, binance_trading_api
    log_test("币安交易 API 模块导入", True)
except Exception as e:
    log_test("币安交易 API 模块导入", False, str(e))
    print(f"\n❌ 关键模块导入失败，无法继续测试：{e}")
    sys.exit(1)

try:
    log_test("API 客户端初始化", True)
    print(f"  - API 端点：{binance_trading_api.base_url}")
    print(f"  - 超时配置：{binance_trading_api.timeout}秒")
    print(f"  - 接收窗口：{binance_trading_api.recv_window}ms")
except Exception as e:
    log_test("API 客户端初始化", False, str(e))

# ============================================================================
# 第二阶段：精度信息获取
# ============================================================================
print("\n" + "="*80)
print("第二阶段：精度信息获取（从币安 API）")
print("="*80)

test_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
precisions = {}

for symbol in test_symbols:
    try:
        precision = binance_trading_api.get_symbol_precision(symbol)
        if precision:
            precisions[symbol] = precision
            log_test(f"{symbol} 精度获取", True)
            print(f"    数量精度：{precision['quantity_precision']} 位小数")
            print(f"    价格精度：{precision['price_precision']} 位小数")
            print(f"    step_size: {precision['step_size']}")
            print(f"    tick_size: {precision['tick_size']}")
            print(f"    min_qty:   {precision['min_qty']}")
            print(f"    max_qty:   {precision['max_qty']}")
        else:
            log_test(f"{symbol} 精度获取", False, "返回 None")
    except Exception as e:
        log_test(f"{symbol} 精度获取", False, str(e))

# ============================================================================
# 第三阶段：精度调整验证
# ============================================================================
print("\n" + "="*80)
print("第三阶段：精度调整验证（使用真实精度数据）")
print("="*80)

# 测试数据
test_cases = [
    ("BTCUSDT", 0.123456789, 50123.456789),
    ("BTCUSDT", 1.5, 50000.0),
    ("ETHUSDT", 1.234567, 3012.345),
    ("ETHUSDT", 10.5, 3000.0),
    ("BNBUSDT", 0.5, 312.345),
]

for symbol, raw_qty, raw_price in test_cases:
    if symbol not in precisions:
        log_test(f"{symbol} 精度调整", False, "精度信息缺失")
        continue
    
    try:
        # 调整数量
        adjusted_qty = binance_trading_api.adjust_quantity(symbol, raw_qty)
        
        # 调整价格
        adjusted_price = binance_trading_api.adjust_price(symbol, raw_price)
        
        # 验证数量是否是 step_size 的整数倍
        precision = precisions[symbol]
        step_size = Decimal(str(precision['step_size']))
        tick_size = Decimal(str(precision['tick_size']))
        
        adjusted_qty_d = Decimal(str(adjusted_qty))
        adjusted_price_d = Decimal(str(adjusted_price))
        
        qty_remainder = adjusted_qty_d % step_size
        price_remainder = adjusted_price_d % tick_size
        
        qty_valid = qty_remainder == 0
        price_valid = price_remainder == 0
        
        if qty_valid and price_valid:
            log_test(f"{symbol} 精度调整", True)
            print(f"    数量：{raw_qty} → {adjusted_qty} ✅")
            print(f"    价格：{raw_price} → {adjusted_price} ✅")
        else:
            log_test(f"{symbol} 精度调整", False, f"余数：qty={qty_remainder}, price={price_remainder}")
            
    except Exception as e:
        log_test(f"{symbol} 精度调整", False, str(e))

# ============================================================================
# 第四阶段：边界值测试
# ============================================================================
print("\n" + "="*80)
print("第四阶段：边界值测试")
print("="*80)

boundary_tests = [
    ("BTCUSDT", 0.0001, "quantity"),  # 小于最小值
    ("BTCUSDT", 1001.0, "quantity"),  # 大于最大值
    ("ETHUSDT", 0.0005, "quantity"),  # 小于最小值
]

for symbol, value, value_type in boundary_tests:
    if symbol not in precisions:
        log_test(f"{symbol} 边界测试", False, "精度信息缺失")
        continue
    
    try:
        precision = precisions[symbol]
        min_qty = precision['min_qty']
        max_qty = precision['max_qty']
        
        if value_type == "quantity":
            adjusted = binance_trading_api.adjust_quantity(symbol, value)
        
        in_range = min_qty <= adjusted <= max_qty
        
        if in_range:
            log_test(f"{symbol} 边界测试", True)
            print(f"    {value} → {adjusted} (min={min_qty}, max={max_qty}) ✅")
        else:
            log_test(f"{symbol} 边界测试", False, f"超出范围：{adjusted}")
            
    except Exception as e:
        log_test(f"{symbol} 边界测试", False, str(e))

# ============================================================================
# 第五阶段：账户信息查询（只读）
# ============================================================================
print("\n" + "="*80)
print("第五阶段：账户信息查询（只读测试）")
print("="*80)

try:
    # 测试获取余额
    print("\n📊 测试：获取账户余额...")
    balances = binance_trading_api.get_account_balance()
    
    if balances:
        log_test("获取账户余额", True)
        print(f"  共获取 {len(balances)} 个资产")
        
        # 显示 USDT 余额
        usdt_balance = next((b for b in balances if b['asset'] == 'USDT'), None)
        if usdt_balance:
            print(f"  USDT 余额：{usdt_balance['availableBalance']} (可用：{usdt_balance['availableBalance']})")
    else:
        log_test("获取账户余额", False, "返回空列表")
        
except Exception as e:
    log_test("获取账户余额", False, f"{type(e).__name__}: {e}")

try:
    # 测试获取持仓
    print("\n📊 测试：获取当前持仓...")
    positions = binance_trading_api.get_position()
    
    if positions is not None:
        log_test("获取持仓信息", True)
        print(f"  共获取 {len(positions)} 个持仓")
        
        # 显示有持仓的币种
        active_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
        if active_positions:
            print(f"  活跃持仓：{len(active_positions)} 个")
            for pos in active_positions[:3]:  # 只显示前 3 个
                print(f"    - {pos['symbol']}: {pos['positionAmt']} @ {pos['entryPrice']}")
    else:
        log_test("获取持仓信息", False, "返回 None")
        
except Exception as e:
    log_test("获取持仓信息", False, f"{type(e).__name__}: {e}")

try:
    # 测试获取行情
    print("\n📊 测试：获取 BTCUSDT 行情...")
    ticker = binance_trading_api.get_futures_ticker("BTCUSDT")
    
    if ticker:
        log_test("获取行情数据", True)
        print(f"  最新价：{ticker.get('lastPrice', 'N/A')}")
        print(f"  24h 涨跌：{ticker.get('priceChangePercent', 'N/A')}%")
    else:
        log_test("获取行情数据", False, "返回 None")
        
except Exception as e:
    log_test("获取行情数据", False, f"{type(e).__name__}: {e}")

# ============================================================================
# 第六阶段：综合测试报告
# ============================================================================
print("\n" + "="*80)
print("第六阶段：综合测试报告")
print("="*80)

total_tests = test_results['passed'] + test_results['failed']
pass_rate = (test_results['passed'] / total_tests * 100) if total_tests > 0 else 0

print(f"\n📊 测试统计:")
print(f"  总测试数：{total_tests}")
print(f"  通过：{test_results['passed']} ✅")
print(f"  失败：{test_results['failed']} ❌")
print(f"  通过率：{pass_rate:.1f}%")

if test_results['errors']:
    print(f"\n⚠️  失败详情:")
    for error in test_results['errors']:
        print(f"  - {error}")

print("\n" + "="*80)
print("测试结论:")
print("="*80)

if pass_rate >= 95:
    print("✅ 测试通过！系统可以安全投入使用")
    print(f"   通过率 {pass_rate:.1f}% >= 95% 阈值")
elif pass_rate >= 80:
    print("⚠️  基本功能正常，但建议检查失败项")
    print(f"   通过率 {pass_rate:.1f}% >= 80% 阈值")
else:
    print("❌ 测试失败，不建议投入使用")
    print(f"   通过率 {pass_rate:.1f}% < 80% 阈值")

print("\n" + "="*80)
print(f"测试完成时间：{datetime.now()}")
print("="*80)

# 退出码
sys.exit(0 if pass_rate >= 95 else 1)
