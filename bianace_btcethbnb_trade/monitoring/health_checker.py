#!/usr/bin/env python3
"""
健康检查器

负责系统健康状态检查。

功能：
1. 系统健康检查
2. 服务依赖检查
3. 数据库连接检查
4. API服务检查
5. 健康报告生成

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
import time
import requests
from datetime import datetime
from typing import Dict, Any, List, Optional
from enum import Enum

from services.base import BaseService
from config.config_manager import get_config


class HealthStatus(Enum):
    """健康状态"""
    HEALTHY = "healthy"         # 健康
    DEGRADED = "degraded"       # 降级
    UNHEALTHY = "unhealthy"     # 不健康
    UNKNOWN = "unknown"         # 未知


class HealthChecker(BaseService):
    """
    健康检查器

    检查系统各组件的健康状态。
    """

    def __init__(self):
        """初始化健康检查器"""
        super().__init__(
            service_name="HealthChecker",
            auto_initialize=False
        )

        # 检查结果缓存
        self._health_cache: Dict[str, Any] = {}

        # 上次检查时间
        self._last_check_time: Optional[datetime] = None

        # 初始化
        self.initialize()

    def _initialize(self):
        """初始化健康检查器"""
        self.log_info("初始化健康检查器")

        # 加载配置
        self._load_config()

        self.log_info("健康检查器初始化完成")

    def _load_config(self):
        """加载配置"""
        # 服务依赖配置
        self.notification_url = get_config(
            'api.services.notification_url',
            'http://43.156.242.184:8766/api/v1'
        )
        self.kline_url = get_config(
            'api.services.kline_url',
            'http://43.156.242.184:8765/api/v1'
        )

        # 币安API配置
        self.binance_api_url = get_config(
            'api.binance.base_url',
            'https://papi.binance.com'
        )

        # 健康检查超时时间
        self.check_timeout = get_config('monitoring.health_check.timeout', 10)

    # ==================== 综合健康检查 ====================

    def check_health(self) -> Dict[str, Any]:
        """
        执行完整的健康检查

        Returns:
            健康检查报告
        """
        start_time = time.time()

        # 执行各项检查
        checks = {
            'system': self.check_system_health(),
            'database': self.check_database_health(),
            'notification_service': self.check_notification_service(),
            'kline_service': self.check_kline_service(),
            'binance_api': self.check_binance_api()
        }

        # 计算总体状态
        overall_status = self._calculate_overall_status(checks)

        # 构建报告
        report = {
            'timestamp': datetime.now().isoformat(),
            'overall_status': overall_status.value,
            'checks': checks,
            'check_duration_ms': round((time.time() - start_time) * 1000, 2)
        }

        # 更新缓存
        self._health_cache = report
        self._last_check_time = datetime.now()

        return report

    def _calculate_overall_status(self, checks: Dict[str, Any]) -> HealthStatus:
        """
        计算总体健康状态

        Args:
            checks: 各项检查结果

        Returns:
            总体健康状态
        """
        statuses = [check['status'] for check in checks.values()]

        # 如果有任何不健康状态，返回不健康
        if HealthStatus.UNHEALTHY.value in statuses:
            return HealthStatus.UNHEALTHY

        # 如果有任何降级状态，返回降级
        if HealthStatus.DEGRADED.value in statuses:
            return HealthStatus.DEGRADED

        # 如果全部健康，返回健康
        if all(s == HealthStatus.HEALTHY.value for s in statuses):
            return HealthStatus.HEALTHY

        # 其他情况返回未知
        return HealthStatus.UNKNOWN

    # ==================== 系统健康检查 ====================

    def check_system_health(self) -> Dict[str, Any]:
        """
        检查系统健康状态

        Returns:
            系统健康检查结果
        """
        try:
            import psutil

            # CPU检查
            cpu_percent = psutil.cpu_percent(interval=1)
            cpu_status = HealthStatus.HEALTHY.value
            if cpu_percent > 80:
                cpu_status = HealthStatus.DEGRADED.value
            elif cpu_percent > 90:
                cpu_status = HealthStatus.UNHEALTHY.value

            # 内存检查
            memory = psutil.virtual_memory()
            memory_status = HealthStatus.HEALTHY.value
            if memory.percent > 80:
                memory_status = HealthStatus.DEGRADED.value
            elif memory.percent > 90:
                memory_status = HealthStatus.UNHEALTHY.value

            # 磁盘检查
            disk = psutil.disk_usage('/')
            disk_status = HealthStatus.HEALTHY.value
            if disk.percent > 80:
                disk_status = HealthStatus.DEGRADED.value
            elif disk.percent > 90:
                disk_status = HealthStatus.UNHEALTHY.value

            # 计算总体状态
            statuses = [cpu_status, memory_status, disk_status]
            if HealthStatus.UNHEALTHY.value in statuses:
                overall = HealthStatus.UNHEALTHY.value
            elif HealthStatus.DEGRADED.value in statuses:
                overall = HealthStatus.DEGRADED.value
            else:
                overall = HealthStatus.HEALTHY.value

            return {
                'status': overall,
                'cpu': {
                    'percent': cpu_percent,
                    'status': cpu_status
                },
                'memory': {
                    'percent': memory.percent,
                    'available_gb': round(memory.available / (1024 ** 3), 2),
                    'status': memory_status
                },
                'disk': {
                    'percent': disk.percent,
                    'free_gb': round(disk.free / (1024 ** 3), 2),
                    'status': disk_status
                }
            }

        except Exception as e:
            self.log_error(f"系统健康检查失败: {e}")
            return {
                'status': HealthStatus.UNKNOWN.value,
                'error': str(e)
            }

    # ==================== 数据库健康检查 ====================

    def check_database_health(self) -> Dict[str, Any]:
        """
        检查数据库健康状态

        Returns:
            数据库健康检查结果
        """
        try:
            # 尝试导入数据库模块
            from models.database import get_database_connection

            # 获取数据库连接
            conn = get_database_connection()

            if conn:
                # 执行简单查询
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
                conn.close()

                return {
                    'status': HealthStatus.HEALTHY.value,
                    'message': '数据库连接正常'
                }
            else:
                return {
                    'status': HealthStatus.UNHEALTHY.value,
                    'message': '无法获取数据库连接'
                }

        except ImportError:
            # 如果数据库模块不存在，返回未知状态
            return {
                'status': HealthStatus.UNKNOWN.value,
                'message': '数据库模块未配置'
            }
        except Exception as e:
            self.log_error(f"数据库健康检查失败: {e}")
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'error': str(e)
            }

    # ==================== 服务依赖检查 ====================

    def check_notification_service(self) -> Dict[str, Any]:
        """
        检查通知服务健康状态

        Returns:
            通知服务健康检查结果
        """
        try:
            # 尝试访问通知服务健康检查端点
            response = requests.get(
                f"{self.notification_url}/health",
                timeout=self.check_timeout
            )

            if response.status_code == 200:
                return {
                    'status': HealthStatus.HEALTHY.value,
                    'url': self.notification_url,
                    'response_time_ms': round(response.elapsed.total_seconds() * 1000, 2)
                }
            else:
                return {
                    'status': HealthStatus.DEGRADED.value,
                    'url': self.notification_url,
                    'http_status': response.status_code,
                    'message': f'HTTP状态码异常: {response.status_code}'
                }

        except requests.exceptions.Timeout:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.notification_url,
                'message': '连接超时'
            }
        except requests.exceptions.ConnectionError:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.notification_url,
                'message': '无法连接到服务'
            }
        except Exception as e:
            self.log_error(f"通知服务健康检查失败: {e}")
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'error': str(e)
            }

    def check_kline_service(self) -> Dict[str, Any]:
        """
        检查K线服务健康状态

        Returns:
            K线服务健康检查结果
        """
        try:
            # 尝试访问K线服务健康检查端点
            response = requests.get(
                f"{self.kline_url}/health",
                timeout=self.check_timeout
            )

            if response.status_code == 200:
                return {
                    'status': HealthStatus.HEALTHY.value,
                    'url': self.kline_url,
                    'response_time_ms': round(response.elapsed.total_seconds() * 1000, 2)
                }
            else:
                return {
                    'status': HealthStatus.DEGRADED.value,
                    'url': self.kline_url,
                    'http_status': response.status_code,
                    'message': f'HTTP状态码异常: {response.status_code}'
                }

        except requests.exceptions.Timeout:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.kline_url,
                'message': '连接超时'
            }
        except requests.exceptions.ConnectionError:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.kline_url,
                'message': '无法连接到服务'
            }
        except Exception as e:
            self.log_error(f"K线服务健康检查失败: {e}")
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'error': str(e)
            }

    def check_binance_api(self) -> Dict[str, Any]:
        """
        检查币安API健康状态

        Returns:
            币安API健康检查结果
        """
        try:
            # 尝试访问币安API（公开端点）
            response = requests.get(
                f"{self.binance_api_url}/papi/v1/time",
                timeout=self.check_timeout
            )

            if response.status_code == 200:
                data = response.json()
                server_time = data.get('serverTime')

                return {
                    'status': HealthStatus.HEALTHY.value,
                    'url': self.binance_api_url,
                    'server_time': server_time,
                    'response_time_ms': round(response.elapsed.total_seconds() * 1000, 2)
                }
            else:
                return {
                    'status': HealthStatus.DEGRADED.value,
                    'url': self.binance_api_url,
                    'http_status': response.status_code,
                    'message': f'HTTP状态码异常: {response.status_code}'
                }

        except requests.exceptions.Timeout:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.binance_api_url,
                'message': '连接超时'
            }
        except requests.exceptions.ConnectionError:
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'url': self.binance_api_url,
                'message': '无法连接到币安API'
            }
        except Exception as e:
            self.log_error(f"币安API健康检查失败: {e}")
            return {
                'status': HealthStatus.UNHEALTHY.value,
                'error': str(e)
            }

    # ==================== 快速健康检查 ====================

    def quick_health_check(self) -> Dict[str, Any]:
        """
        快速健康检查（仅检查关键服务）

        Returns:
            快速健康检查结果
        """
        try:
            # 只检查关键服务
            checks = {
                'system': self.check_system_health(),
                'binance_api': self.check_binance_api()
            }

            # 计算总体状态
            overall_status = self._calculate_overall_status(checks)

            return {
                'timestamp': datetime.now().isoformat(),
                'overall_status': overall_status.value,
                'checks': checks
            }

        except Exception as e:
            self.log_error(f"快速健康检查失败: {e}")
            return {
                'timestamp': datetime.now().isoformat(),
                'overall_status': HealthStatus.UNKNOWN.value,
                'error': str(e)
            }

    # ==================== 健康报告 ====================

    def get_health_report(self) -> str:
        """
        获取健康报告（文本格式）

        Returns:
            健康报告文本
        """
        report = self.check_health()

        status_emoji = {
            'healthy': '✅',
            'degraded': '⚠️',
            'unhealthy': '❌',
            'unknown': '❓'
        }

        lines = [
            "🏥 系统健康检查报告",
            f"检查时间: {report['timestamp']}",
            f"总体状态: {status_emoji.get(report['overall_status'], '❓')} {report['overall_status'].upper()}",
            f"检查耗时: {report['check_duration_ms']}ms",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            ""
        ]

        # 系统状态
        system = report['checks'].get('system', {})
        if system:
            lines.append("📊 系统状态:")
            cpu = system.get('cpu', {})
            memory = system.get('memory', {})
            disk = system.get('disk', {})

            lines.append(f"  CPU: {status_emoji.get(cpu.get('status'), '❓')} {cpu.get('percent', 0):.1f}%")
            lines.append(f"  内存: {status_emoji.get(memory.get('status'), '❓')} {memory.get('percent', 0):.1f}%")
            lines.append(f"  磁盘: {status_emoji.get(disk.get('status'), '❓')} {disk.get('percent', 0):.1f}%")
            lines.append("")

        # 数据库状态
        database = report['checks'].get('database', {})
        if database:
            lines.append("💾 数据库:")
            lines.append(f"  {status_emoji.get(database.get('status'), '❓')} {database.get('message', '未知')}")
            lines.append("")

        # 服务依赖
        services = ['notification_service', 'kline_service', 'binance_api']
        lines.append("🔌 服务依赖:")

        for service_name in services:
            service = report['checks'].get(service_name, {})
            status = service.get('status', 'unknown')
            emoji = status_emoji.get(status, '❓')

            if status == 'healthy':
                response_time = service.get('response_time_ms', 0)
                lines.append(f"  {emoji} {service_name}: {response_time}ms")
            else:
                message = service.get('message', '未知错误')
                lines.append(f"  {emoji} {service_name}: {message}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)

    def get_cached_health(self) -> Optional[Dict[str, Any]]:
        """
        获取缓存的健康检查结果

        Returns:
            缓存的健康检查结果
        """
        return self._health_cache if self._health_cache else None

    def get_last_check_time(self) -> Optional[datetime]:
        """
        获取上次检查时间

        Returns:
            上次检查时间
        """
        return self._last_check_time

    def is_healthy(self) -> bool:
        """
        检查系统是否健康

        Returns:
            是否健康
        """
        report = self.quick_health_check()
        return report.get('overall_status') == HealthStatus.HEALTHY.value
