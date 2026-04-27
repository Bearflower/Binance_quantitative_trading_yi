#!/usr/bin/env python3
"""
周报生成和推送模块
生成每周交易报告并通过飞书推送
"""

import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional

import requests

from models.database import DatabaseManager, get_db_manager, get_db_connection
from services.trade_statistics import TradeStatistics, get_stats_calculator

logger = logging.getLogger(__name__)


class WeeklyReportGenerator:
    """周报生成器"""
    
    def __init__(self):
        self.db: DatabaseManager = get_db_manager()
        self.stats_calculator: TradeStatistics = get_stats_calculator()
        self.webhook_url = os.getenv('LARK_WEBHOOK_URL')
    
    def generate_weekly_report(self) -> Dict[str, Any]:
        """
        生成本周交易报告
        
        Returns:
            报告数据字典
        """
        # 获取本周统计数据
        stats = self.stats_calculator.calculate_weekly_statistics()
        
        # 获取本周平仓记录
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start_of_week = datetime(monday.year, monday.month, monday.day)
        
        closed_trades = self.db.get_closed_positions(
            start_time=start_of_week
        )
        
        # 统计触发次数（止盈止损触发）
        trigger_count = self._count_triggers(start_of_week)
        
        # 生成报告
        report = {
            'report_type': 'WEEKLY',
            'period': f"{start_of_week.strftime('%Y-%m-%d')} 至 {now.strftime('%Y-%m-%d')}",
            'generated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            
            # 交易统计
            'total_trades': stats['total_trades'],
            'completed_trades': stats['total_trades'],  # 已完成交易数
            'winning_trades': stats['winning_trades'],
            'losing_trades': stats['losing_trades'],
            'win_rate': float(stats['win_rate']),
            
            # 盈亏统计
            'total_net_pnl': float(stats['total_net_pnl']),
            'avg_pnl_rate': float(stats['avg_pnl_rate']),
            'profit_loss_ratio': float(stats['profit_loss_ratio']),
            
            # 触发统计
            'trigger_count': trigger_count,
            
            # 连续统计
            'max_consecutive_wins': stats['max_consecutive_wins'],
            'max_consecutive_losses': stats['max_consecutive_losses'],
            
            # 详细交易列表
            'trades': closed_trades
        }
        
        return report
    
    def _count_triggers(self, start_time: datetime) -> int:
        """统计触发次数"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) as trigger_count
                FROM tp_sl_triggers
                WHERE trigger_time >= %s
            """, (int(start_time.timestamp() * 1000),))
            
            row = cursor.fetchone()
            return row['trigger_count'] if row else 0
    
    def format_report_message(self, report: Dict[str, Any]) -> str:
        """
        格式化报告为飞书消息
        
        Args:
            report: 报告数据
        
        Returns:
            格式化的消息文本
        """
        # 盈亏颜色
        pnl_color = "🟢" if report['total_net_pnl'] >= 0 else "🔴"
        
        # 胜率描述
        win_rate = report['win_rate']
        if win_rate >= 60:
            win_rate_emoji = "🎯"
        elif win_rate >= 40:
            win_rate_emoji = "⚖️"
        else:
            win_rate_emoji = "📉"
        
        message = f"""📊 交易周报 ({report['period']})

📈 交易统计
├─ 触发交易：{report['trigger_count']} 次
├─ 完成交易：{report['completed_trades']} 笔
├─ 盈利次数：{report['winning_trades']} 笔
├─ 亏损次数：{report['losing_trades']} 笔
└─ 胜率：{win_rate_emoji} {win_rate:.2f}%

💰 盈亏统计
├─ 总盈亏：{pnl_color} {report['total_net_pnl']:.2f} USDT
├─ 平均收益率：{report['avg_pnl_rate']:.2f}%
└─ 盈亏比：{report['profit_loss_ratio']:.2f}

📊 连续统计
├─ 最大连胜：{report['max_consecutive_wins']} 次
└─ 最大连败：{report['max_consecutive_losses']} 次

生成时间：{report['generated_at']}"""
        
        return message
    
    def send_weekly_report(self, report: Dict[str, Any] = None) -> bool:
        """
        发送周报到飞书
        
        Args:
            report: 报告数据，如不提供则自动生成
        
        Returns:
            True 表示发送成功
        """
        if not self.webhook_url:
            logger.warning("飞书 webhook URL 未配置，跳过发送")
            return False
        
        try:
            # 生成报告
            if not report:
                report = self.generate_weekly_report()
            
            # 格式化消息
            message = self.format_report_message(report)
            
            # 构建飞书消息
            payload = {
                "msg_type": "text",
                "content": {
                    "text": message
                }
            }
            
            # 发送请求
            response = requests.post(self.webhook_url, json=payload)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('StatusCode') == 0 or result.get('code') == 0:
                    logger.info("✅ 周报已发送到飞书")
                    return True
            
            logger.error(f"飞书发送失败：{response.text}")
            return False
            
        except Exception as e:
            logger.error(f"发送周报失败：{e}", exc_info=True)
            return False


# 全局实例
_report_generator: Optional[WeeklyReportGenerator] = None


def get_report_generator() -> WeeklyReportGenerator:
    """获取周报生成器实例"""
    global _report_generator
    if _report_generator is None:
        _report_generator = WeeklyReportGenerator()
    return _report_generator


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("周报生成测试")
    print("=" * 60)
    
    generator = get_report_generator()
    
    print("\n生成本周报告...")
    report = generator.generate_weekly_report()
    
    print(f"\n报告摘要:")
    print(f"  周期：{report['period']}")
    print(f"  触发交易：{report['trigger_count']} 次")
    print(f"  完成交易：{report['completed_trades']} 笔")
    print(f"  胜率：{report['win_rate']:.2f}%")
    print(f"  总盈亏：{report['total_net_pnl']:.2f} USDT")
    print(f"  平均收益率：{report['avg_pnl_rate']:.2f}%")
    
    print("\n发送飞书通知...")
    success = generator.send_weekly_report(report)
    
    if success:
        print("✅ 报告已发送")
    else:
        print("❌ 报告发送失败")
    
    print("\n" + "=" * 60)
    print("测试完成")
