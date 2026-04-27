#!/usr/bin/env python3
"""
告警冷却期测试

测试AlertManager的冷却期功能：
1. 告警冷却期检查
2. 被抑制告警记录
3. 查询接口

版本: v1.0.0
创建时间: 2026-04-27
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from monitoring.alert_manager import AlertManager, AlertRule, AlertLevel


class TestAlertCooldown(unittest.TestCase):
    """告警冷却期测试"""

    def setUp(self):
        """测试前准备"""
        # 创建AlertManager实例（禁用通知）
        self.alert_manager = AlertManager(
            notification_url=None,
            project_name="test_project"
        )
        self.alert_manager.enable_notification = False

    def test_alert_rule_cooldown(self):
        """测试告警规则冷却期"""
        # 创建告警规则（冷却期1分钟）
        rule = AlertRule(
            name="test_rule",
            metric="cpu_usage",
            threshold=80.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            cooldown_minutes=1
        )

        # 第一次触发
        self.assertTrue(rule.should_trigger(85.0))
        alert = rule.trigger(85.0)
        self.assertIsNotNone(alert)
        self.assertIsNotNone(rule.last_triggered)

        # 在冷却期内，不应触发
        self.assertFalse(rule.should_trigger(90.0))

    def test_alert_cooldown_check(self):
        """测试告警冷却期检查"""
        # 添加测试规则
        rule = AlertRule(
            name="test_cooldown",
            metric="test_metric",
            threshold=50.0,
            cooldown_minutes=5
        )
        self.alert_manager.add_rule(rule)

        # 第一次检查，不在冷却期
        self.assertFalse(self.alert_manager._is_in_alert_cooldown("test_cooldown"))

        # 触发告警
        rule.last_triggered = datetime.now()

        # 现在应该在冷却期
        self.assertTrue(self.alert_manager._is_in_alert_cooldown("test_cooldown"))

    def test_suppressed_alert_recording(self):
        """测试被抑制告警记录"""
        # 创建告警信息
        alert = {
            'rule_name': 'test_rule',
            'metric': 'test_metric',
            'value': 85.0,
            'threshold': 80.0,
            'level': 'warning',
            'message': '测试告警',
            'triggered_at': datetime.now().isoformat()
        }

        # 记录被抑制的告警
        self.alert_manager._record_suppressed_alert(alert, cooldown_minutes=30)

        # 查询被抑制的告警
        suppressed = self.alert_manager.get_suppressed_alerts()
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]['rule_name'], 'test_rule')
        self.assertEqual(suppressed[0]['cooldown_minutes'], 30)
        self.assertEqual(suppressed[0]['reason'], '告警冷却期内')

    def test_check_and_alert_with_cooldown(self):
        """测试带冷却期的告警检查"""
        # 添加测试规则
        rule = AlertRule(
            name="cooldown_test",
            metric="test_value",
            threshold=100.0,
            cooldown_minutes=60
        )
        self.alert_manager.add_rule(rule)

        # 准备指标数据
        metrics = {
            'system': {},
            'application': {},
            'business': {}
        }

        # 手动设置指标值
        self.alert_manager._extract_metric_values = Mock(return_value={'test_value': 150.0})

        # 第一次检查
        triggered, suppressed = self.alert_manager.check_and_alert_with_cooldown(metrics)
        self.assertEqual(len(triggered), 1)
        self.assertEqual(len(suppressed), 0)

        # 第二次检查（在冷却期内）
        triggered, suppressed = self.alert_manager.check_and_alert_with_cooldown(metrics)
        self.assertEqual(len(triggered), 0)
        self.assertEqual(len(suppressed), 1)

    def test_get_suppressed_alerts_filtering(self):
        """测试被抑制告警的过滤查询"""
        # 记录多个被抑制的告警
        for i in range(5):
            alert = {
                'rule_name': f'rule_{i % 2}',
                'metric': 'test_metric',
                'value': 85.0,
                'threshold': 80.0,
                'level': 'warning',
                'message': f'测试告警 {i}',
                'triggered_at': datetime.now().isoformat()
            }
            self.alert_manager._record_suppressed_alert(alert, cooldown_minutes=30)

        # 查询所有
        all_suppressed = self.alert_manager.get_suppressed_alerts()
        self.assertEqual(len(all_suppressed), 5)

        # 按规则名称过滤
        rule_0_suppressed = self.alert_manager.get_suppressed_alerts(rule_name='rule_0')
        self.assertEqual(len(rule_0_suppressed), 3)  # 0, 2, 4

    def test_clear_suppressed_alerts(self):
        """测试清空被抑制告警"""
        # 记录一些被抑制的告警
        alert = {
            'rule_name': 'test_rule',
            'metric': 'test_metric',
            'value': 85.0,
            'threshold': 80.0,
            'level': 'warning',
            'message': '测试告警',
            'triggered_at': datetime.now().isoformat()
        }
        self.alert_manager._record_suppressed_alert(alert, cooldown_minutes=30)

        # 清空
        self.alert_manager.clear_suppressed_alerts()

        # 验证已清空
        suppressed = self.alert_manager.get_suppressed_alerts()
        self.assertEqual(len(suppressed), 0)

    def test_cooldown_expiration(self):
        """测试冷却期过期"""
        # 创建规则并设置上次触发时间为2小时前
        rule = AlertRule(
            name="expired_cooldown",
            metric="test_metric",
            threshold=50.0,
            cooldown_minutes=60  # 1小时冷却期
        )
        rule.last_triggered = datetime.now() - timedelta(hours=2)
        self.alert_manager.add_rule(rule)

        # 冷却期已过期
        self.assertFalse(self.alert_manager._is_in_alert_cooldown("expired_cooldown"))


class TestAlertManagerIntegration(unittest.TestCase):
    """AlertManager集成测试"""

    def setUp(self):
        """测试前准备"""
        self.alert_manager = AlertManager(
            notification_url=None,
            project_name="test_project"
        )

    def test_multiple_rules_cooldown(self):
        """测试多个规则的冷却期管理"""
        # 添加多个规则
        rules = [
            AlertRule("rule1", "metric1", 80, cooldown_minutes=30),
            AlertRule("rule2", "metric2", 90, cooldown_minutes=60),
            AlertRule("rule3", "metric3", 70, cooldown_minutes=15)
        ]

        for rule in rules:
            self.alert_manager.add_rule(rule)

        # 触发所有规则
        for rule in rules:
            rule.last_triggered = datetime.now()

        # 验证所有规则都在冷却期
        for rule in rules:
            self.assertTrue(self.alert_manager._is_in_alert_cooldown(rule.name))

    def test_suppressed_alerts_limit(self):
        """测试被抑制告警记录数量限制"""
        # 设置较小的历史记录大小
        self.alert_manager.max_history_size = 10

        # 记录超过限制的告警
        for i in range(15):
            alert = {
                'rule_name': f'rule_{i}',
                'metric': 'test_metric',
                'value': 85.0,
                'threshold': 80.0,
                'level': 'warning',
                'message': f'测试告警 {i}',
                'triggered_at': datetime.now().isoformat()
            }
            self.alert_manager._record_suppressed_alert(alert, cooldown_minutes=30)

        # 验证只保留了最新的10条
        suppressed = self.alert_manager.get_suppressed_alerts(limit=100)
        self.assertEqual(len(suppressed), 10)


if __name__ == '__main__':
    unittest.main()
