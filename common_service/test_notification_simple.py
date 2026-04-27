#!/usr/bin/env python3
"""测试通知服务"""

import requests
import sys

BASE_URL = "http://43.156.242.184:8766/api/v1"

def test_send_notification(project: str, message: str, level: str = "info"):
    """测试发送通知"""
    try:
        print(f"\n{'='*60}")
        print(f"测试项目：{project}")
        print(f"消息内容：{message}")
        
        response = requests.post(
            f"{BASE_URL}/send",
            json={
                "project": project,
                "message": message,
                "type": "text",
                "level": level
            },
            timeout=15  # 增加超时时间
        )
        
        print(f"HTTP 状态码：{response.status_code}")
        print(f"响应内容：{response.text}")
        print(f"{'='*60}\n")
        
        return response.status_code == 200
        
    except Exception as e:
        print(f"❌ 发送失败：{e}")
        print(f"{'='*60}\n")
        return False

def main():
    """测试所有项目"""
    print("\n🚀 开始测试通知服务...\n")
    
    # 测试 5 个项目
    test_cases = [
        ("btc_eth", "₿ BTC/ETH 交易系统 - 测试通知", "info"),
        ("inspection", "🔍 检查自动化系统 - 测试通知", "info"),
        ("stock", "📊 股票筛选系统 - 测试通知", "info"),
        ("grid", "📈 网格交易系统 - 测试通知", "info"),
        ("new_coin", "🪙 新币做空系统 - 测试通知", "warning"),
    ]
    
    success_count = 0
    for project, message, level in test_cases:
        if test_send_notification(project, message, level):
            success_count += 1
    
    print(f"\n✅ 测试完成：成功 {success_count}/{len(test_cases)} 个\n")
    
    if success_count == len(test_cases):
        print("🎉 所有通知服务正常工作！")
        return 0
    else:
        print("⚠️  部分通知服务失败，请检查日志")
        return 1

if __name__ == "__main__":
    sys.exit(main())
