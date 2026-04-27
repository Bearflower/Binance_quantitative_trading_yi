# 监控告警使用示例

本文档详细介绍如何使用监控告警系统进行指标收集、告警管理和健康检查。

## 目录

1. [指标收集](#指标收集)
2. [告警管理](#告警管理)
3. [健康检查](#健康检查)
4. [集成到现有系统](#集成到现有系统)
5. [最佳实践](#最佳实践)

---

## 指标收集

### 示例 1：基本指标收集

```python
from monitoring.metrics_collector import MetricsCollector

# 创建指标收集器
collector = MetricsCollector()

# 记录交易指标
collector.record_trade(
    symbol='BTCUSDT',
    direction='LONG',
    pnl=10.5,
    duration_minutes=120
)

# 记录 API 调用指标
collector.record_api_call(
    endpoint='/api/v3/order',
    method='POST',
    status_code=200,
    duration_ms=150
)

# 记录错误指标
collector.record_error(
    error_type='BinanceAPIError',
    error_message='Rate limit exceeded',
    context={'symbol': 'BTCUSDT'}
)

# 获取指标摘要
summary = collector.get_metrics_summary()
print(f"指标摘要: {summary}")
```

### 示例 2：自定义指标

```python
from monitoring.metrics_collector import MetricsCollector

collector = MetricsCollector()

# 定义自定义指标
collector.define_metric(
    name='position_size',
    metric_type='gauge',
    description='当前持仓大小',
    labels=['symbol', 'direction']
)

# 记录自定义指标值
collector.set_metric(
    name='position_size',
    value=0.01,
    labels={'symbol': 'BTCUSDT', 'direction': 'LONG'}
)

# 增加指标值
collector.increment_metric(
    name='total_trades',
    labels={'symbol': 'BTCUSDT'}
)

# 观察指标值（用于直方图）
collector.observe_metric(
    name='trade_duration',
    value=120,
    labels={'symbol': 'BTCUSDT'}
)
```

### 示例 3：指标聚合

```python
from monitoring.metrics_collector import MetricsCollector
from datetime import datetime, timedelta

collector = MetricsCollector()

# 记录多个交易
for i in range(10):
    collector.record_trade(
        symbol='BTCUSDT',
        direction='LONG' if i % 2 == 0 else 'SHORT',
        pnl=5 * (i + 1),
        duration_minutes=30 * (i + 1)
    )

# 获取时间范围内的指标
metrics = collector.get_metrics_by_time_range(
    start_time=datetime.now() - timedelta(hours=1),
    end_time=datetime.now()
)

print(f"过去 1 小时指标:")
print(f"  总交易数: {metrics['total_trades']}")
print(f"  总盈亏: {metrics['total_pnl']}")
print(f"  平均时长: {metrics['avg_duration']} 分钟")
```

---

## 告警管理

### 示例 4：定义告警规则

```python
from monitoring.alert_manager import AlertManager, AlertRule, AlertLevel

# 创建告警管理器
alert_manager = AlertManager()

# 定义告警规则
rules = [
    AlertRule(
        name='high_loss_alert',
        condition='daily_pnl < -30',
        level=AlertLevel.CRITICAL,
        message='当日亏损超过 30U',
        cooldown_minutes=60
    ),
    AlertRule(
        name='consecutive_losses_alert',
        condition='consecutive_losses >= 3',
        level=AlertLevel.WARNING,
        message='连续亏损 3 笔',
        cooldown_minutes=30
    ),
    AlertRule(
        name='api_error_rate_alert',
        condition='api_error_rate > 0.1',
        level=AlertLevel.WARNING,
        message='API 错误率超过 10%',
        cooldown_minutes=10
    )
]

# 添加告警规则
for rule in rules:
    alert_manager.add_rule(rule)

print(f"已添加 {len(rules)} 条告警规则")
```

### 示例 5：触发告警

```python
from monitoring.alert_manager import AlertManager, AlertLevel

alert_manager = AlertManager()

# 手动触发告警
alert_manager.trigger_alert(
    name='manual_alert',
    level=AlertLevel.WARNING,
    message='市场波动较大，注意风险',
    context={
        'symbol': 'BTCUSDT',
        'change_percent': 6.5,
        'threshold': 5.0
    }
)

# 基于条件触发告警
alert_manager.check_and_alert(
    metric_name='daily_pnl',
    value=-35,
    context={'date': '2026-04-27'}
)
```

### 示例 6：告警通知

```python
from monitoring.alert_manager import AlertManager, AlertLevel
from utils.lark_notifier import LarkNotifier
import os

# 创建飞书通知器
notifier = LarkNotifier(os.getenv('LARK_WEBHOOK_URL'))

# 创建告警管理器（集成通知）
alert_manager = AlertManager(notifier=notifier)

# 触发告警（自动发送通知）
alert_manager.trigger_alert(
    name='system_error',
    level=AlertLevel.CRITICAL,
    message='数据库连接失败',
    context={
        'error': 'Connection refused',
        'host': 'postgres-db',
        'port': 5432
    }
)

print("告警已触发并发送飞书通知")
```

### 示例 7：告警历史

```python
from monitoring.alert_manager import AlertManager
from datetime import datetime, timedelta

alert_manager = AlertManager()

# 获取告警历史
history = alert_manager.get_alert_history(
    start_time=datetime.now() - timedelta(days=1),
    end_time=datetime.now()
)

print(f"过去 24 小时告警历史:")
for alert in history:
    print(f"  [{alert['timestamp']}] {alert['level']}: {alert['message']}")

# 获取告警统计
stats = alert_manager.get_alert_statistics()
print(f"\n告警统计:")
print(f"  总告警数: {stats['total_alerts']}")
print(f"  按级别: {stats['by_level']}")
```

---

## 健康检查

### 示例 8：基本健康检查

```python
from monitoring.health_checker import HealthChecker

# 创建健康检查器
health_checker = HealthChecker()

# 检查系统健康状态
health_status = health_checker.check_health()

print(f"系统健康状态: {health_status['status']}")
print(f"检查项:")
for check_name, result in health_status['checks'].items():
    status = '✅' if result['healthy'] else '❌'
    print(f"  {status} {check_name}: {result['message']}")
```

### 示例 9：自定义健康检查

```python
from monitoring.health_checker import HealthChecker, HealthCheckResult

# 创建健康检查器
health_checker = HealthChecker()

# 定义自定义健康检查
def check_database_connection() -> HealthCheckResult:
    """检查数据库连接"""
    try:
        # 模拟数据库连接检查
        # 实际应该执行简单的查询
        return HealthCheckResult(
            healthy=True,
            message='数据库连接正常'
        )
    except Exception as e:
        return HealthCheckResult(
            healthy=False,
            message=f'数据库连接失败: {e}'
        )

def check_api_connectivity() -> HealthCheckResult:
    """检查 API 连接"""
    try:
        # 模拟 API 连接检查
        return HealthCheckResult(
            healthy=True,
            message='API 连接正常'
        )
    except Exception as e:
        return HealthCheckResult(
            healthy=False,
            message=f'API 连接失败: {e}'
        )

# 注册健康检查
health_checker.register_check('database', check_database_connection)
health_checker.register_check('api', check_api_connectivity)

# 执行健康检查
status = health_checker.check_health()
print(f"健康状态: {status['status']}")
```

### 示例 10：定期健康检查

```python
from monitoring.health_checker import HealthChecker
import time
import threading

# 创建健康检查器
health_checker = HealthChecker()

def periodic_health_check(interval_seconds: int = 60):
    """定期健康检查"""
    while True:
        status = health_checker.check_health()
        
        if status['status'] != 'healthy':
            print(f"⚠️ 系统不健康: {status['status']}")
            # 发送告警通知
            # alert_manager.trigger_alert(...)
        else:
            print(f"✅ 系统健康")
        
        time.sleep(interval_seconds)

# 启动定期健康检查（后台线程）
health_thread = threading.Thread(
    target=periodic_health_check,
    args=(60,),
    daemon=True
)
health_thread.start()

print("健康检查线程已启动")
```

---

## 集成到现有系统

### 示例 11：集成到服务

```python
from services.base import BaseService
from monitoring.metrics_collector import MetricsCollector
from monitoring.alert_manager import AlertManager
from monitoring.health_checker import HealthChecker

class MonitoredService(BaseService):
    """带监控的服务"""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # 初始化监控组件
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager()
        self.health_checker = HealthChecker()
        
        # 注册健康检查
        self.health_checker.register_check(
            'service_status',
            self._check_service_health
        )
    
    def _initialize(self):
        """初始化服务"""
        self.log_info("服务初始化完成")
    
    def _check_service_health(self):
        """检查服务健康状态"""
        from monitoring.health_checker import HealthCheckResult
        
        if self.is_running():
            return HealthCheckResult(healthy=True, message='服务运行正常')
        else:
            return HealthCheckResult(healthy=False, message='服务未运行')
    
    def execute_trade(self, signal: dict):
        """执行交易（带监控）"""
        import time
        
        start_time = time.time()
        
        try:
            # 执行交易
            result = self._do_execute_trade(signal)
            
            # 记录成功指标
            self.metrics_collector.record_trade(
                symbol=signal['symbol'],
                direction=signal['direction'],
                pnl=result.get('pnl', 0),
                duration_minutes=(time.time() - start_time) / 60
            )
            
            return result
            
        except Exception as e:
            # 记录错误指标
            self.metrics_collector.record_error(
                error_type=type(e).__name__,
                error_message=str(e),
                context={'signal': signal}
            )
            
            # 触发告警
            self.alert_manager.trigger_alert(
                name='trade_execution_error',
                level='ERROR',
                message=f'交易执行失败: {e}',
                context={'signal': signal}
            )
            
            raise
    
    def _do_execute_trade(self, signal: dict):
        """实际执行交易"""
        # 模拟交易执行
        return {'status': 'FILLED', 'pnl': 10.5}

# 使用示例
service = MonitoredService(service_name='MonitoredService')

# 执行交易（自动监控）
result = service.execute_trade({
    'symbol': 'BTCUSDT',
    'direction': 'LONG',
    'price': 50000
})

print(f"交易结果: {result}")

# 检查健康状态
health = service.health_checker.check_health()
print(f"健康状态: {health['status']}")
```

### 示例 12：集成到调度器

```python
from monitoring.metrics_collector import MetricsCollector
from monitoring.alert_manager import AlertManager
from monitoring.health_checker import HealthChecker
import schedule
import time

class MonitoredScheduler:
    """带监控的调度器"""
    
    def __init__(self):
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager()
        self.health_checker = HealthChecker()
    
    def scheduled_task(self):
        """定时任务"""
        print("执行定时任务...")
        
        try:
            # 执行任务逻辑
            # ...
            
            # 记录成功
            self.metrics_collector.increment_metric(
                name='scheduled_task_success',
                labels={'task': 'daily_report'}
            )
            
        except Exception as e:
            # 记录失败
            self.metrics_collector.increment_metric(
                name='scheduled_task_failure',
                labels={'task': 'daily_report'}
            )
            
            # 触发告警
            self.alert_manager.trigger_alert(
                name='scheduled_task_error',
                level='ERROR',
                message=f'定时任务执行失败: {e}'
            )
    
    def health_check_task(self):
        """健康检查任务"""
        status = self.health_checker.check_health()
        
        if status['status'] != 'healthy':
            self.alert_manager.trigger_alert(
                name='health_check_failed',
                level='WARNING',
                message='系统健康检查失败'
            )
    
    def start(self):
        """启动调度器"""
        # 定时任务
        schedule.every(1).hours.do(self.scheduled_task)
        
        # 健康检查
        schedule.every(5).minutes.do(self.health_check_task)
        
        # 运行调度器
        while True:
            schedule.run_pending()
            time.sleep(1)

# 使用示例
scheduler = MonitoredScheduler()
# scheduler.start()  # 取消注释以启动
```

---

## 最佳实践

### 示例 13：完整的监控系统

```python
from monitoring.metrics_collector import MetricsCollector
from monitoring.alert_manager import AlertManager, AlertRule, AlertLevel
from monitoring.health_checker import HealthChecker
from utils.lark_notifier import LarkNotifier
import logging

logger = logging.getLogger(__name__)

class MonitoringSystem:
    """完整的监控系统"""
    
    def __init__(self, enable_notification: bool = True):
        """
        初始化监控系统
        
        Args:
            enable_notification: 是否启用通知
        """
        # 初始化组件
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager(
            notifier=LarkNotifier() if enable_notification else None
        )
        self.health_checker = HealthChecker()
        
        # 设置默认告警规则
        self._setup_default_alerts()
        
        # 注册健康检查
        self._register_health_checks()
        
        logger.info("监控系统初始化完成")
    
    def _setup_default_alerts(self):
        """设置默认告警规则"""
        default_rules = [
            AlertRule(
                name='high_daily_loss',
                condition='daily_pnl < -30',
                level=AlertLevel.CRITICAL,
                message='当日亏损超限',
                cooldown_minutes=60
            ),
            AlertRule(
                name='consecutive_losses',
                condition='consecutive_losses >= 3',
                level=AlertLevel.WARNING,
                message='连续亏损',
                cooldown_minutes=30
            ),
            AlertRule(
                name='api_error_rate',
                condition='api_error_rate > 0.1',
                level=AlertLevel.WARNING,
                message='API 错误率过高',
                cooldown_minutes=10
            )
        ]
        
        for rule in default_rules:
            self.alert_manager.add_rule(rule)
    
    def _register_health_checks(self):
        """注册健康检查"""
        self.health_checker.register_check(
            'metrics_collector',
            self._check_metrics_collector
        )
        self.health_checker.register_check(
            'alert_manager',
            self._check_alert_manager
        )
    
    def _check_metrics_collector(self):
        """检查指标收集器"""
        from monitoring.health_checker import HealthCheckResult
        
        try:
            # 检查指标收集器是否正常工作
            self.metrics_collector.increment_metric('health_check')
            return HealthCheckResult(healthy=True, message='指标收集器正常')
        except Exception as e:
            return HealthCheckResult(healthy=False, message=f'指标收集器异常: {e}')
    
    def _check_alert_manager(self):
        """检查告警管理器"""
        from monitoring.health_checker import HealthCheckResult
        
        try:
            # 检查告警管理器是否正常工作
            rules_count = len(self.alert_manager.rules)
            return HealthCheckResult(
                healthy=True,
                message=f'告警管理器正常，已加载 {rules_count} 条规则'
            )
        except Exception as e:
            return HealthCheckResult(healthy=False, message=f'告警管理器异常: {e}')
    
    def record_trade(self, symbol: str, direction: str, pnl: float, **kwargs):
        """记录交易指标"""
        self.metrics_collector.record_trade(
            symbol=symbol,
            direction=direction,
            pnl=pnl,
            **kwargs
        )
        
        # 检查是否触发告警
        self._check_trade_alerts(symbol, pnl)
    
    def _check_trade_alerts(self, symbol: str, pnl: float):
        """检查交易告警"""
        # 获取当日盈亏
        daily_pnl = self.metrics_collector.get_daily_pnl()
        
        # 检查告警条件
        self.alert_manager.check_and_alert(
            metric_name='daily_pnl',
            value=daily_pnl,
            context={'symbol': symbol, 'last_pnl': pnl}
        )
    
    def get_system_status(self):
        """获取系统状态"""
        health = self.health_checker.check_health()
        metrics = self.metrics_collector.get_metrics_summary()
        
        return {
            'health': health,
            'metrics': metrics,
            'timestamp': time.time()
        }

# 使用示例
monitoring = MonitoringSystem(enable_notification=True)

# 记录交易
monitoring.record_trade('BTCUSDT', 'LONG', 10.5)

# 获取系统状态
status = monitoring.get_system_status()
print(f"系统状态: {status['health']['status']}")
```

---

## 注意事项

1. **指标命名**：使用清晰、一致的指标命名规范
2. **告警级别**：合理设置告警级别，避免过度告警
3. **冷却时间**：为告警设置冷却时间，避免重复通知
4. **健康检查频率**：合理设置健康检查频率，避免过度消耗资源
5. **通知渠道**：根据告警级别选择合适的通知渠道
6. **监控数据保留**：定期清理历史监控数据

---

## 相关文档

- [异常处理使用示例](./exception_handling_examples.md)
- [服务基类使用示例](./service_base_examples.md)
- [数据仓库使用示例](./repository_examples.md)
- [监控告警系统使用指南](../design/监控告警系统使用指南.md)
