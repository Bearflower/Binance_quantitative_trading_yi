#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书推送脚本（T+1 日开盘前运行）

功能：
1. 读取昨日扫描的信号
2. 生成飞书卡片消息
3. 推送到飞书
"""

import json
import requests
from pathlib import Path
from datetime import datetime
import sys


class FeishuPusher:
    """飞书推送器"""
    
    def __init__(self, webhook_url: str):
        """
        初始化飞书推送器
        
        Args:
            webhook_url: 飞书 webhook URL
        """
        self.webhook_url = webhook_url
    
    def send_card_message(self, signals: list) -> bool:
        """
        发送卡片消息
        
        Args:
            signals: 信号列表
        
        Returns:
            bool: 是否发送成功
        """
        # 构建卡片消息
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "template": "blue",
                    "title": {
                        "tag": "plain_text",
                        "content": "📈 今日买入信号（开盘前提醒）"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**共筛选出 {len(signals)} 只股票，建议开盘后择机买入（高开>5% 请放弃）**\n\n⚠️ **风险提示**: 支撑位×0.97 为止损价，移动止盈回撤 8%"
                        }
                    },
                    {
                        "tag": "hr"
                    }
                ]
            }
        }
        
        # 添加每个股票的详细信息
        for idx, sig in enumerate(signals, 1):
            stock_info = {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{idx}. {sig['name']} ({sig['code']})**\n"
                              f"- 💰 支撑位：{sig['support_level']}元\n"
                              f"- 🛑 止损价：{sig['stop_loss_price']}元（支撑位×0.97）\n"
                              f"- 📊 建议买入价：今日开盘价\n"
                              f"- 📈 移动止盈：从持仓最高价回撤 8% 卖出\n"
                              f"- 📉 硬止损：-10%\n"
                              f"- 📅 信号日期：{sig.get('signal_date', 'N/A')}"
                }
            }
            card["card"]["elements"].append(stock_info)
            card["card"]["elements"].append({"tag": "hr"})
        
        # 添加操作按钮
        card["card"]["elements"].append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "📖 查看详细策略文档"
                    },
                    "url": "https://your-doc-link.com",  # 替换为实际文档链接
                    "type": "default"
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "💡 使用指南"
                    },
                    "type": "primary"
                }
            ]
        })
        
        # 发送消息
        try:
            response = requests.post(
                self.webhook_url,
                json=card,
                headers={'Content-Type': 'application/json'}
            )
            
            result = response.json()
            if result.get('StatusCode') == 0 or result.get('code') == 0:
                print("✅ 飞书推送成功")
                return True
            else:
                print(f"❌ 飞书推送失败：{result}")
                return False
        except Exception as e:
            print(f"❌ 发送请求失败：{e}")
            return False
    
    def send_text_message(self, text: str) -> bool:
        """
        发送文本消息（备用方案）
        
        Args:
            text: 消息文本
        
        Returns:
            bool: 是否发送成功
        """
        message = {
            "msg_type": "text",
            "content": {
                "text": text
            }
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                json=message,
                headers={'Content-Type': 'application/json'}
            )
            
            result = response.json()
            if result.get('StatusCode') == 0 or result.get('code') == 0:
                print("✅ 飞书推送成功")
                return True
            else:
                print(f"❌ 飞书推送失败：{result}")
                return False
        except Exception as e:
            print(f"❌ 发送请求失败：{e}")
            return False


def load_signals(signal_date: str = None) -> list:
    """
    加载信号数据
    
    Args:
        signal_date: 信号日期（YYYY-MM-DD），默认昨天
    
    Returns:
        list: 信号列表
    """
    if signal_date is None:
        # 默认获取昨天的信号
        from datetime import timedelta
        yesterday = datetime.now() - timedelta(days=1)
        
        # 如果是周一，获取上周五的信号
        if yesterday.weekday() == 0:
            yesterday = yesterday - timedelta(days=2)
        
        signal_date = yesterday.strftime('%Y-%m-%d')
    
    signal_file = Path('signals') / f'signals_{signal_date}.json'
    
    if not signal_file.exists():
        print(f"⚠️  信号文件不存在：{signal_file}")
        return []
    
    with open(signal_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def main():
    """主函数"""
    print("=" * 80)
    print("飞书推送系统")
    print("=" * 80)
    
    # 配置飞书 webhook URL
    FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/955aced6-5b07-42a6-a714-4c5f4726b003"
    
    # 检查是否配置了 webhook
    if "955aced6-5b07-42a6-a714-4c5f4726b003" not in FEISHU_WEBHOOK:
        print("\n⚠️  请先配置飞书 webhook URL")
        print("编辑文件：feishu_push.py")
        print("修改：FEISHU_WEBHOOK = '您的实际 webhook URL'")
        sys.exit(1)
    
    # 加载信号
    signals = load_signals()
    
    if not signals:
        print("\n⚠️  今日无买入信号")
        # 发送无信号通知（可选）
        # pusher = FeishuPusher(FEISHU_WEBHOOK)
        # pusher.send_text_message("⚠️ 今日无买入信号")
        return
    
    print(f"\n📊 发现 {len(signals)} 个买入信号")
    print()
    
    # 显示信号列表
    for idx, sig in enumerate(signals, 1):
        print(f"{idx}. {sig['code']} - {sig['name']}: 支撑 {sig['support_level']}, 止损 {sig['stop_loss_price']}")
    
    print()
    
    # 检查是否为交互模式（有 stdin 输入）
    import sys
    auto_send = True  # 默认自动发送（用于定时任务）
    if sys.stdin.isatty():
        # 交互模式，询问用户
        confirm = input("是否发送飞书推送？(y/n): ")
        auto_send = confirm.lower() == 'y'
    
    if not auto_send:
        print("❌ 取消推送")
        return
    
    # 创建推送器并发送
    pusher = FeishuPusher(FEISHU_WEBHOOK)
    
    print("\n正在发送飞书推送...")
    success = pusher.send_card_message(signals)
    
    if success:
        print("\n✅ 推送完成！")
        print("\n📋 操作提醒:")
        print("1. 请在 9:15-9:25 查看飞书消息")
        print("2. 观察开盘价，若高开>5% 或涨停请放弃")
        print("3. 买入后立即设置条件单（止损 + 移动止盈）")
    else:
        print("\n❌ 推送失败，请检查 webhook 配置")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断推送")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 推送异常：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
