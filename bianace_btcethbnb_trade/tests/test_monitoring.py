#!/usr/bin/env python3
"""
监控系统集成测试

测试监控系统的各个组件和集成功能。

版本: v1.0.0
创建时间: 2026-04-27
"""

import unittest
import time
from decimal import Decimal
from datetime import datetime

from monitoring.metrics_collector import MetricsCollector
from monitoring.alert_manager import AlertManager, AlertLevel, AlertRule
from monitoring.health_checker import HealthChecker


class TestMetricsCollector(unittest.TestCase):
    """测试指标收集器"""

    @classmethod
    def setUpClass(cls):
        """设置测试类"""
        cls.collector = MetricsCollector(collection_interval=10, history_size=100)

    def test_collect_system_metrics(self):
        """测试系统指标收集"""
        metrics = self.collector.collect_system_metrics()

        # 验证返回的数据结构
        self.assertIn('timestamp', metrics)
        self.assertIn('cpu', metrics)
        self.assertIn('memory', metrics)
        self.assertIn('disk', metrics)

        # 验证CPU指标
        cpu = metrics['cpu']
        self.assertIn('percent', cpu)
        self.assertGreaterEqual(cpu['percent'], 0)
        self.assertLessEqual(cpu['percent'], 100)

        # 验证内存指标
        memory = metrics['memory']
        self.assertIn('percent', memory)
        self.assertGreaterEqual(memory['percent'], 0)
        self.assertLessEqual(memory['percent'], 100)

        # 验证磁盘指标
        disk = metrics['disk']
        self.assertIn('percent', disk)
        self.assertGreaterEqual(disk['percent'], 0)
        self.assertLessEqual(disk['percent'], 100)

    def test_record_api_request(self):
        """测试API请求记录"""
        # 记录成功的请求
        self.collector.record_api_request(
            endpoint='/api/test',
            success=True,
            response_time=0.5
        )

        # 记录失败的请求
        self.collector.record_api_request(
            endpoint='/api/test',
            success=False,
            response_time=1.0,
            error='Timeout'
        )

        # 收集应用指标
        metrics = self.collector.collect_app_metrics()

        # 验证返回的数据结构
        self.assertIn('api', metrics)
        api = metrics['api']
        self.assertIn('total_requests', api)
        self.assertIn('successful_requests', api)
        self.assertIn('failed_requests', api)
        self.assertIn('success_rate', api)

        # 验证统计正确性
        self.assertEqual(api['total_requests'], 2)
        self.assertEqual(api['successful_requests'], 1)
        self.assertEqual(api['failed_requests'], 1)

    def test_record_trade(self):
        """测试交易记录"""
        # 记录成功的盈利交易
        self.collector.record_trade(
            symbol='BTCUSDT',
            success=True,
            pnl=Decimal('10.5')
        )

        # 记录成功的亏损交易
        self.collector.record_trade(
            symbol='ETHUSDT',
            success=True,
            pnl=Decimal('-5.2')
        )

        # 收集业务指标
        metrics = self.collector.collect_business_metrics()

        # 验证返回的数据结构
        self.assertIn('trading', metrics)
        trading = metrics['trading']
        self.assertIn('total_trades', trading)
        self.assertIn('successful_trades', trading)
        self.assertIn('win_rate', trading)

    def test_collect_all_metrics(self):
        """测试收集所有指标"""
        metrics = self.collector.collect_all_metrics()

        # 验证返回的数据结构
        self.assertIn('timestamp', metrics)
        self.assertIn('system', metrics)
        self.assertIn('application', metrics)
        self.assertIn('business', metrics)
        self.assertIn('alerts', metrics)

    def test_get_metrics_summary(self):
        """测试获取指标摘要"""
        # 先收集指标
        self.collector.collect_all_metrics()

        # 获取摘要
        summary = self.collector.get_metrics_summary()

        # 验证返回的数据结构
        self.assertIn('timestamp', summary)
        self.assertIn('system', summary)
        self.assertIn('application', summary)
        self.assertIn('business', summary)

    def test_metrics_history(self):
        """测试指标历史记录"""
        # 收集多次指标
        for _ in range(5):
            self.collector.collect_system_metrics()
            time.sleep(0.1)

        # 获取历史记录
        history = self.collector.get_system_metrics_history(limit=10)

        # 验证历史记录
        self.assertGreater(len(history), 0)
        self.assertLessEqual(len(history), 10)


class TestAlertManager(unittest.TestCase):
    """测试告警管理器"""

    @classmethod
    def setUpClass(cls):
        """设置测试类"""
        cls.alert_manager = AlertManager()

    def test_add_and_remove_rule(self):
        """测试添加和移除告警规则"""
        # 添加规则
        rule = AlertRule(
            name="test_rule",
            metric="test_metric",
            threshold=50.0,
            comparison='greater',
            level=AlertLevel.WARNING
        )

        self.alert_manager.add_rule(rule)

        # 验证规则已添加
        retrieved_rule = self.alert_manager.get_rule("test_rule")
        self.assertIsNotNone(retrieved_rule)
        self.assertEqual(retrieved_rule.name, "test_rule")

        # 移除规则
        self.alert_manager.remove_rule("test_rule")

        # 验证规则已移除
        retrieved_rule = self.alert_manager.get_rule("test_rule")
        self.assertIsNone(retrieved_rule)

    def test_enable_disable_rule(self):
        """测试启用和禁用告警规则"""
        # 添加规则
        rule = AlertRule(
            name="test_rule_2",
            metric="test_metric",
            threshold=50.0,
            comparison='greater',
            level=AlertLevel.WARNING
        )

        self.alert_manager.add_rule(rule)

        # 禁用规则
        self.alert_manager.disable_rule("test_rule_2")
        retrieved_rule = self.alert_manager.get_rule("test_rule_2")
        self.assertFalse(retrieved_rule.enabled)

        # 启用规则
        self.alert_manager.enable_rule("test_rule_2")
        retrieved_rule = self.alert_manager.get_rule("test_rule_2")
        self.assertTrue(retrieved_rule.enabled)

        # 清理
        self.alert_manager.remove_rule("test_rule_2")

    def test_check_and_alert(self):
        """测试检查指标并触发告警"""
        # 构造测试指标数据
        metrics = {
            'system': {
                'cpu': {'percent': 85.0},  # 超过阈值80%
                'memory': {'percent': 70.0},
                'disk': {'percent': 60.0}
            },
            'application': {
                'api': {
                    'success_rate': 98.0,
                    'avg_response_time': 3.0,
                    'error_rate': 2.0
                }
            },
            'business': {
                'trading': {
                    'trade_success_rate': 95.0
                },
                'account': {
                    'capital_usage': 70.0
                }
            }
        }

        # 检查告警
        alerts = self.alert_manager.check_and_alert(metrics)

        # 验证告警被触发
        self.assertGreater(len(alerts), 0)

        # 验证CPU告警
        cpu_alert = next((a for a in alerts if a['metric'] == 'cpu_usage'), None)
        self.assertIsNotNone(cpu_alert)
        self.assertEqual(cpu_alert['level'], 'warning')

    def test_send_custom_alert(self):
        """测试发送自定义告警"""
        # 发送自定义告警
        self.alert_manager.send_custom_alert(
            title="测试告警",
            message="这是一个测试告警",
            level=AlertLevel.WARNING
        )

        # 验证告警历史
        history = self.alert_manager.get_alert_history(limit=10)
        self.assertGreater(len(history), 0)

        # 验证最新告警
        latest_alert = history[-1]
        self.assertEqual(latest_alert['rule_name'], 'custom')
        self.assertIn('测试告警', latest_alert['message'])

    def test_alert_history(self):
        """测试告警历史记录"""
        # 获取告警历史
        history = self.alert_manager.get_alert_history(limit=100)

        # 验证历史记录是列表
        self.assertIsInstance(history, list)

    def test_alert_stats(self):
        """测试告警统计"""
        # 获取告警统计
        stats = self.alert_manager.get_alert_stats()

        # 验证统计是字典
        self.assertIsInstance(stats, dict)

    def test_alert_suppression(self):
        """测试告警抑制"""
        # 添加规则
        rule = AlertRule(
            name="test_suppression",
            metric="test_metric",
            threshold=50.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            cooldown_minutes=60
        )

        self.alert_manager.add_rule(rule)

        # 抑制告警
        self.alert_manager.suppress_alert("test_suppression", duration_minutes=30)

        # 验证抑制状态
        retrieved_rule = self.alert_manager.get_rule("test_suppression")
        self.assertIsNotNone(retrieved_rule.last_triggered)

        # 取消抑制
        self.alert_manager.unsuppress_alert("test_suppression")
        retrieved_rule = self.alert_manager.get_rule("test_suppression")
        self.assertIsNone(retrieved_rule.last_triggered)

        # 清理
        self.alert_manager.remove_rule("test_suppression")


class TestHealthChecker(unittest.TestCase):
    """测试健康检查器"""

    @classmethod
    def setUpClass(cls):
        """设置测试类"""
        cls.health_checker = HealthChecker()

    def test_check_system_health(self):
        """测试系统健康检查"""
        result = self.health_checker.check_system_health()

        # 验证返回的数据结构
        self.assertIn('status', result)
        self.assertIn('cpu', result)
        self.assertIn('memory', result)
        self.assertIn('disk', result)

        # 验证状态值
        self.assertIn(result['status'], ['healthy', 'degraded', 'unhealthy', 'unknown'])

    def test_check_notification_service(self):
        """测试通知服务健康检查"""
        result = self.health_checker.check_notification_service()

        # 验证返回的数据结构
        self.assertIn('status', result)
        self.assertIn('url', result)

        # 验证状态值
        self.assertIn(result['status'], ['healthy', 'degraded', 'unhealthy', 'unknown'])

    def test_check_kline_service(self):
        """测试K线服务健康检查"""
        result = self.health_checker.check_kline_service()

        # 验证返回的数据结构
        self.assertIn('status', result)
        self.assertIn('url', result)

        # 验证状态值
        self.assertIn(result['status'], ['healthy', 'degraded', 'unhealthy', 'unknown'])

    def test_check_binance_api(self):
        """测试币安API健康检查"""
        result = self.health_checker.check_binance_api()

        # 验证返回的数据结构
        self.assertIn('status', result)
        self.assertIn('url', result)

        # 验证状态值
        self.assertIn(result['status'], ['healthy', 'degraded', 'unhealthy', 'unknown'])

    def test_check_health(self):
        """测试完整健康检查"""
        report = self.health_checker.check_health()

        # 验证返回的数据结构
        self.assertIn('timestamp', report)
        self.assertIn('overall_status', report)
        self.assertIn('checks', report)
        self.assertIn('check_duration_ms', report)

        # 验证总体状态
        self.assertIn(
            report['overall_status'],
            ['healthy', 'degraded', 'unhealthy', 'unknown']
        )

        # 验证各项检查
        checks = report['checks']
        self.assertIn('system', checks)
        self.assertIn('database', checks)
        self.assertIn('notification_service', checks)
        self.assertIn('kline_service', checks)
        self.assertIn('binance_api', checks)

    def test_quick_health_check(self):
        """测试快速健康检查"""
        report = self.health_checker.quick_health_check()

        # 验证返回的数据结构
        self.assertIn('timestamp', report)
        self.assertIn('overall_status', report)
        self.assertIn('checks', report)

        # 验证只检查关键服务
        checks = report['checks']
        self.assertIn('system', checks)
        self.assertIn('binance_api', checks)

    def test_get_health_report(self):
        """测试获取健康报告"""
        report_text = self.health_checker.get_health_report()

        # 验证报告是字符串
        self.assertIsInstance(report_text, str)

        # 验证报告包含关键信息
        self.assertIn('系统健康检查报告', report_text)
        self.assertIn('总体状态', report_text)
        self.assertIn('系统状态', report_text)

    def test_is_healthy(self):
        """测试健康状态判断"""
        is_healthy = self.health_checker.is_healthy()

        # 验证返回布尔值
        self.assertIsInstance(is_healthy, bool)


class TestMonitoringIntegration(unittest.TestCase):
    """测试监控系统集成"""

    @classmethod
    def setUpClass(cls):
        """设置测试类"""
        cls.collector = MetricsCollector(collection_interval=10, history_size=100)
        cls.alert_manager = AlertManager()
        cls.health_checker = HealthChecker()

    def test_full_monitoring_workflow(self):
        """测试完整的监控工作流"""
        # 1. 收集指标
        metrics = self.collector.collect_all_metrics()
        self.assertIsNotNone(metrics)

        # 2. 检查告警
        alerts = self.alert_manager.check_and_alert(metrics)
        self.assertIsInstance(alerts, list)

        # 3. 健康检查
        health_report = self.health_checker.check_health()
        self.assertIsNotNone(health_report)

        # 4. 获取指标摘要
        summary = self.collector.get_metrics_summary()
        self.assertIsNotNone(summary)

        # 5. 获取告警历史
        alert_history = self.alert_manager.get_alert_history(limit=10)
        self.assertIsInstance(alert_history, list)

        # 6. 获取健康报告
        health_report_text = self.health_checker.get_health_report()
        self.assertIsInstance(health_report_text, str)


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
