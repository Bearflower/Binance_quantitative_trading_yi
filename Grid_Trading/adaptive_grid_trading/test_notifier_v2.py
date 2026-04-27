#!/usr/bin/env python3
"""测试网格交易通知模块"""

import sys
from pathlib import Path

# 添加模块路径
project_root = Path(__file__).parent
src_path = project_root / 'src'
sys.path.insert(0, str(src_path))

from monitoring.notifier_v2 import AlertNotifier

def test_grid_notifier():
    """测试网格交易通知器"""
    print("=" * 80)
    print("测试网格交易通知模块")
    print("=" * 80)
    
    notifier = AlertNotifier()
    
    # 测试 1：网格创建成功
    print("\n1. 测试网格创建成功通知...")
    notifier.notify_grid_created(
        grid_id="GRID_001",
        upper_price=50.0,
        lower_price=40.0,
        grid_count=10,
        investment=1000.0
    )
    print("   已发送")
    
    # 测试 2：市场状态变化
    print("\n2. 测试市场状态变化通知...")
    notifier.notify_state_change(
        old_state="震荡",
        new_state="上涨",
        price=45.67,
        adx=25.5
    )
    print("   已发送")
    
    # 测试 3：网格终止
    print("\n3. 测试网格终止通知...")
    notifier.notify_grid_terminated(
        grid_id="GRID_001",
        profit=123.45
    )
    print("   已发送")
    
    # 测试 4：风险事件
    print("\n4. 测试风险事件通知...")
    notifier.notify_risk_event(
        event_type="止损触发",
        trigger_price=42.50,
        trigger_pnl=-0.05,
        action="已卖出平仓"
    )
    print("   已发送")
    
    # 测试 5：系统错误
    print("\n5. 测试系统错误通知...")
    notifier.notify_error(
        error_type="API 错误",
        error_message="无法连接到币安 API",
        details="超时：连接超时 30 秒"
    )
    print("   已发送")
    
    print("\n" + "=" * 80)
    print("测试完成！所有通知已发送到通用服务")
    print("=" * 80)

if __name__ == '__main__':
    test_grid_notifier()
