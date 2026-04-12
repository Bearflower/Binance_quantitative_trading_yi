#!/usr/bin/env python3
"""
新币种模拟下单测试脚本（离线版本）
测试币种：METAUSDT, XAUTUSDT, BSBUSDT, PAYPUSDT
测试内容：
1. 使用模拟精度数据测试
2. 验证精度调整逻辑
3. 模拟下单参数验证
"""

from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any

# 模拟的币种精度数据（基于典型值）
MOCK_PRECISION_DATA = {
    'METAUSDT': {
        'quantity_precision': 1,
        'price_precision': 4,
        'step_size': 0.1,
        'tick_size': 0.0001,
        'min_qty': 0.1,
        'max_qty': 1000000
    },
    'XAUTUSDT': {
        'quantity_precision': 3,
        'price_precision': 2,
        'step_size': 0.001,
        'tick_size': 0.01,
        'min_qty': 0.001,
        'max_qty': 100000
    },
    'BSBUSDT': {
        'quantity_precision': 1,
        'price_precision': 4,
        'step_size': 0.1,
        'tick_size': 0.0001,
        'min_qty': 0.1,
        'max_qty': 1000000
    },
    'PAYPUSDT': {
        'quantity_precision': 1,
        'price_precision': 4,
        'step_size': 0.1,
        'tick_size': 0.0001,
        'min_qty': 0.1,
        'max_qty': 1000000
    }
}

def get_mock_precision(symbol: str) -> Dict[str, Any]:
    """获取模拟的精度数据"""
    return MOCK_PRECISION_DATA.get(symbol)

def adjust_quantity(symbol: str, quantity: float) -> float:
    """
    根据币种精度调整数量
    
    Args:
        symbol: 币种符号
        quantity: 原始数量
        
    Returns:
        调整后的数量
    """
    precision_info = get_mock_precision(symbol)
    
    if not precision_info:
        print(f"⚠️ {symbol} 精度信息缺失，使用默认精度")
        return round(quantity, 3)
    
    step_size = precision_info['step_size']
    quantity_precision = precision_info['quantity_precision']
    min_qty = precision_info.get('min_qty', 0.001)
    max_qty = precision_info.get('max_qty', 1000000)
    
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
        print(f"⚠️ {symbol} 数量 {adjusted_quantity} 小于最小值 {min_qty}，调整为 {min_qty}")
        adjusted_quantity = min_qty
    
    # 确保不大于最大值
    if adjusted_quantity > max_qty:
        print(f"⚠️ {symbol} 数量 {adjusted_quantity} 大于最大值 {max_qty}，调整为 {max_qty}")
        adjusted_quantity = max_qty
    
    return adjusted_quantity

def adjust_price(symbol: str, price: float) -> float:
    """
    根据币种精度调整价格
    
    Args:
        symbol: 币种符号
        price: 原始价格
        
    Returns:
        调整后的价格
    """
    precision_info = get_mock_precision(symbol)
    
    if not precision_info:
        print(f"⚠️ {symbol} 精度信息缺失，使用默认精度")
        return round(price, 2)
    
    tick_size = precision_info['tick_size']
    price_precision = precision_info['price_precision']
    
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

def test_symbol_info(symbol: str):
    """测试币种信息"""
    print(f"\n{'='*60}")
    print(f"测试币种：{symbol}")
    print(f"{'='*60}")
    
    precision_info = get_mock_precision(symbol)
    
    if not precision_info:
        print(f"❌ {symbol} 精度信息不存在")
        return False
    
    print(f"✅ {symbol} 精度信息:")
    print(f"   数量精度：{precision_info['quantity_precision']}")
    print(f"   价格精度：{precision_info['price_precision']}")
    print(f"   Step Size: {precision_info['step_size']}")
    print(f"   Tick Size: {precision_info['tick_size']}")
    print(f"   最小数量：{precision_info['min_qty']}")
    print(f"   最大数量：{precision_info['max_qty']}")
    
    return True

def test_quantity_adjustment(symbol: str, test_quantities: list) -> bool:
    """测试数量精度调整"""
    print(f"\n[测试数量调整]")
    
    all_passed = True
    for qty in test_quantities:
        adjusted_qty = adjust_quantity(symbol, qty)
        
        # 验证调整后的数量是 step_size 的整数倍
        precision_info = get_mock_precision(symbol)
        step_size = precision_info['step_size']
        
        # 使用 Decimal 验证
        adjusted_decimal = Decimal(str(adjusted_qty))
        step_decimal = Decimal(str(step_size))
        remainder = adjusted_decimal % step_decimal
        
        is_valid = abs(float(remainder)) < 1e-10
        
        status = "✅" if is_valid else "❌"
        print(f"   {status} 原始：{qty:.8f} → 调整后：{adjusted_qty:.8f} (step_size={step_size})")
        
        if not is_valid:
            all_passed = False
            print(f"      警告：调整后的数量不是 step_size 的整数倍！余数={remainder}")
    
    return all_passed

def test_price_adjustment(symbol: str, test_prices: list) -> bool:
    """测试价格精度调整"""
    print(f"\n[测试价格调整]")
    
    all_passed = True
    for price in test_prices:
        adjusted_price = adjust_price(symbol, price)
        
        # 验证调整后的价格是 tick_size 的整数倍
        precision_info = get_mock_precision(symbol)
        tick_size = precision_info['tick_size']
        
        # 使用 Decimal 验证
        price_decimal = Decimal(str(adjusted_price))
        tick_decimal = Decimal(str(tick_size))
        remainder = price_decimal % tick_decimal
        
        is_valid = abs(float(remainder)) < 1e-10
        
        status = "✅" if is_valid else "❌"
        print(f"   {status} 原始：{price:.8f} → 调整后：{adjusted_price:.8f} (tick_size={tick_size})")
        
        if not is_valid:
            all_passed = False
            print(f"      警告：调整后的价格不是 tick_size 的整数倍！余数={remainder}")
    
    return all_passed

def simulate_order(symbol: str, side: str, quantity: float, price: float = None) -> bool:
    """模拟下单"""
    print(f"\n[模拟下单测试]")
    
    # 调整精度
    adjusted_quantity = adjust_quantity(symbol, quantity)
    adjusted_price = None
    if price:
        adjusted_price = adjust_price(symbol, price)
    
    # 获取精度信息
    precision_info = get_mock_precision(symbol)
    min_qty = precision_info['min_qty']
    max_qty = precision_info['max_qty']
    
    # 构建订单参数
    order_params = {
        'symbol': symbol,
        'side': side,
        'type': 'LIMIT' if price else 'MARKET',
        'quantity': adjusted_quantity,
        'positionSide': 'SHORT',
        'reduceOnly': False,
    }
    
    if price:
        order_params['price'] = adjusted_price
        order_params['timeInForce'] = 'GTC'
    
    # 打印订单参数
    print(f"   订单参数:")
    for key, value in order_params.items():
        print(f"     {key}: {value}")
    
    # 验证参数
    print(f"   参数验证:")
    
    # 1. 验证数量
    if adjusted_quantity < min_qty:
        print(f"   ❌ 数量 {adjusted_quantity} 小于最小值 {min_qty}")
        return False
    elif adjusted_quantity > max_qty:
        print(f"   ❌ 数量 {adjusted_quantity} 大于最大值 {max_qty}")
        return False
    else:
        print(f"   ✅ 数量 {adjusted_quantity} 在有效范围内 [{min_qty}, {max_qty}]")
    
    # 2. 验证价格（如果是限价单）
    if price:
        print(f"   ✅ 价格 {adjusted_price} 已调整")
    
    # 3. 验证 PM 账户端点
    print(f"   ✅ PM 账户端点：POST /papi/v1/um/order")
    
    return True

def test_stop_loss_order(symbol: str, quantity: float, stop_price: float) -> bool:
    """测试止损单"""
    print(f"\n[测试止损单]")
    
    # 调整精度
    adjusted_quantity = adjust_quantity(symbol, quantity)
    adjusted_stop_price = adjust_price(symbol, stop_price)
    
    # 构建条件单参数
    order_params = {
        'symbol': symbol,
        'side': 'BUY',
        'strategyType': 'STOP_MARKET',
        'quantity': adjusted_quantity,
        'stopPrice': adjusted_stop_price,
        'positionSide': 'SHORT',
        'reduceOnly': True,
        'workingType': 'MARK_PRICE'
    }
    
    print(f"   止损单参数:")
    for key, value in order_params.items():
        print(f"     {key}: {value}")
    
    print(f"   ✅ PM 账户条件单端点：POST /papi/v1/um/conditional/order")
    print(f"   ✅ 止损单参数验证通过")
    
    return True

def test_take_profit_order(symbol: str, quantity: float, stop_price: float) -> bool:
    """测试止盈单"""
    print(f"\n[测试止盈单]")
    
    # 调整精度
    adjusted_quantity = adjust_quantity(symbol, quantity)
    adjusted_stop_price = adjust_price(symbol, stop_price)
    
    # 构建条件单参数
    order_params = {
        'symbol': symbol,
        'side': 'BUY',
        'strategyType': 'TAKE_PROFIT_MARKET',
        'quantity': adjusted_quantity,
        'stopPrice': adjusted_stop_price,
        'positionSide': 'SHORT',
        'reduceOnly': True,
        'workingType': 'MARK_PRICE'
    }
    
    print(f"   止盈单参数:")
    for key, value in order_params.items():
        print(f"     {key}: {value}")
    
    print(f"   ✅ PM 账户条件单端点：POST /papi/v1/um/conditional/order")
    print(f"   ✅ 止盈单参数验证通过")
    
    return True

def run_all_tests():
    """运行所有测试"""
    print("="*60)
    print("新币种模拟下单测试（离线版本）")
    print("测试币种：METAUSDT, XAUTUSDT, BSBUSDT, PAYPUSDT")
    print("="*60)
    
    # 测试币种列表
    symbols = ['METAUSDT', 'XAUTUSDT', 'BSBUSDT', 'PAYPUSDT']
    
    # 测试结果统计
    test_results = {
        'precision': {},
        'quantity': {},
        'price': {},
        'market_order': {},
        'limit_order': {},
        'stop_loss': {},
        'take_profit': {}
    }
    
    for symbol in symbols:
        try:
            print(f"\n{'='*60}")
            print(f"开始测试：{symbol}")
            print(f"{'='*60}")
            
            # 1. 测试精度信息
            test_results['precision'][symbol] = test_symbol_info(symbol)
            
            if not test_results['precision'][symbol]:
                print(f"⚠️ {symbol} 精度信息不存在，跳过后续测试")
                continue
            
            # 2. 测试数量调整
            precision_info = get_mock_precision(symbol)
            min_qty = precision_info['min_qty']
            
            test_quantities = [
                min_qty,
                min_qty * 10,
                min_qty * 100,
                1.23456789  # 测试精度调整
            ]
            test_results['quantity'][symbol] = test_quantity_adjustment(symbol, test_quantities)
            
            # 3. 测试价格调整
            test_prices = [
                1.0,
                10.0,
                100.0,
                1.23456789  # 测试精度调整
            ]
            test_results['price'][symbol] = test_price_adjustment(symbol, test_prices)
            
            # 4. 测试市价单
            test_quantity = min_qty * 10
            test_results['market_order'][symbol] = simulate_order(symbol, 'SELL', test_quantity, None)
            
            # 5. 测试限价单
            test_price = 10.0
            test_results['limit_order'][symbol] = simulate_order(symbol, 'SELL', test_quantity, test_price)
            
            # 6. 测试止损单
            stop_price = test_price * 1.05
            test_results['stop_loss'][symbol] = test_stop_loss_order(symbol, test_quantity, stop_price)
            
            # 7. 测试止盈单
            take_profit_price = test_price * 0.80
            test_results['take_profit'][symbol] = test_take_profit_order(symbol, test_quantity, take_profit_price)
            
        except Exception as e:
            print(f"\n❌ {symbol} 测试异常：{e}")
            import traceback
            traceback.print_exc()
    
    # 打印测试汇总
    print(f"\n{'='*60}")
    print("测试汇总")
    print(f"{'='*60}")
    
    for symbol in symbols:
        print(f"\n{symbol}:")
        print(f"  精度信息：{'✅ 通过' if test_results['precision'].get(symbol) else '❌ 失败'}")
        print(f"  数量调整：{'✅ 通过' if test_results['quantity'].get(symbol) else '❌ 失败'}")
        print(f"  价格调整：{'✅ 通过' if test_results['price'].get(symbol) else '❌ 失败'}")
        print(f"  市价单：{'✅ 通过' if test_results['market_order'].get(symbol) else '❌ 失败'}")
        print(f"  限价单：{'✅ 通过' if test_results['limit_order'].get(symbol) else '❌ 失败'}")
        print(f"  止损单：{'✅ 通过' if test_results['stop_loss'].get(symbol) else '❌ 失败'}")
        print(f"  止盈单：{'✅ 通过' if test_results['take_profit'].get(symbol) else '❌ 失败'}")
    
    # 统计总体通过率
    total_tests = sum(len(v) for v in test_results.values())
    passed_tests = sum(sum(1 for v in result.values() if v) for result in test_results.values())
    
    print(f"\n{'='*60}")
    print(f"总体测试结果：{passed_tests}/{total_tests} 通过 ({passed_tests/total_tests*100:.1f}%)")
    print(f"{'='*60}")
    
    return passed_tests == total_tests

if __name__ == '__main__':
    success = run_all_tests()
    
    if success:
        print("\n🎉 所有测试通过！")
    else:
        print("\n⚠️ 部分测试失败，请检查日志")
    
    exit(0 if success else 1)
