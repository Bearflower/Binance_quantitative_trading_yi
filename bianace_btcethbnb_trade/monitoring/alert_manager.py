#!/usr/bin/env python3
"""
告警管理器

负责告警规则管理和通知发送。

功能：
1. 告警规则管理
2. 告警级别定义
3. 告警通知发送（飞书、邮件等）
4. 告警历史记录
5. 告警抑制和聚合

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
from enum import Enum
from collections import defaultdict
import threading
import requests

from services.base import BaseService
from config.config_manager import get_config


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"           # 信息
    WARNING = "warning"     # 警告
    CRITICAL = "critical"   # 严重
    EMERGENCY = "emergency" # 紧急


class AlertRule:
    """
    告警规则

    定义告警触发条件和处理方式。
    """

    def __init__(
        self,
        name: str,
        metric: str,
        threshold: float,
        comparison: str = 'greater',  # greater, less, equal
        level: AlertLevel = AlertLevel.WARNING,
        message_template: str = None,
        cooldown_minutes: int = 30,
        enabled: bool = True
    ):
        """
        初始化告警规则

        Args:
            name: 规则名称
            metric: 指标名称
            threshold: 阈值
            comparison: 比较方式（greater/less/equal）
            level: 告警级别
            message_template: 消息模板
            cooldown_minutes: 冷却时间（分钟）
            enabled: 是否启用
        """
        self.name = name
        self.metric = metric
        self.threshold = threshold
        self.comparison = comparison
        self.level = level
        self.message_template = message_template or f"{metric} 触发告警: {{value}} {comparison} {threshold}"
        self.cooldown_minutes = cooldown_minutes
        self.enabled = enabled

        # 上次触发时间
        self.last_triggered: Optional[datetime] = None

    def should_trigger(self, value: float) -> bool:
        """
        判断是否应该触发告警

        Args:
            value: 指标值

        Returns:
            是否触发
        """
        if not self.enabled:
            return False

        # 检查冷却时间
        if self.last_triggered:
            cooldown = timedelta(minutes=self.cooldown_minutes)
            if datetime.now() - self.last_triggered < cooldown:
                return False

        # 检查阈值
        if self.comparison == 'greater':
            return value > self.threshold
        elif self.comparison == 'less':
            return value < self.threshold
        elif self.comparison == 'equal':
            return abs(value - self.threshold) < 0.0001

        return False

    def trigger(self, value: float) -> Dict[str, Any]:
        """
        触发告警

        Args:
            value: 指标值

        Returns:
            告警信息
        """
        self.last_triggered = datetime.now()

        return {
            'rule_name': self.name,
            'metric': self.metric,
            'value': value,
            'threshold': self.threshold,
            'level': self.level.value,
            'message': self.message_template.format(value=value),
            'triggered_at': self.last_triggered.isoformat()
        }


class AlertManager(BaseService):
    """
    告警管理器

    管理告警规则和通知发送。
    """

    def __init__(
        self,
        notification_url: Optional[str] = None,
        project_name: str = "btc_eth"
    ):
        """
        初始化告警管理器

        Args:
            notification_url: 通知服务URL
            project_name: 项目名称
        """
        super().__init__(
            service_name="AlertManager",
            auto_initialize=False
        )

        self.notification_url = notification_url
        self.project_name = project_name

        # 告警规则
        self._rules: Dict[str, AlertRule] = {}

        # 告警历史
        self._alert_history: List[Dict[str, Any]] = []

        # 告警统计
        self._alert_stats = defaultdict(int)

        # 被抑制的告警记录
        self._suppressed_alerts: List[Dict[str, Any]] = []

        # 线程锁
        self._lock = threading.Lock()

        # 初始化
        self.initialize()

    def _initialize(self):
        """初始化告警管理器"""
        self.log_info("初始化告警管理器")

        # 加载配置
        self._load_config()

        # 加载默认规则
        self._load_default_rules()

        self.log_info(f"告警管理器初始化完成，已加载 {len(self._rules)} 条规则")

    def _load_config(self):
        """加载配置"""
        # 通知服务配置
        self.notification_url = self.notification_url or get_config(
            'api.services.notification_url',
            'http://43.156.242.184:8766/api/v1'
        )
        self.project_name = get_config(
            'api.services.notification_project',
            'btc_eth'
        )

        # 告警配置
        self.enable_notification = get_config('monitoring.alert.enable_notification', True)
        self.max_history_size = get_config('monitoring.alert.max_history_size', 1000)

    def _load_default_rules(self):
        """加载默认告警规则"""
        # 系统告警规则
        self.add_rule(AlertRule(
            name="cpu_high",
            metric="cpu_usage",
            threshold=80.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            message_template="CPU使用率过高: {value:.1f}% > 80%",
            cooldown_minutes=30
        ))

        self.add_rule(AlertRule(
            name="memory_high",
            metric="memory_usage",
            threshold=80.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            message_template="内存使用率过高: {value:.1f}% > 80%",
            cooldown_minutes=30
        ))

        self.add_rule(AlertRule(
            name="disk_critical",
            metric="disk_usage",
            threshold=90.0,
            comparison='greater',
            level=AlertLevel.CRITICAL,
            message_template="磁盘使用率过高: {value:.1f}% > 90%",
            cooldown_minutes=60
        ))

        # 应用告警规则
        self.add_rule(AlertRule(
            name="api_success_rate_low",
            metric="api_success_rate",
            threshold=95.0,
            comparison='less',
            level=AlertLevel.WARNING,
            message_template="API成功率过低: {value:.2f}% < 95%",
            cooldown_minutes=30
        ))

        self.add_rule(AlertRule(
            name="response_time_high",
            metric="response_time",
            threshold=5.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            message_template="响应时间过长: {value:.3f}s > 5s",
            cooldown_minutes=30
        ))

        self.add_rule(AlertRule(
            name="error_rate_high",
            metric="error_rate",
            threshold=5.0,
            comparison='greater',
            level=AlertLevel.CRITICAL,
            message_template="错误率过高: {value:.2f}% > 5%",
            cooldown_minutes=30
        ))

        # 业务告警规则
        self.add_rule(AlertRule(
            name="trade_success_rate_low",
            metric="trade_success_rate",
            threshold=90.0,
            comparison='less',
            level=AlertLevel.WARNING,
            message_template="交易成功率过低: {value:.2f}% < 90%",
            cooldown_minutes=60
        ))

        self.add_rule(AlertRule(
            name="consecutive_losses",
            metric="consecutive_losses",
            threshold=5.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            message_template="连续亏损次数过多: {value:.0f} > 5",
            cooldown_minutes=120
        ))

        self.add_rule(AlertRule(
            name="capital_usage_high",
            metric="capital_usage",
            threshold=80.0,
            comparison='greater',
            level=AlertLevel.WARNING,
            message_template="资金使用率过高: {value:.2f}% > 80%",
            cooldown_minutes=60
        ))

    # ==================== 规则管理 ====================

    def add_rule(self, rule: AlertRule):
        """
        添加告警规则

        Args:
            rule: 告警规则
        """
        with self._lock:
            self._rules[rule.name] = rule
            self.log_debug(f"添加告警规则: {rule.name}")

    def remove_rule(self, rule_name: str):
        """
        移除告警规则

        Args:
            rule_name: 规则名称
        """
        with self._lock:
            if rule_name in self._rules:
                del self._rules[rule_name]
                self.log_debug(f"移除告警规则: {rule_name}")

    def get_rule(self, rule_name: str) -> Optional[AlertRule]:
        """
        获取告警规则

        Args:
            rule_name: 规则名称

        Returns:
            告警规则
        """
        return self._rules.get(rule_name)

    def get_all_rules(self) -> List[AlertRule]:
        """
        获取所有告警规则

        Returns:
            告警规则列表
        """
        return list(self._rules.values())

    def enable_rule(self, rule_name: str):
        """启用告警规则"""
        if rule_name in self._rules:
            self._rules[rule_name].enabled = True
            self.log_info(f"启用告警规则: {rule_name}")

    def disable_rule(self, rule_name: str):
        """禁用告警规则"""
        if rule_name in self._rules:
            self._rules[rule_name].enabled = False
            self.log_info(f"禁用告警规则: {rule_name}")

    # ==================== 告警处理 ====================

    def check_and_alert(
        self,
        metrics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        检查指标并触发告警

        Args:
            metrics: 指标数据

        Returns:
            触发的告警列表
        """
        triggered_alerts = []

        # 提取指标值
        metric_values = self._extract_metric_values(metrics)

        # 检查每条规则
        for rule in self._rules.values():
            if rule.metric in metric_values:
                value = metric_values[rule.metric]

                if rule.should_trigger(value):
                    alert = rule.trigger(value)
                    triggered_alerts.append(alert)

                    # 记录告警
                    self._record_alert(alert)

                    # 发送通知
                    if self.enable_notification:
                        self._send_notification(alert)

        return triggered_alerts

    def check_and_alert_with_cooldown(
        self,
        metrics: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        检查指标并触发告警（带冷却期检查）

        在冷却期内不发送飞书通知，只记录告警。

        Args:
            metrics: 指标数据

        Returns:
            (触发的告警列表, 被抑制的告警列表)
        """
        triggered_alerts = []
        suppressed_alerts = []

        # 提取指标值
        metric_values = self._extract_metric_values(metrics)

        # 检查每条规则
        for rule in self._rules.values():
            if rule.metric in metric_values:
                value = metric_values[rule.metric]

                # 检查是否应该触发（包含冷却期检查）
                if rule.should_trigger(value):
                    alert = rule.trigger(value)

                    # 检查是否在冷却期内
                    if self._is_in_alert_cooldown(rule.name):
                        # 在冷却期内，记录但不发送通知
                        self._record_suppressed_alert(alert, rule.cooldown_minutes)
                        suppressed_alerts.append(alert)
                        self.log_debug(f"告警 {rule.name} 在冷却期内，不发送通知")
                    else:
                        # 不在冷却期，正常处理
                        triggered_alerts.append(alert)
                        self._record_alert(alert)

                        # 发送通知
                        if self.enable_notification:
                            self._send_notification(alert)

        return triggered_alerts, suppressed_alerts

    def _extract_metric_values(self, metrics: Dict[str, Any]) -> Dict[str, float]:
        """
        从指标数据中提取指标值

        Args:
            metrics: 指标数据

        Returns:
            指标值字典
        """
        values = {}

        # 系统指标
        system = metrics.get('system', {})
        if 'cpu' in system:
            values['cpu_usage'] = system['cpu'].get('percent', 0)
        if 'memory' in system:
            values['memory_usage'] = system['memory'].get('percent', 0)
        if 'disk' in system:
            values['disk_usage'] = system['disk'].get('percent', 0)

        # 应用指标
        application = metrics.get('application', {})
        if 'api' in application:
            api = application['api']
            values['api_success_rate'] = api.get('success_rate', 100)
            values['response_time'] = api.get('avg_response_time', 0)
            values['error_rate'] = api.get('error_rate', 0)

        # 业务指标
        business = metrics.get('business', {})
        if 'trading' in business:
            trading = business['trading']
            values['trade_success_rate'] = trading.get('trade_success_rate', 100)

        if 'account' in business:
            account = business['account']
            values['capital_usage'] = account.get('capital_usage', 0)

        return values

    def _record_alert(self, alert: Dict[str, Any]):
        """
        记录告警

        Args:
            alert: 告警信息
        """
        with self._lock:
            self._alert_history.append(alert)

            # 限制历史记录大小
            if len(self._alert_history) > self.max_history_size:
                self._alert_history = self._alert_history[-self.max_history_size:]

            # 更新统计
            self._alert_stats[alert['rule_name']] += 1

        self.log_info(f"记录告警: {alert['rule_name']} - {alert['message']}")

    # ==================== 通知发送 ====================

    def _send_notification(self, alert: Dict[str, Any]):
        """
        发送告警通知

        Args:
            alert: 告警信息
        """
        try:
            # 构建通知消息
            level_emoji = {
                'info': 'ℹ️',
                'warning': '⚠️',
                'critical': '🔴',
                'emergency': '🚨'
            }

            emoji = level_emoji.get(alert['level'], '⚠️')

            message = f"""
{emoji} **系统告警通知**

**告警级别**: {alert['level'].upper()}
**告警规则**: {alert['rule_name']}
**告警指标**: {alert['metric']}
**当前值**: {alert['value']}
**阈值**: {alert['threshold']}
**触发时间**: {alert['triggered_at']}

**告警信息**: {alert['message']}

请及时处理！
            """.strip()

            # 调用通知服务
            self._call_notification_service(message, alert['level'])

        except Exception as e:
            self.log_error(f"发送告警通知失败: {e}")

    def _call_notification_service(self, message: str, level: str):
        """
        调用通知服务

        Args:
            message: 消息内容
            level: 告警级别
        """
        try:
            payload = {
                "project": self.project_name,
                "message": message,
                "type": "text",
                "level": level
            }

            response = requests.post(
                f"{self.notification_url}/send",
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    self.log_debug("告警通知发送成功")
                else:
                    self.log_error(f"告警通知发送失败: {result.get('message')}")
            else:
                self.log_error(f"告警通知HTTP错误: {response.status_code}")

        except requests.exceptions.Timeout:
            self.log_error("告警通知发送超时")
        except Exception as e:
            self.log_error(f"告警通知发送异常: {e}")

    # ==================== 手动告警 ====================

    def send_custom_alert(
        self,
        title: str,
        message: str,
        level: AlertLevel = AlertLevel.WARNING
    ):
        """
        发送自定义告警

        Args:
            title: 告警标题
            message: 告警消息
            level: 告警级别
        """
        alert = {
            'rule_name': 'custom',
            'metric': 'manual',
            'value': 0,
            'threshold': 0,
            'level': level.value,
            'message': f"{title}: {message}",
            'triggered_at': datetime.now().isoformat()
        }

        # 记录告警
        self._record_alert(alert)

        # 发送通知
        if self.enable_notification:
            self._send_notification(alert)

    # ==================== 告警查询 ====================

    def get_alert_history(
        self,
        limit: int = 100,
        level: Optional[str] = None,
        rule_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取告警历史

        Args:
            limit: 返回记录数
            level: 告警级别（可选）
            rule_name: 规则名称（可选）

        Returns:
            告警历史列表
        """
        with self._lock:
            alerts = self._alert_history.copy()

        # 过滤
        if level:
            alerts = [a for a in alerts if a['level'] == level]

        if rule_name:
            alerts = [a for a in alerts if a['rule_name'] == rule_name]

        # 返回最近的记录
        return alerts[-limit:]

    def get_alert_stats(self) -> Dict[str, int]:
        """
        获取告警统计

        Returns:
            告警统计字典
        """
        with self._lock:
            return dict(self._alert_stats)

    def clear_alert_history(self):
        """清空告警历史"""
        with self._lock:
            self._alert_history.clear()
            self._alert_stats.clear()

        self.log_info("告警历史已清空")

    # ==================== 告警抑制 ====================

    def suppress_alert(
        self,
        rule_name: str,
        duration_minutes: int = 60
    ):
        """
        抑制告警

        Args:
            rule_name: 规则名称
            duration_minutes: 抑制时长（分钟）
        """
        if rule_name in self._rules:
            rule = self._rules[rule_name]
            rule.last_triggered = datetime.now() + timedelta(minutes=duration_minutes)
            self.log_info(f"抑制告警 {rule_name} {duration_minutes} 分钟")

    def unsuppress_alert(self, rule_name: str):
        """
        取消告警抑制

        Args:
            rule_name: 规则名称
        """
        if rule_name in self._rules:
            rule = self._rules[rule_name]
            rule.last_triggered = None
            self.log_info(f"取消告警抑制: {rule_name}")

    # ==================== 告警冷却期检查 ====================

    def _is_in_alert_cooldown(self, rule_name: str) -> bool:
        """
        检查告警是否在冷却期内

        Args:
            rule_name: 规则名称

        Returns:
            是否在冷却期内
        """
        if rule_name not in self._rules:
            return False

        rule = self._rules[rule_name]

        # 如果没有上次触发时间，不在冷却期
        if not rule.last_triggered:
            return False

        # 检查冷却时间
        cooldown = timedelta(minutes=rule.cooldown_minutes)
        time_since_last = datetime.now() - rule.last_triggered

        return time_since_last < cooldown

    def _record_suppressed_alert(self, alert: Dict[str, Any], cooldown_minutes: int):
        """
        记录被抑制的告警

        Args:
            alert: 告警信息
            cooldown_minutes: 冷却时间（分钟）
        """
        with self._lock:
            suppressed_record = {
                **alert,
                'suppressed_at': datetime.now().isoformat(),
                'cooldown_minutes': cooldown_minutes,
                'reason': '告警冷却期内'
            }
            self._suppressed_alerts.append(suppressed_record)

            # 限制记录大小
            if len(self._suppressed_alerts) > self.max_history_size:
                self._suppressed_alerts = self._suppressed_alerts[-self.max_history_size:]

        self.log_info(f"记录被抑制的告警: {alert['rule_name']} - 冷却期 {cooldown_minutes} 分钟")

    def get_suppressed_alerts(
        self,
        limit: int = 100,
        rule_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取被抑制的告警记录

        Args:
            limit: 返回记录数
            rule_name: 规则名称（可选）

        Returns:
            被抑制的告警列表
        """
        with self._lock:
            alerts = self._suppressed_alerts.copy()

        # 过滤
        if rule_name:
            alerts = [a for a in alerts if a['rule_name'] == rule_name]

        # 返回最近的记录
        return alerts[-limit:]

    def clear_suppressed_alerts(self):
        """清空被抑制的告警记录"""
        with self._lock:
            self._suppressed_alerts.clear()

        self.log_info("被抑制的告警记录已清空")
