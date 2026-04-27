#!/usr/bin/env python3
"""
指标收集器

负责收集系统、应用和业务指标。

功能：
1. 系统指标收集（CPU、内存、磁盘、网络）
2. 应用指标收集（API调用、响应时间、错误率）
3. 业务指标收集（交易成功率、盈亏、持仓）
4. 指标缓存和聚合
5. 指标历史记录

版本: v1.0.0
创建时间: 2026-04-27
"""

import os
import time
import psutil
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from collections import deque
from decimal import Decimal
import threading

from services.base import BaseService
from config.config_manager import get_config


class MetricsCollector(BaseService):
    """
    指标收集器

    收集并管理各类监控指标。
    """

    def __init__(
        self,
        collection_interval: int = 60,
        history_size: int = 1000
    ):
        """
        初始化指标收集器

        Args:
            collection_interval: 收集间隔（秒）
            history_size: 历史记录大小
        """
        super().__init__(
            service_name="MetricsCollector",
            auto_initialize=False
        )

        self.collection_interval = collection_interval
        self.history_size = history_size

        # 指标历史记录（使用双端队列）
        self._system_metrics_history: deque = deque(maxlen=history_size)
        self._app_metrics_history: deque = deque(maxlen=history_size)
        self._business_metrics_history: deque = deque(maxlen=history_size)

        # 实时指标缓存
        self._current_metrics: Dict[str, Any] = {}

        # 统计数据
        self._api_stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_response_time': 0.0,
            'response_times': deque(maxlen=100)
        }

        self._trade_stats = {
            'total_trades': 0,
            'successful_trades': 0,
            'failed_trades': 0,
            'total_pnl': Decimal('0'),
            'winning_trades': 0,
            'losing_trades': 0
        }

        # 线程锁
        self._lock = threading.Lock()

        # 初始化
        self.initialize()

    def _initialize(self):
        """初始化指标收集器"""
        self.log_info("初始化指标收集器")

        # 加载配置
        self._load_config()

        # 收集初始指标
        self.collect_all_metrics()

        self.log_info("指标收集器初始化完成")

    def _load_config(self):
        """加载配置"""
        # 监控配置
        self.monitoring_config = get_config('monitoring', {})

        # 系统告警阈值
        self.cpu_threshold = get_config('monitoring.alert_thresholds.cpu_usage', 80)
        self.memory_threshold = get_config('monitoring.alert_thresholds.memory_usage', 80)
        self.disk_threshold = get_config('monitoring.alert_thresholds.disk_usage', 90)

        # 应用告警阈值
        self.api_success_rate_threshold = get_config('monitoring.alert_thresholds.api_success_rate', 95)
        self.response_time_threshold = get_config('monitoring.alert_thresholds.response_time', 5.0)
        self.error_rate_threshold = get_config('monitoring.alert_thresholds.error_rate', 5)

        # 业务告警阈值
        self.trade_success_rate_threshold = get_config('monitoring.alert_thresholds.trade_success_rate', 90)
        self.consecutive_losses_threshold = get_config('monitoring.alert_thresholds.consecutive_losses', 5)
        self.capital_usage_threshold = get_config('monitoring.alert_thresholds.capital_usage', 80)

    # ==================== 系统指标收集 ====================

    def collect_system_metrics(self) -> Dict[str, Any]:
        """
        收集系统指标

        Returns:
            系统指标字典
        """
        try:
            # CPU 使用率
            cpu_percent = psutil.cpu_percent(interval=1)

            # 内存使用率
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
            memory_used = memory.used / (1024 ** 3)  # GB
            memory_total = memory.total / (1024 ** 3)  # GB

            # 磁盘使用率
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent
            disk_used = disk.used / (1024 ** 3)  # GB
            disk_total = disk.total / (1024 ** 3)  # GB

            # 网络连接数（可能在某些系统上失败）
            try:
                net_connections = len(psutil.net_connections())
            except (psutil.AccessDenied, psutil.Error):
                net_connections = 0

            # 系统负载（可能在某些系统上不可用）
            try:
                load_avg = os.getloadavg()
            except (OSError, AttributeError):
                load_avg = (0.0, 0.0, 0.0)

            metrics = {
                'timestamp': datetime.now().isoformat(),
                'cpu': {
                    'percent': cpu_percent,
                    'count': psutil.cpu_count(),
                    'load_avg_1m': load_avg[0],
                    'load_avg_5m': load_avg[1],
                    'load_avg_15m': load_avg[2]
                },
                'memory': {
                    'percent': memory_percent,
                    'used_gb': round(memory_used, 2),
                    'total_gb': round(memory_total, 2),
                    'available_gb': round(memory.available / (1024 ** 3), 2)
                },
                'disk': {
                    'percent': disk_percent,
                    'used_gb': round(disk_used, 2),
                    'total_gb': round(disk_total, 2),
                    'free_gb': round(disk.free / (1024 ** 3), 2)
                },
                'network': {
                    'connections': net_connections
                },
                'alerts': self._check_system_alerts(
                    cpu_percent,
                    memory_percent,
                    disk_percent,
                    net_connections
                )
            }

            # 添加到历史记录
            with self._lock:
                self._system_metrics_history.append(metrics)

            return metrics

        except Exception as e:
            self.log_error(f"收集系统指标失败: {e}")
            return {}

    def _check_system_alerts(
        self,
        cpu_percent: float,
        memory_percent: float,
        disk_percent: float,
        net_connections: int
    ) -> List[Dict[str, Any]]:
        """
        检查系统告警

        Args:
            cpu_percent: CPU使用率
            memory_percent: 内存使用率
            disk_percent: 磁盘使用率
            net_connections: 网络连接数

        Returns:
            告警列表
        """
        alerts = []

        if cpu_percent > self.cpu_threshold:
            alerts.append({
                'type': 'system',
                'level': 'warning',
                'metric': 'cpu_usage',
                'value': cpu_percent,
                'threshold': self.cpu_threshold,
                'message': f'CPU使用率过高: {cpu_percent:.1f}% > {self.cpu_threshold}%'
            })

        if memory_percent > self.memory_threshold:
            alerts.append({
                'type': 'system',
                'level': 'warning',
                'metric': 'memory_usage',
                'value': memory_percent,
                'threshold': self.memory_threshold,
                'message': f'内存使用率过高: {memory_percent:.1f}% > {self.memory_threshold}%'
            })

        if disk_percent > self.disk_threshold:
            alerts.append({
                'type': 'system',
                'level': 'critical',
                'metric': 'disk_usage',
                'value': disk_percent,
                'threshold': self.disk_threshold,
                'message': f'磁盘使用率过高: {disk_percent:.1f}% > {self.disk_threshold}%'
            })

        return alerts

    # ==================== 应用指标收集 ====================

    def record_api_request(
        self,
        endpoint: str,
        success: bool,
        response_time: float,
        error: Optional[str] = None
    ):
        """
        记录API请求

        Args:
            endpoint: API端点
            success: 是否成功
            response_time: 响应时间（秒）
            error: 错误信息（可选）
        """
        with self._lock:
            self._api_stats['total_requests'] += 1
            self._api_stats['total_response_time'] += response_time
            self._api_stats['response_times'].append(response_time)

            if success:
                self._api_stats['successful_requests'] += 1
            else:
                self._api_stats['failed_requests'] += 1

    def collect_app_metrics(self) -> Dict[str, Any]:
        """
        收集应用指标

        Returns:
            应用指标字典
        """
        try:
            with self._lock:
                total = self._api_stats['total_requests']
                successful = self._api_stats['successful_requests']
                failed = self._api_stats['failed_requests']

                # 计算成功率
                success_rate = (successful / total * 100) if total > 0 else 100.0

                # 计算错误率
                error_rate = (failed / total * 100) if total > 0 else 0.0

                # 计算平均响应时间
                avg_response_time = (
                    self._api_stats['total_response_time'] / total
                    if total > 0 else 0.0
                )

                # 计算P95响应时间
                response_times = list(self._api_stats['response_times'])
                p95_response_time = (
                    sorted(response_times)[int(len(response_times) * 0.95)]
                    if response_times else 0.0
                )

            metrics = {
                'timestamp': datetime.now().isoformat(),
                'api': {
                    'total_requests': total,
                    'successful_requests': successful,
                    'failed_requests': failed,
                    'success_rate': round(success_rate, 2),
                    'error_rate': round(error_rate, 2),
                    'avg_response_time': round(avg_response_time, 3),
                    'p95_response_time': round(p95_response_time, 3)
                },
                'alerts': self._check_app_alerts(
                    success_rate,
                    avg_response_time,
                    error_rate
                )
            }

            # 添加到历史记录
            with self._lock:
                self._app_metrics_history.append(metrics)

            return metrics

        except Exception as e:
            self.log_error(f"收集应用指标失败: {e}")
            return {}

    def _check_app_alerts(
        self,
        success_rate: float,
        avg_response_time: float,
        error_rate: float
    ) -> List[Dict[str, Any]]:
        """
        检查应用告警

        Args:
            success_rate: API成功率
            avg_response_time: 平均响应时间
            error_rate: 错误率

        Returns:
            告警列表
        """
        alerts = []

        if success_rate < self.api_success_rate_threshold:
            alerts.append({
                'type': 'application',
                'level': 'warning',
                'metric': 'api_success_rate',
                'value': success_rate,
                'threshold': self.api_success_rate_threshold,
                'message': f'API成功率过低: {success_rate:.2f}% < {self.api_success_rate_threshold}%'
            })

        if avg_response_time > self.response_time_threshold:
            alerts.append({
                'type': 'application',
                'level': 'warning',
                'metric': 'response_time',
                'value': avg_response_time,
                'threshold': self.response_time_threshold,
                'message': f'平均响应时间过长: {avg_response_time:.3f}s > {self.response_time_threshold}s'
            })

        if error_rate > self.error_rate_threshold:
            alerts.append({
                'type': 'application',
                'level': 'critical',
                'metric': 'error_rate',
                'value': error_rate,
                'threshold': self.error_rate_threshold,
                'message': f'错误率过高: {error_rate:.2f}% > {self.error_rate_threshold}%'
            })

        return alerts

    # ==================== 业务指标收集 ====================

    def record_trade(
        self,
        symbol: str,
        success: bool,
        pnl: Optional[Decimal] = None
    ):
        """
        记录交易

        Args:
            symbol: 交易对
            success: 是否成功
            pnl: 盈亏金额（可选）
        """
        with self._lock:
            self._trade_stats['total_trades'] += 1

            if success:
                self._trade_stats['successful_trades'] += 1
            else:
                self._trade_stats['failed_trades'] += 1

            if pnl is not None:
                self._trade_stats['total_pnl'] += pnl
                if pnl > 0:
                    self._trade_stats['winning_trades'] += 1
                elif pnl < 0:
                    self._trade_stats['losing_trades'] += 1

    def collect_business_metrics(
        self,
        account_info: Optional[Dict[str, Any]] = None,
        positions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        收集业务指标

        Args:
            account_info: 账户信息（可选）
            positions: 持仓信息（可选）

        Returns:
            业务指标字典
        """
        try:
            with self._lock:
                total = self._trade_stats['total_trades']
                successful = self._trade_stats['successful_trades']
                failed = self._trade_stats['failed_trades']
                total_pnl = self._trade_stats['total_pnl']
                winning = self._trade_stats['winning_trades']
                losing = self._trade_stats['losing_trades']

                # 计算交易成功率
                trade_success_rate = (successful / total * 100) if total > 0 else 100.0

                # 计算胜率
                win_rate = (winning / (winning + losing) * 100) if (winning + losing) > 0 else 0.0

            # 账户和持仓信息
            account_metrics = {}
            position_metrics = {}

            if account_info:
                total_capital = Decimal(str(account_info.get('totalCapital', '0')))
                available_capital = Decimal(str(account_info.get('availableCapital', '0')))
                used_margin = Decimal(str(account_info.get('usedMargin', '0')))

                capital_usage = float(used_margin / total_capital * 100) if total_capital > 0 else 0.0

                account_metrics = {
                    'total_capital': float(total_capital),
                    'available_capital': float(available_capital),
                    'used_margin': float(used_margin),
                    'capital_usage': round(capital_usage, 2),
                    'total_pnl': float(total_pnl)
                }

            if positions:
                position_count = len(positions)
                total_position_value = sum(
                    float(p.get('positionValue', 0)) for p in positions
                )

                # 检查持仓时间
                long_positions = []
                for pos in positions:
                    open_time = pos.get('openTime')
                    if open_time:
                        open_dt = datetime.fromtimestamp(open_time / 1000)
                        duration = (datetime.now() - open_dt).total_seconds() / 3600  # 小时
                        if duration > 72:  # 超过72小时
                            long_positions.append({
                                'symbol': pos.get('symbol'),
                                'duration_hours': round(duration, 2)
                            })

                position_metrics = {
                    'position_count': position_count,
                    'total_position_value': round(total_position_value, 2),
                    'long_positions': long_positions
                }

            metrics = {
                'timestamp': datetime.now().isoformat(),
                'trading': {
                    'total_trades': total,
                    'successful_trades': successful,
                    'failed_trades': failed,
                    'trade_success_rate': round(trade_success_rate, 2),
                    'win_rate': round(win_rate, 2),
                    'total_pnl': float(total_pnl)
                },
                'account': account_metrics,
                'positions': position_metrics,
                'alerts': self._check_business_alerts(
                    trade_success_rate,
                    account_metrics,
                    position_metrics
                )
            }

            # 添加到历史记录
            with self._lock:
                self._business_metrics_history.append(metrics)

            return metrics

        except Exception as e:
            self.log_error(f"收集业务指标失败: {e}")
            return {}

    def _check_business_alerts(
        self,
        trade_success_rate: float,
        account_metrics: Dict[str, Any],
        position_metrics: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        检查业务告警

        Args:
            trade_success_rate: 交易成功率
            account_metrics: 账户指标
            position_metrics: 持仓指标

        Returns:
            告警列表
        """
        alerts = []

        # 交易成功率告警
        if trade_success_rate < self.trade_success_rate_threshold:
            alerts.append({
                'type': 'business',
                'level': 'warning',
                'metric': 'trade_success_rate',
                'value': trade_success_rate,
                'threshold': self.trade_success_rate_threshold,
                'message': f'交易成功率过低: {trade_success_rate:.2f}% < {self.trade_success_rate_threshold}%'
            })

        # 资金使用率告警
        if account_metrics:
            capital_usage = account_metrics.get('capital_usage', 0)
            if capital_usage > self.capital_usage_threshold:
                alerts.append({
                    'type': 'business',
                    'level': 'warning',
                    'metric': 'capital_usage',
                    'value': capital_usage,
                    'threshold': self.capital_usage_threshold,
                    'message': f'资金使用率过高: {capital_usage:.2f}% > {self.capital_usage_threshold}%'
                })

        # 持仓时间过长告警
        if position_metrics:
            long_positions = position_metrics.get('long_positions', [])
            for pos in long_positions:
                alerts.append({
                    'type': 'business',
                    'level': 'warning',
                    'metric': 'position_duration',
                    'symbol': pos['symbol'],
                    'value': pos['duration_hours'],
                    'threshold': 72,
                    'message': f"持仓时间过长: {pos['symbol']} 已持仓 {pos['duration_hours']:.2f} 小时"
                })

        return alerts

    # ==================== 综合指标收集 ====================

    def collect_all_metrics(
        self,
        account_info: Optional[Dict[str, Any]] = None,
        positions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        收集所有指标

        Args:
            account_info: 账户信息（可选）
            positions: 持仓信息（可选）

        Returns:
            所有指标字典
        """
        system_metrics = self.collect_system_metrics()
        app_metrics = self.collect_app_metrics()
        business_metrics = self.collect_business_metrics(account_info, positions)

        # 合并所有告警
        all_alerts = (
            system_metrics.get('alerts', []) +
            app_metrics.get('alerts', []) +
            business_metrics.get('alerts', [])
        )

        metrics = {
            'timestamp': datetime.now().isoformat(),
            'system': system_metrics,
            'application': app_metrics,
            'business': business_metrics,
            'alerts': all_alerts
        }

        # 更新缓存
        with self._lock:
            self._current_metrics = metrics

        return metrics

    # ==================== 指标查询 ====================

    def get_current_metrics(self) -> Dict[str, Any]:
        """
        获取当前指标

        Returns:
            当前指标字典
        """
        with self._lock:
            return self._current_metrics.copy()

    def get_system_metrics_history(
        self,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取系统指标历史

        Args:
            limit: 返回记录数

        Returns:
            系统指标历史列表
        """
        with self._lock:
            return list(self._system_metrics_history)[-limit:]

    def get_app_metrics_history(
        self,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取应用指标历史

        Args:
            limit: 返回记录数

        Returns:
            应用指标历史列表
        """
        with self._lock:
            return list(self._app_metrics_history)[-limit:]

    def get_business_metrics_history(
        self,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取业务指标历史

        Args:
            limit: 返回记录数

        Returns:
            业务指标历史列表
        """
        with self._lock:
            return list(self._business_metrics_history)[-limit:]

    def get_metrics_summary(self) -> Dict[str, Any]:
        """
        获取指标摘要

        Returns:
            指标摘要字典
        """
        current = self.get_current_metrics()

        if not current:
            return {}

        return {
            'timestamp': current.get('timestamp'),
            'system': {
                'cpu_percent': current.get('system', {}).get('cpu', {}).get('percent'),
                'memory_percent': current.get('system', {}).get('memory', {}).get('percent'),
                'disk_percent': current.get('system', {}).get('disk', {}).get('percent')
            },
            'application': {
                'api_success_rate': current.get('application', {}).get('api', {}).get('success_rate'),
                'avg_response_time': current.get('application', {}).get('api', {}).get('avg_response_time'),
                'error_rate': current.get('application', {}).get('api', {}).get('error_rate')
            },
            'business': {
                'trade_success_rate': current.get('business', {}).get('trading', {}).get('trade_success_rate'),
                'win_rate': current.get('business', {}).get('trading', {}).get('win_rate'),
                'total_pnl': current.get('business', {}).get('trading', {}).get('total_pnl')
            },
            'alert_count': len(current.get('alerts', []))
        }

    # ==================== 统计重置 ====================

    def reset_stats(self):
        """重置统计数据"""
        with self._lock:
            self._api_stats = {
                'total_requests': 0,
                'successful_requests': 0,
                'failed_requests': 0,
                'total_response_time': 0.0,
                'response_times': deque(maxlen=100)
            }

            self._trade_stats = {
                'total_trades': 0,
                'successful_trades': 0,
                'failed_trades': 0,
                'total_pnl': Decimal('0'),
                'winning_trades': 0,
                'losing_trades': 0
            }

        self.log_info("统计数据已重置")
