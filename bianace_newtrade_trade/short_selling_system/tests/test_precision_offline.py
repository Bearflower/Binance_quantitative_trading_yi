"""
离线精度测试 - 不依赖网络

使用已知的精度数据验证精度处理逻辑
"""

from decimal import Decimal, ROUND_DOWN


def test_precision_logic():
    """测试精度处理逻辑（使用已知的 BTC/ETH 精度数据）"""
    
    print("\n" + "="*80)
    print("离线精度测试 - 验证 BTC/ETH 精度处理")
    print("="*80)
    
    # 已知的精度数据（从币安 API 获取）
    known_precisions = {
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
    
    def adjust_quantity_offline(symbol: str, quantity: float) -> float:
        """离线版本的数量调整（不依赖网络）"""
        precision = known_precisions.get(symbol)
        
        if not precision:
            return round(quantity, 3)
        
        step_size = precision['step_size']
        quantity_precision = precision['quantity_precision']
        min_qty = precision['min_qty']
        max_qty = precision['max_qty']
        
        # 使用 Decimal 避免浮点数精度问题
        qty_decimal = Decimal(str(quantity))
        step_decimal = Decimal(str(step_size))
        
        # 计算是 step_size 的多少倍（向下取整）
        multiples = int(qty_decimal / step_decimal)
        
        # 重新计算调整后的数量
        adjusted_decimal = step_decimal * multiples
        
        # 转换为浮点数并保留指定精度
        adjusted_quantity = float(adjusted_decimal.quantize(
            Decimal(10) ** -quantity_precision,
            rounding=ROUND_DOWN
        ))
        
        # 确保不小于最小值
        if adjusted_quantity < min_qty:
            adjusted_quantity = min_qty
        
        # 确保不大于最大值
        if adjusted_quantity > max_qty:
            adjusted_quantity = max_qty
        
        return adjusted_quantity
    
    def adjust_price_offline(symbol: str, price: float) -> float:
        """离线版本的价格调整（不依赖网络）"""
        precision = known_precisions.get(symbol)
        
        if not precision:
            return round(price, 2)
        
        tick_size = precision['tick_size']
        price_precision = precision['price_precision']
        
        # 使用 Decimal 避免浮点数精度问题
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))
        
        # 计算是 tick_size 的多少倍（向下取整）
        multiples = int(price_decimal / tick_decimal)
        
        # 重新计算调整后的价格
        adjusted_decimal = tick_decimal * multiples
        
        # 转换为浮点数并保留指定精度
        adjusted_price = float(adjusted_decimal.quantize(
            Decimal(10) ** -price_precision,
            rounding=ROUND_DOWN
        ))
        
        return adjusted_price
    
    # 测试 BTC 数量调整
    print("\n📊 BTCUSDT 数量调整测试")
    print("-" * 80)
    
    btc_qty_tests = [
        0.123456789,
        1.5,
        0.001,
        10.0,
        0.001234567,
        999.999999
    ]
    
    for qty in btc_qty_tests:
        adjusted = adjust_quantity_offline("BTCUSDT", qty)
        step_size = known_precisions["BTCUSDT"]["step_size"]
        
        # 验证
        adjusted_d = Decimal(str(adjusted))
        step_d = Decimal(str(step_size))
        remainder = adjusted_d % step_d
        is_valid = remainder == 0
        
        print(f"  {qty:.9f} → {adjusted:.3f} (step_size={step_size}) {'✅' if is_valid else '❌'}")
    
    # 测试 BTC 价格调整
    print("\n📊 BTCUSDT 价格调整测试")
    print("-" * 80)
    
    btc_price_tests = [
        50123.456789,
        50000.0,
        49999.99,
        60000.123
    ]
    
    for price in btc_price_tests:
        adjusted = adjust_price_offline("BTCUSDT", price)
        tick_size = known_precisions["BTCUSDT"]["tick_size"]
        
        # 验证
        adjusted_d = Decimal(str(adjusted))
        tick_d = Decimal(str(tick_size))
        remainder = adjusted_d % tick_d
        is_valid = remainder == 0
        
        print(f"  {price:.6f} → {adjusted:.1f} (tick_size={tick_size}) {'✅' if is_valid else '❌'}")
    
    # 测试 ETH 数量调整
    print("\n📊 ETHUSDT 数量调整测试")
    print("-" * 80)
    
    eth_qty_tests = [
        1.234567,
        10.5,
        0.001,
        9999.999
    ]
    
    for qty in eth_qty_tests:
        adjusted = adjust_quantity_offline("ETHUSDT", qty)
        step_size = known_precisions["ETHUSDT"]["step_size"]
        
        # 验证
        adjusted_d = Decimal(str(adjusted))
        step_d = Decimal(str(step_size))
        remainder = adjusted_d % step_d
        is_valid = remainder == 0
        
        print(f"  {qty:.6f} → {adjusted:.3f} (step_size={step_size}) {'✅' if is_valid else '❌'}")
    
    # 测试 ETH 价格调整
    print("\n📊 ETHUSDT 价格调整测试")
    print("-" * 80)
    
    eth_price_tests = [
        3012.345,
        3000.0,
        2999.999
    ]
    
    for price in eth_price_tests:
        adjusted = adjust_price_offline("ETHUSDT", price)
        tick_size = known_precisions["ETHUSDT"]["tick_size"]
        
        # 验证
        adjusted_d = Decimal(str(adjusted))
        tick_d = Decimal(str(tick_size))
        remainder = adjusted_d % tick_d
        is_valid = remainder == 0
        
        print(f"  {price:.6f} → {adjusted:.2f} (tick_size={tick_size}) {'✅' if is_valid else '❌'}")
    
    # 测试 BNB 数量调整
    print("\n📊 BNBUSDT 数量调整测试")
    print("-" * 80)
    
    bnb_qty_tests = [
        0.5,
        1.234,
        10.0,
        0.01
    ]
    
    for qty in bnb_qty_tests:
        adjusted = adjust_quantity_offline("BNBUSDT", qty)
        step_size = known_precisions["BNBUSDT"]["step_size"]
        
        # 验证
        adjusted_d = Decimal(str(adjusted))
        step_d = Decimal(str(step_size))
        remainder = adjusted_d % step_d
        is_valid = remainder == 0
        
        print(f"  {qty:.6f} → {adjusted:.2f} (step_size={step_size}) {'✅' if is_valid else '❌'}")
    
    # 边界值测试
    print("\n📊 边界值测试")
    print("-" * 80)
    
    boundary_tests = [
        ("BTCUSDT", 0.0001, "quantity"),  # 小于最小值
        ("BTCUSDT", 1001.0, "quantity"),  # 大于最大值
        ("ETHUSDT", 0.0005, "quantity"),  # 小于最小值
    ]
    
    for symbol, value, value_type in boundary_tests:
        min_qty = known_precisions[symbol]["min_qty"]
        max_qty = known_precisions[symbol]["max_qty"]
        
        if value_type == "quantity":
            adjusted = adjust_quantity_offline(symbol, value)
        
        print(f"  {symbol} {value}: min={min_qty}, max={max_qty}, 调整后={adjusted}")
        if adjusted < min_qty:
            print(f"    ⚠️ 小于最小值，已调整为 {min_qty}")
        elif adjusted > max_qty:
            print(f"    ⚠️ 大于最大值，已调整为 {max_qty}")
        else:
            print(f"    ✅ 在有效范围内")
    
    print("\n" + "="*80)
    print("✅ 所有测试完成")
    print("="*80)
    
    # 总结
    print("\n📋 精度处理要点总结:")
    print("  1. 使用 Decimal 避免浮点数精度问题")
    print("  2. 先计算 step_size/tick_size 的倍数，再转换回浮点数")
    print("  3. 向下取整确保不超过原始值")
    print("  4. 检查 min_qty 和 max_qty 限制")
    print("  5. 最终验证是否是 step_size/tick_size 的整数倍")


if __name__ == "__main__":
    test_precision_logic()
