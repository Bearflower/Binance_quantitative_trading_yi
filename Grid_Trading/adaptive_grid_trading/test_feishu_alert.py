"""
测试飞书报警功能
用于验证飞书 webhook 配置是否正确
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.utils.config_loader import ConfigLoader
from src.monitoring.notifier import AlertNotifier

def test_feishu_alert():
    """测试飞书报警"""
    print("=" * 60)
    print("📧 飞书报警测试")
    print("=" * 60)
    
    # 加载配置
    try:
        config = ConfigLoader()
        alert_config = config.get('monitoring.alert', {})
        
        print(f"\n✅ 配置加载成功")
        print(f"   飞书 webhook: {alert_config.get('feishu_webhook', '未配置')[:50]}...")
        print(f"   启用状态：{alert_config.get('enabled', True)}")
        
        # 创建通知器
        notifier = AlertNotifier(
            feishu_webhook=alert_config.get('feishu_webhook'),
            enabled=alert_config.get('enabled', True)
        )
        
        # 发送测试消息
        print("\n📤 发送测试消息...")
        success = notifier.send_feishu(
            title="🧪 网格系统测试",
            content="这是一条测试消息，用于验证飞书 webhook 配置是否正确。\n\n如果您收到这条消息，说明飞书报警功能工作正常！",
            alert_type="info"
        )
        
        if success:
            print("\n✅ 测试成功！飞书报警功能正常工作")
        else:
            print("\n❌ 测试失败！请检查飞书 webhook 配置")
            
    except Exception as e:
        print(f"\n❌ 测试出错：{e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    test_feishu_alert()
