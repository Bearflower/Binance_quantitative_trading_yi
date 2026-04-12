"""
精度验证脚本

快速验证精度处理是否正确
"""

from decimal import Decimal, ROUND_DOWN


def verify_precision(symbol, quantity, price):
    """
    验证给定币种的精度处理
    
    Args:
        symbol: 币种符号
        quantity: 数量
        price: 价格
    
    Returns:
        (adjusted_qty, adjusted_price, is_valid)
    """
    
    # 已知精度数据
    PRECISION_DATA = {
        "BTCUSDT": {
            "quantity_precision": 3,
            "price_precision": 1,
            "step_size": 0.001,
            "tick_size": 0.1,
            "min_qty": 0.001,
            "max_qty": 1000
        },
        "ETHUSDT": {
            "quantity_precision": 3,
            "price_precision": 2,
            "step_size": 0.001,
            "tick_size": 0.01,
            "min_qty": 0.001,
            "max_qty": 10000
        },
        "BNBUSDT": {
            "quantity_precision": 2,
            "price_precision": 2,
            "step_size": 0.01,
            "tick_size": 0.01,
            "min_qty": 0.01,
            "max_qty": 100000
        }
    }
    
    if symbol not in PRECISION_DATA:
        print(f"❌ 未知币种：{symbol}")
        return None, None, False
    
    precision = PRECISION_DATA[symbol]
    step_size = precision['step_size']
    tick_size = precision['tick_size']
    qty_precision = precision['quantity_precision']
    price_precision = precision['price_precision']
    min_qty = precision['min_qty']
    max_qty = precision['max_qty']
    
    # 调整数量
    qty_decimal = Decimal(str(quantity))
    step_decimal = Decimal(str(step_size))
    multiples = int(qty_decimal / step_decimal)
    adjusted_decimal = step_decimal * multiples
    adjusted_qty = float(adjusted_decimal.quantize(
        Decimal(10) ** -qty_precision,
        rounding=ROUND_DOWN
    ))
    
    # 边界检查
    if adjusted_qty < min_qty:
        adjusted_qty = min_qty
    if adjusted_qty > max_qty:
        adjusted_qty = max_qty
    
    # 调整价格
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))
    price_multiples = int(price_decimal / tick_decimal)
    adjusted_price_decimal = tick_decimal * price_multiples
    adjusted_price = float(adjusted_price_decimal.quantize(
        Decimal(10) ** -price_precision,
        rounding=ROUND_DOWN
    ))
    
    # 验证（使用 Decimal 避免浮点数问题）
    qty_decimal = Decimal(str(adjusted_qty))
    step_decimal = Decimal(str(step_size))
    qty_remainder = qty_decimal % step_decimal
    
    price_decimal = Decimal(str(adjusted_price))
    tick_decimal = Decimal(str(tick_size))
    price_remainder = price_decimal % tick_decimal
    
    qty_valid = qty_remainder == 0
    price_valid = price_remainder == 0
    
    is_valid = qty_valid and price_valid
    
    return adjusted_qty, adjusted_price, is_valid


def main():
    """主函数"""
    print("\n" + "="*80)
    print("精度验证工具")
    print("="*80)
    
    # 测试用例
    test_cases = [
        ("BTCUSDT", 0.123456789, 50123.456789),
        ("BTCUSDT", 1.5, 50000.0),
        ("ETHUSDT", 1.234567, 3012.345),
        ("ETHUSDT", 10.5, 3000.0),
        ("BNBUSDT", 0.5, 312.345),
    ]
    
    all_passed = True
    
    for symbol, qty, price in test_cases:
        adjusted_qty, adjusted_price, is_valid = verify_precision(symbol, qty, price)
        
        status = "✅" if is_valid else "❌"
        
        print(f"\n{symbol}:")
        print(f"  原始：数量={qty}, 价格={price}")
        print(f"  调整后：数量={adjusted_qty}, 价格={adjusted_price}")
        print(f"  验证：{status}")
        
        if not is_valid:
            all_passed = False
    
    print("\n" + "="*80)
    if all_passed:
        print("✅ 所有验证通过！")
    else:
        print("❌ 有验证失败！")
    print("="*80)
    
    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
