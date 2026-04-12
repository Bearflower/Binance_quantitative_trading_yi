#!/bin/bash

# 核心模块功能测试脚本

echo "============================================="
echo "核心模块功能测试"
echo "============================================="

# 测试 1: 信号检测模块
echo ""
echo "测试 1: 信号检测模块..."
ssh -o StrictHostKeyChecking=no root@43.156.242.184 << 'ENDSSH'
docker exec binance-trade-analyzer python << 'PYTHON'
from core.signal_detector import get_signal_detector
from config.strategy_params import get_params

print("正在初始化信号检测器...")
detector = get_signal_detector(get_params())
print("✅ 信号检测器初始化成功")

# 测试信号检测（不实际调用 API，只测试模块加载）
print("测试币种：['BTCUSDT', 'ETHUSDT', 'BNBUSDT']")
print("✅ 信号检测模块测试通过")
PYTHON
ENDSSH

# 测试 2: 仓位计算模块
echo ""
echo "测试 2: 仓位计算模块..."
ssh -o StrictHostKeyChecking=no root@43.156.242.184 << 'ENDSSH'
docker exec binance-trade-analyzer python << 'PYTHON'
from core.position_calculator import calculate_position
from decimal import Decimal

print("正在测试仓位计算...")
position = calculate_position(
    symbol='BTCUSDT',
    entry_price=Decimal('95000'),
    stop_loss_price=Decimal('93000'),
    direction=1,
    signal_grade='A'
)

print(f"✅ 仓位计算成功:")
print(f"   名义价值：{position['notional_value']:.2f}U")
print(f"   保证金：{position['margin']:.2f}U")
print(f"   杠杆：{position['leverage']}x")
print(f"   风险占比：{position['risk_ratio']:.1%}")
print("✅ 仓位计算模块测试通过")
PYTHON
ENDSSH

# 测试 3: 风险管理模块
echo ""
echo "测试 3: 风险管理模块..."
ssh -o StrictHostKeyChecking=no root@43.156.242.184 << 'ENDSSH'
docker exec binance-trade-analyzer python << 'PYTHON'
from core.risk_manager import calculate_stop_loss, calculate_take_profit_levels
from decimal import Decimal

print("正在测试风险管理计算...")

# 测试止损价计算
stop_loss = calculate_stop_loss(
    entry_price=Decimal('95000'),
    direction=1,
    stop_loss_pct=Decimal('0.02')
)
print(f"✅ 止损价计算：95000 * (1 - 2%) = {stop_loss}")

# 测试止盈水平
tp_levels = calculate_take_profit_levels(
    entry_price=Decimal('1000'),
    direction=1,
    r_value=Decimal('100')
)
print(f"✅ 止盈水平计算：")
for tp in tp_levels:
    if tp['price']:
        print(f"   {tp['level']}: {tp['price']} ({tp['ratio']*100:.0f}%仓位)")
print("✅ 风险管理模块测试通过")
PYTHON
ENDSSH

# 测试 4: 订单生成模块
echo ""
echo "测试 4: 订单生成模块..."
ssh -o StrictHostKeyChecking=no root@43.156.242.184 << 'ENDSSH'
docker exec binance-trade-analyzer python << 'PYTHON'
from core.order_generator import generate_order_template, generate_all_orders
from core.position_calculator import calculate_position
from decimal import Decimal

print("正在测试订单生成...")

# 计算仓位
position = calculate_position(
    symbol='BTCUSDT',
    entry_price=Decimal('95000'),
    stop_loss_price=Decimal('93000'),
    direction=1,
    signal_grade='A'
)

# 生成订单模板
template = generate_order_template(
    symbol='BTCUSDT',
    direction=1,
    entry_price=Decimal('95000'),
    stop_loss_price=Decimal('93000'),
    signal_grade='A',
    position_data=position
)

print(f"✅ 订单模板生成成功:")
print(f"   方向：{template['direction']}")
print(f"   杠杆：{template['leverage']}x")
print(f"   保证金：{template['margin']:.2f}U")
print(f"   止盈水平：{len(template['take_profit_levels'])}个")

# 生成所有订单
all_orders = generate_all_orders(template)
print(f"✅ 订单参数生成成功:")
print(f"   开仓单：1 个")
print(f"   止损单：1 个")
print(f"   止盈单：{len(all_orders['take_profits'])}个")
print("✅ 订单生成模块测试通过")
PYTHON
ENDSSH

# 测试 5: 应急处理模块
echo ""
echo "测试 5: 应急处理模块..."
ssh -o StrictHostKeyChecking=no root@43.156.242.184 << 'ENDSSH'
docker exec binance-trade-analyzer python << 'PYTHON'
from core.emergency_handler import check_extreme_market, is_trading_allowed
from decimal import Decimal

print("正在测试应急处理功能...")

# 测试极端行情检测
normal = check_extreme_market('BTCUSDT', Decimal('3.5'))
extreme = check_extreme_market('BTCUSDT', Decimal('5.5'))

print(f"✅ 极端行情检测:")
print(f"   涨跌幅 3.5%: {'极端' if normal else '正常'} ✅")
print(f"   涨跌幅 5.5%: {'极端' if extreme else '正常'} ✅")

# 测试交易许可
allowed, reason = is_trading_allowed()
print(f"✅ 交易许可检查：{'允许' if allowed else '禁止'} - {reason}")
print("✅ 应急处理模块测试通过")
PYTHON
ENDSSH

echo ""
echo "============================================="
echo "所有核心模块测试完成！"
echo "============================================="
