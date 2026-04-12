"""
精度处理测试工具

用于测试和验证币安 API 的精度处理逻辑
确保 BTC、ETH 等币种的下单数量和价格能够一次性成功
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.binance_trading_api import BinanceTradingAPI
from decimal import Decimal


def test_precision_calculation():
    """测试精度计算"""
    print("\n" + "="*80)
    print("精度处理测试工具")
    print("="*80)
    
    api = BinanceTradingAPI()
    
    # 测试币种列表
    test_symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "BNBUSDT",
        "SOLUSDT",
        "XRPUSDT"
    ]
    
    print("\n📊 测试币种精度信息获取")
    print("-" * 80)
    
    for symbol in test_symbols:
        print(f"\n测试 {symbol}:")
        precision = api.get_symbol_precision(symbol)
        
        if precision:
            print(f"  ✅ 精度信息获取成功")
            print(f"    - 数量精度：{precision['quantity_precision']} 位小数")
            print(f"    - 价格精度：{precision['price_precision']} 位小数")
            print(f"    - step_size: {precision['step_size']}")
            print(f"    - tick_size: {precision['tick_size']}")
            print(f"    - min_qty:   {precision['min_qty']}")
            print(f"    - max_qty:   {precision['max_qty']}")
        else:
            print(f"  ❌ 精度信息获取失败")
    
    print("\n" + "="*80)
    print("数量调整测试")
    print("="*80)
    
    # 测试数量调整
    test_cases = [
        ("BTCUSDT", 0.123456789),
        ("BTCUSDT", 1.5),
        ("BTCUSDT", 0.001),
        ("ETHUSDT", 1.234567),
        ("ETHUSDT", 10.5),
        ("BNBUSDT", 0.5),
    ]
    
    for symbol, quantity in test_cases:
        print(f"\n测试 {symbol} 数量 {quantity}:")
        adjusted = api.adjust_quantity(symbol, quantity)
        
        # 验证是否是 step_size 的整数倍
        precision = api.get_symbol_precision(symbol)
        if precision:
            step_size = precision['step_size']
            remainder = adjusted % step_size
            is_multiple = abs(remainder) < 1e-10
            
            print(f"  调整后：{adjusted}")
            print(f"  step_size: {step_size}")
            print(f"  余数：{remainder}")
            print(f"  ✅ 是 step_size 的整数倍" if is_multiple else f"  ❌ 不是 step_size 的整数倍")
    
    print("\n" + "="*80)
    print("价格调整测试")
    print("="*80)
    
    # 测试价格调整
    test_price_cases = [
        ("BTCUSDT", 50123.456789),
        ("BTCUSDT", 50000.0),
        ("ETHUSDT", 3012.345),
        ("ETHUSDT", 3000.0),
        ("BNBUSDT", 312.345),
    ]
    
    for symbol, price in test_price_cases:
        print(f"\n测试 {symbol} 价格 {price}:")
        adjusted = api.adjust_price(symbol, price)
        
        # 验证是否是 tick_size 的整数倍
        precision = api.get_symbol_precision(symbol)
        if precision:
            tick_size = precision['tick_size']
            remainder = adjusted % tick_size
            is_multiple = abs(remainder) < 1e-10
            
            print(f"  调整后：{adjusted}")
            print(f"  tick_size: {tick_size}")
            print(f"  余数：{remainder}")
            print(f"  ✅ 是 tick_size 的整数倍" if is_multiple else f"  ❌ 不是 tick_size 的整数倍")
    
    print("\n" + "="*80)
    print("Decimal 精度验证")
    print("="*80)
    
    # 使用 Decimal 验证精度
    test_decimal_cases = [
        ("BTCUSDT", 0.123456789, "quantity"),
        ("BTCUSDT", 50123.456789, "price"),
        ("ETHUSDT", 1.234567, "quantity"),
        ("ETHUSDT", 3012.345, "price"),
    ]
    
    for symbol, value, value_type in test_decimal_cases:
        precision = api.get_symbol_precision(symbol)
        if precision:
            if value_type == "quantity":
                step_size = precision['step_size']
                adjusted = api.adjust_quantity(symbol, value)
            else:
                tick_size = precision['tick_size']
                adjusted = api.adjust_price(symbol, value)
            
            # 使用 Decimal 严格验证
            adjusted_decimal = Decimal(str(adjusted))
            if value_type == "quantity":
                step_decimal = Decimal(str(step_size))
            else:
                step_decimal = Decimal(str(tick_size))
            
            remainder = adjusted_decimal % step_decimal
            is_exact_multiple = remainder == 0
            
            print(f"\n{symbol} {value_type}={value}:")
            print(f"  调整后：{adjusted}")
            print(f"  Decimal 余数：{remainder}")
            print(f"  {'✅ 精确匹配' if is_exact_multiple else '❌ 存在误差'}")
    
    print("\n" + "="*80)
    print("边界值测试")
    print("="*80)
    
    # 测试边界值
    boundary_tests = [
        ("BTCUSDT", 0.0001, "quantity"),  # 很小
        ("BTCUSDT", 1000.0, "quantity"),  # 很大
        ("ETHUSDT", 0.001, "quantity"),
        ("ETHUSDT", 10000.0, "quantity"),
    ]
    
    for symbol, value, value_type in boundary_tests:
        precision = api.get_symbol_precision(symbol)
        if precision:
            min_qty = precision['min_qty']
            max_qty = precision['max_qty']
            
            if value_type == "quantity":
                adjusted = api.adjust_quantity(symbol, value)
            else:
                adjusted = api.adjust_price(symbol, value)
            
            print(f"\n{symbol} {value_type}={value}:")
            print(f"  min={min_qty}, max={max_qty}")
            print(f"  调整后：{adjusted}")
            print(f"  {'✅ 在范围内' if min_qty <= adjusted <= max_qty else '❌ 超出范围'}")
    
    print("\n" + "="*80)
    print("测试完成")
    print("="*80)


def verify_step_size_alignment():
    """验证 step_size 对齐"""
    print("\n" + "="*80)
    print("step_size 对齐验证")
    print("="*80)
    
    api = BinanceTradingAPI()
    
    # 获取 BTCUSDT 精度
    precision = api.get_symbol_precision("BTCUSDT")
    if not precision:
        print("❌ 无法获取 BTCUSDT 精度信息")
        return
    
    step_size = precision['step_size']
    print(f"\nBTCUSDT step_size: {step_size}")
    
    # 测试一系列数量
    test_quantities = [
        0.001,
        0.01,
        0.1,
        1.0,
        1.5,
        10.0,
        0.123456789,
        0.001234567
    ]
    
    print("\n测试数量调整:")
    print("-" * 80)
    print(f"{'原始数量':<20} {'调整后':<20} {'倍数':<10} {'余数':<20} {'状态'}")
    print("-" * 80)
    
    for qty in test_quantities:
        adjusted = api.adjust_quantity("BTCUSDT", qty)
        
        # 计算倍数
        from decimal import Decimal
        adjusted_d = Decimal(str(adjusted))
        step_d = Decimal(str(step_size))
        multiples = adjusted_d / step_d
        
        remainder = adjusted_d % step_d
        
        status = "✅" if remainder == 0 else "❌"
        
        print(f"{qty:<20.8f} {adjusted:<20.8f} {float(multiples):<10.4f} {float(remainder):<20.15f} {status}")
    
    print("-" * 80)


if __name__ == "__main__":
    test_precision_calculation()
    verify_step_size_alignment()
