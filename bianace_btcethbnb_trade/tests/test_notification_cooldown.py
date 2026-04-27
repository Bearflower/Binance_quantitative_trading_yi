#!/usr/bin/env python3
"""
通知冷却期测试

测试NotificationManager的冷却期功能：
1. 交易通知冷却期检查
2. 被抑制通知记录
3. FrequencyController集成
4. 查询接口

版本: v1.0.0
创建时间: 2026-04-27
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from scheduler.notifier import NotificationManager


class TestNotificationCooldown(unittest.TestCase):
    """通知冷却期测试"""

    def setUp(self):
        """测试前准备"""
        # 创建NotificationManager实例（模拟飞书通知器）
        with patch('scheduler.notifier.LarkNotifier') as mock_lark:
            mock_lark_instance = Mock()
            mock_lark.return_value = mock_lark_instance
            self.notifier = NotificationManager()
            self.mock_lark = mock_lark_instance

    def test_trade_notification_cooldown(self):
        """测试交易通知冷却期"""
        # 第一次发送
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知1"
        )
        self.assertTrue(success)
        self.assertEqual(reason, "通知发送成功")

        # 第二次发送（在冷却期内）
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知2"
        )
        self.assertFalse(success)
        self.assertIn("通知冷却期内", reason)

    def test_force_send_notification(self):
        """测试强制发送通知（忽略冷却期）"""
        # 第一次发送
        self.notifier.send_trade_notification_with_cooldown(
            symbol="ETHUSDT",
            message="测试通知1"
        )

        # 强制发送（忽略冷却期）
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="ETHUSDT",
            message="测试通知2",
            force=True
        )
        self.assertTrue(success)
        self.assertEqual(reason, "通知发送成功")

    def test_different_symbols_independent(self):
        """测试不同交易对的冷却期独立"""
        # 发送BTC通知
        success1, _ = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="BTC通知"
        )
        self.assertTrue(success1)

        # 发送ETH通知（应该成功，不同交易对）
        success2, _ = self.notifier.send_trade_notification_with_cooldown(
            symbol="ETHUSDT",
            message="ETH通知"
        )
        self.assertTrue(success2)

        # 再次发送BTC通知（应该失败，在冷却期内）
        success3, reason3 = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="BTC通知2"
        )
        self.assertFalse(success3)

    def test_suppressed_notification_recording(self):
        """测试被抑制通知记录"""
        # 发送通知
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BNBUSDT",
            message="测试通知"
        )

        # 再次发送（被抑制）
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BNBUSDT",
            message="被抑制的通知"
        )

        # 查询被抑制的通知
        suppressed = self.notifier.get_suppressed_notifications()
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]['symbol'], 'BNBUSDT')
        self.assertEqual(suppressed[0]['type'], 'trade')

    def test_get_suppressed_notifications_filtering(self):
        """测试被抑制通知的过滤查询"""
        # 发送多个通知
        for symbol in ['BTCUSDT', 'ETHUSDT', 'BTCUSDT', 'BNBUSDT']:
            self.notifier.send_trade_notification_with_cooldown(
                symbol=symbol,
                message=f"{symbol}通知"
            )

        # 查询所有被抑制的通知
        all_suppressed = self.notifier.get_suppressed_notifications()
        self.assertEqual(len(all_suppressed), 1)  # 只有BTCUSDT被抑制

        # 按交易对过滤
        btc_suppressed = self.notifier.get_suppressed_notifications(symbol='BTCUSDT')
        self.assertEqual(len(btc_suppressed), 1)

    def test_clear_suppressed_notifications(self):
        """测试清空被抑制通知"""
        # 发送通知并产生被抑制的记录
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知"
        )
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="被抑制的通知"
        )

        # 清空
        self.notifier.clear_suppressed_notifications()

        # 验证已清空
        suppressed = self.notifier.get_suppressed_notifications()
        self.assertEqual(len(suppressed), 0)

    def test_cooldown_config(self):
        """测试冷却期配置"""
        # 设置新的冷却期
        self.notifier.set_cooldown_config(
            trade_cooldown=60,
            error_cooldown=120
        )

        # 验证配置已更新
        self.assertEqual(self.notifier.trade_notification_cooldown, 60)
        self.assertEqual(self.notifier.error_notification_cooldown, 120)

    def test_notification_time_recording(self):
        """测试通知时间记录"""
        # 发送通知
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知"
        )

        # 验证通知时间已记录
        notification_key = "trade_BTCUSDT"
        self.assertIn(notification_key, self.notifier._last_notification_times)

        last_time = self.notifier._last_notification_times[notification_key]
        self.assertIsInstance(last_time, datetime)

    def test_cooldown_expiration(self):
        """测试冷却期过期"""
        # 发送通知
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知"
        )

        # 手动设置通知时间为40分钟前（超过默认30分钟冷却期）
        notification_key = "trade_BTCUSDT"
        self.notifier._last_notification_times[notification_key] = \
            datetime.now() - timedelta(minutes=40)

        # 现在应该可以发送
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="冷却期已过"
        )
        self.assertTrue(success)


class TestNotificationManagerIntegration(unittest.TestCase):
    """NotificationManager集成测试"""

    def setUp(self):
        """测试前准备"""
        with patch('scheduler.notifier.LarkNotifier') as mock_lark:
            mock_lark_instance = Mock()
            mock_lark.return_value = mock_lark_instance
            self.notifier = NotificationManager()
            self.mock_lark = mock_lark_instance

    def test_frequency_controller_integration(self):
        """测试FrequencyController集成"""
        # 如果FrequencyController初始化成功
        if self.notifier.frequency_controller:
            # 模拟交易冷却期
            with patch.object(
                self.notifier.frequency_controller,
                '_check_cooldown',
                return_value=(True, datetime.now() + timedelta(hours=1))
            ):
                # 应该被抑制
                success, reason = self.notifier.send_trade_notification_with_cooldown(
                    symbol="BTCUSDT",
                    message="测试通知"
                )
                self.assertFalse(success)
                self.assertIn("交易冷却期内", reason)

    def test_multiple_notification_types(self):
        """测试多种通知类型"""
        # 发送交易通知
        success1, _ = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="交易通知"
        )
        self.assertTrue(success1)

        # 发送分析结果通知（不受交易冷却期影响）
        result = {
            'success': True,
            'signals': [
                {'币种': 'BTCUSDT', '开仓方向': '多', '信号等级': 'A', '开仓推荐度': 8}
            ]
        }
        self.notifier.send_analysis_result(result)

        # 验证飞书通知被调用
        self.assertTrue(self.mock_lark.send_text_message.called)

    def test_error_handling(self):
        """测试错误处理"""
        # 模拟飞书通知失败
        self.mock_lark.send_text_message.side_effect = Exception("网络错误")

        # 发送通知
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="测试通知"
        )

        # 验证失败处理
        self.assertFalse(success)
        self.assertIn("发送失败", reason)

    def test_suppressed_notifications_limit(self):
        """测试被抑制通知记录数量限制"""
        # 发送通知并产生大量被抑制的记录
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message="初始通知"
        )

        # 产生超过1000条被抑制记录
        for i in range(1100):
            self.notifier.send_trade_notification_with_cooldown(
                symbol="BTCUSDT",
                message=f"被抑制的通知 {i}"
            )

        # 验证只保留了最新的1000条
        suppressed = self.notifier.get_suppressed_notifications(limit=2000)
        self.assertLessEqual(len(suppressed), 1000)


class TestNotificationCooldownEdgeCases(unittest.TestCase):
    """通知冷却期边界情况测试"""

    def setUp(self):
        """测试前准备"""
        with patch('scheduler.notifier.LarkNotifier') as mock_lark:
            mock_lark_instance = Mock()
            mock_lark.return_value = mock_lark_instance
            self.notifier = NotificationManager()
            self.mock_lark = mock_lark_instance

    def test_empty_symbol(self):
        """测试空交易对"""
        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="",
            message="测试通知"
        )
        # 应该正常处理（不会崩溃）
        self.assertIsNotNone(success)

    def test_very_long_message(self):
        """测试超长消息"""
        long_message = "测试" * 1000  # 超长消息

        success, reason = self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message=long_message
        )

        # 应该正常处理
        self.assertTrue(success)

        # 验证被抑制通知的消息被截断
        self.notifier.send_trade_notification_with_cooldown(
            symbol="BTCUSDT",
            message=long_message
        )
        suppressed = self.notifier.get_suppressed_notifications()
        self.assertLessEqual(len(suppressed[0]['message']), 200)

    def test_concurrent_notifications(self):
        """测试并发通知（线程安全）"""
        import threading

        results = []

        def send_notification(symbol):
            success, reason = self.notifier.send_trade_notification_with_cooldown(
                symbol=symbol,
                message=f"{symbol}通知"
            )
            results.append((symbol, success))

        # 创建多个线程同时发送通知
        threads = []
        for i in range(10):
            t = threading.Thread(target=send_notification, args=(f"COIN{i}USDT",))
            threads.append(t)
            t.start()

        # 等待所有线程完成
        for t in threads:
            t.join()

        # 验证所有通知都被处理
        self.assertEqual(len(results), 10)

        # 所有通知都应该成功（不同交易对）
        successful = [r for r in results if r[1]]
        self.assertEqual(len(successful), 10)


if __name__ == '__main__':
    unittest.main()
