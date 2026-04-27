# 异常处理使用示例

本文档详细介绍如何使用自定义异常、错误处理器和错误装饰器进行统一的异常处理。

## 目录

1. [自定义异常使用](#自定义异常使用)
2. [错误处理器使用](#错误处理器使用)
3. [错误装饰器使用](#错误装饰器使用)
4. [飞书通知集成](#飞书通知集成)
5. [错误统计和历史记录](#错误统计和历史记录)

---

## 自定义异常使用

### 示例 1：基本异常类型

```python
from utils.exceptions import (
    TradingSystemError,
    ConfigurationError,
    BinanceAPIError,
    InsufficientFundsError,
    RiskLimitExceededError
)

# 配置错误
try:
    raise ConfigurationError("缺少必需的配置项: api_key")
except ConfigurationError as e:
    print(f"配置错误: {e}")

# API 错误
try:
    raise BinanceAPIError("API 调用失败", status_code=429, msg="Rate limit exceeded")
except BinanceAPIError as e:
    print(f"API 错误: {e}, 状态码: {e.status_code}")

# 资金不足错误
try:
    raise InsufficientFundsError("账户余额不足", required=100, available=50)
except InsufficientFundsError as e:
    print(f"资金不足: 需要 {e.required}U, 可用 {e.available}U")

# 风险限制错误
try:
    raise RiskLimitExceededError("超过最大持仓限制", current=3, max_allowed=2)
except RiskLimitExceededError as e:
    print(f"风险超限: 当前 {e.current}, 最大允许 {e.max_allowed}")
```

### 示例 2：自定义业务异常

```python
from utils.exceptions import TradingSystemError

class SignalValidationError(TradingSystemError):
    """信号验证错误"""
    
    def __init__(self, message: str, signal_data: dict = None):
        super().__init__(message)
        self.signal_data = signal_data or {}

class PositionCalculationError(TradingSystemError):
    """仓位计算错误"""
    
    def __init__(self, message: str, symbol: str = None, entry_price: float = None):
        super().__init__(message)
        self.symbol = symbol
        self.entry_price = entry_price

# 使用自定义异常
try:
    signal = {'symbol': 'BTCUSDT', 'direction': 'INVALID'}
    raise SignalValidationError("信号方向无效", signal_data=signal)
except SignalValidationError as e:
    print(f"信号验证失败: {e}")
    print(f"信号数据: {e.signal_data}")
```

---

## 错误处理器使用

### 示例 3：基本错误处理

```python
from utils.error_handler import ErrorHandler

# 创建错误处理器
error_handler = ErrorHandler()

# 处理异常
try:
    result = 1 / 0
except Exception as e:
    error_info = error_handler.handle_error(
        error=e,
        context={'operation': 'division', 'numerator': 1, 'denominator': 0}
    )
    print(f"错误已记录: {error_info['error_id']}")
```

### 示例 4：错误处理器配置

```python
from utils.error_handler import ErrorHandler

# 创建自定义配置的错误处理器
error_handler = ErrorHandler(
    enable_notification=True,  # 启用飞书通知
    enable_statistics=True,    # 启用错误统计
    max_history=1000          # 最大历史记录数
)

# 处理不同级别的错误
try:
    # 模拟一个警告级别的错误
    raise Warning("市场波动较大，注意风险")
except Warning as w:
    error_handler.handle_error(w, level='WARNING')

try:
    # 模拟一个严重错误
    raise RuntimeError("数据库连接失败")
except RuntimeError as e:
    error_handler.handle_error(e, level='CRITICAL')
```

### 示例 5：错误处理器集成到服务

```python
from services.base import BaseService
from utils.error_handler import ErrorHandler

class MyService(BaseService):
    """自定义服务示例"""
    
    def __init__(self):
        super().__init__(service_name="MyService")
        self.error_handler = ErrorHandler()
    
    def process_data(self, data: dict):
        """处理数据"""
        try:
            # 业务逻辑
            if not data.get('symbol'):
                raise ValueError("缺少交易对信息")
            
            # 处理数据...
            return {'status': 'success'}
            
        except Exception as e:
            # 使用错误处理器
            self.error_handler.handle_error(
                error=e,
                context={'data': data, 'operation': 'process_data'}
            )
            # 使用服务基类的错误处理
            self.handle_error(e, context={'data': data})
            return {'status': 'failed', 'error': str(e)}

# 使用示例
service = MyService()
result = service.process_data({'price': 50000})
print(result)
```

---

## 错误装饰器使用

### 示例 6：函数错误装饰器

```python
from utils.error_handler import error_handler_decorator

@error_handler_decorator
def divide_numbers(a: int, b: int) -> float:
    """除法运算（自动处理错误）"""
    return a / b

# 正常调用
result = divide_numbers(10, 2)
print(f"结果: {result}")  # 输出: 5.0

# 错误调用（自动捕获并记录）
result = divide_numbers(10, 0)
print(f"结果: {result}")  # 输出: None（错误已记录）
```

### 示例 7：带重试的错误装饰器

```python
from utils.error_handler import retry_on_error
from tenacity import stop_after_attempt, wait_exponential

@retry_on_error(
    max_attempts=3,
    wait_strategy=wait_exponential(multiplier=1, min=2, max=10),
    exceptions=(ConnectionError, TimeoutError)
)
def fetch_market_data(symbol: str):
    """获取市场数据（带重试）"""
    # 模拟 API 调用
    import random
    if random.random() < 0.5:
        raise ConnectionError("网络连接失败")
    return {'symbol': symbol, 'price': 50000}

# 调用（会自动重试）
try:
    data = fetch_market_data('BTCUSDT')
    print(f"市场数据: {data}")
except Exception as e:
    print(f"重试 3 次后仍然失败: {e}")
```

### 示例 8：类方法错误装饰器

```python
from utils.error_handler import error_handler_decorator

class DataProcessor:
    """数据处理器"""
    
    @error_handler_decorator
    def parse_json(self, json_str: str) -> dict:
        """解析 JSON（自动处理错误）"""
        import json
        return json.loads(json_str)
    
    @error_handler_decorator
    def validate_data(self, data: dict) -> bool:
        """验证数据（自动处理错误）"""
        if not data.get('symbol'):
            raise ValueError("缺少交易对")
        return True

# 使用示例
processor = DataProcessor()

# 正常解析
data = processor.parse_json('{"symbol": "BTCUSDT", "price": 50000}')
print(f"解析结果: {data}")

# 错误解析（自动捕获）
data = processor.parse_json('invalid json')
print(f"解析结果: {data}")  # 输出: None
```

---

## 飞书通知集成

### 示例 9：错误通知到飞书

```python
from utils.error_handler import ErrorHandler
from utils.lark_notifier import LarkNotifier
import os

# 创建飞书通知器
lark_webhook = os.getenv('LARK_WEBHOOK_URL')
notifier = LarkNotifier(lark_webhook) if lark_webhook else None

# 创建错误处理器（集成飞书通知）
error_handler = ErrorHandler(
    enable_notification=True,
    notifier=notifier
)

# 处理错误（自动发送飞书通知）
try:
    raise RuntimeError("交易系统异常：数据库连接失败")
except RuntimeError as e:
    error_handler.handle_error(
        error=e,
        context={'service': 'TradeExecutor', 'operation': 'execute_trade'},
        level='CRITICAL'
    )
    print("错误已记录并发送飞书通知")
```

### 示例 10：自定义通知消息

```python
from utils.lark_notifier import LarkNotifier
import os

# 创建飞书通知器
notifier = LarkNotifier(os.getenv('LARK_WEBHOOK_URL'))

# 发送错误通知
notifier.send_error_notification(
    title="交易系统错误",
    error_type="BinanceAPIError",
    error_message="API 调用失败：Rate limit exceeded",
    context={
        'symbol': 'BTCUSDT',
        'operation': 'place_order',
        'timestamp': '2026-04-27 16:30:00'
    }
)

# 发送告警通知
notifier.send_alert(
    level='WARNING',
    title="市场波动告警",
    message="BTCUSDT 24小时涨跌幅超过5%",
    details={
        'symbol': 'BTCUSDT',
        'change_percent': 6.5,
        'threshold': 5.0
    }
)
```

---

## 错误统计和历史记录

### 示例 11：错误统计

```python
from utils.error_handler import ErrorHandler

# 创建错误处理器
error_handler = ErrorHandler(enable_statistics=True)

# 模拟多个错误
errors = [
    ValueError("参数错误"),
    ConnectionError("网络错误"),
    ValueError("参数错误"),
    RuntimeError("运行时错误"),
    ConnectionError("网络错误"),
]

for error in errors:
    error_handler.handle_error(error)

# 获取错误统计
stats = error_handler.get_error_statistics()
print(f"错误统计: {stats}")

# 输出示例：
# {
#     'total_errors': 5,
#     'by_type': {
#         'ValueError': 2,
#         'ConnectionError': 2,
#         'RuntimeError': 1
#     },
#     'by_level': {
#         'ERROR': 5
#     }
# }
```

### 示例 12：错误历史记录

```python
from utils.error_handler import ErrorHandler

# 创建错误处理器
error_handler = ErrorHandler(max_history=100)

# 记录多个错误
for i in range(5):
    try:
        raise ValueError(f"错误 #{i+1}")
    except ValueError as e:
        error_handler.handle_error(
            error=e,
            context={'index': i, 'operation': 'test'}
        )

# 获取错误历史
history = error_handler.get_error_history(limit=3)
print(f"最近 3 条错误:")
for record in history:
    print(f"  - {record['timestamp']}: {record['error_type']} - {record['message']}")

# 清空历史记录
error_handler.clear_history()
print("历史记录已清空")
```

### 示例 13：错误报告生成

```python
from utils.error_handler import ErrorHandler
from datetime import datetime, timedelta

# 创建错误处理器
error_handler = ErrorHandler()

# 模拟一天的错误
for i in range(10):
    try:
        if i % 3 == 0:
            raise ConnectionError("网络连接失败")
        elif i % 3 == 1:
            raise ValueError("参数验证失败")
        else:
            raise RuntimeError("运行时错误")
    except Exception as e:
        error_handler.handle_error(e)

# 生成错误报告
report = error_handler.generate_error_report(
    start_time=datetime.now() - timedelta(days=1),
    end_time=datetime.now()
)

print(f"错误报告:")
print(f"  总错误数: {report['total_errors']}")
print(f"  错误类型分布: {report['error_distribution']}")
print(f"  高频错误: {report['top_errors']}")
```

---

## 最佳实践

### 示例 14：完整的错误处理流程

```python
from utils.error_handler import ErrorHandler, error_handler_decorator
from utils.exceptions import TradingSystemError, BinanceAPIError
from utils.lark_notifier import LarkNotifier
import logging

logger = logging.getLogger(__name__)

class TradeManager:
    """交易管理器（完整错误处理示例）"""
    
    def __init__(self):
        self.error_handler = ErrorHandler(
            enable_notification=True,
            enable_statistics=True,
            notifier=LarkNotifier()
        )
    
    @error_handler_decorator
    def execute_trade(self, signal: dict):
        """执行交易"""
        # 1. 验证信号
        self._validate_signal(signal)
        
        # 2. 计算仓位
        position = self._calculate_position(signal)
        
        # 3. 下单
        order = self._place_order(signal, position)
        
        return order
    
    def _validate_signal(self, signal: dict):
        """验证信号"""
        if not signal.get('symbol'):
            raise ValueError("缺少交易对信息")
        if signal.get('direction') not in ['LONG', 'SHORT']:
            raise ValueError(f"无效的方向: {signal.get('direction')}")
    
    def _calculate_position(self, signal: dict):
        """计算仓位"""
        try:
            # 仓位计算逻辑...
            return {'quantity': 0.01, 'margin': 10}
        except Exception as e:
            self.error_handler.handle_error(
                error=e,
                context={'signal': signal, 'operation': 'calculate_position'}
            )
            raise
    
    def _place_order(self, signal: dict, position: dict):
        """下单"""
        try:
            # 下单逻辑...
            return {'orderId': '123456', 'status': 'FILLED'}
        except BinanceAPIError as e:
            # API 错误特殊处理
            self.error_handler.handle_error(
                error=e,
                context={'signal': signal, 'position': position},
                level='CRITICAL'
            )
            raise

# 使用示例
manager = TradeManager()
try:
    order = manager.execute_trade({
        'symbol': 'BTCUSDT',
        'direction': 'LONG',
        'price': 50000
    })
    print(f"订单成功: {order}")
except Exception as e:
    logger.error(f"交易执行失败: {e}")
```

---

## 注意事项

1. **异常分类**：使用合适的异常类型，便于错误分类和处理
2. **上下文信息**：提供足够的上下文信息，便于问题排查
3. **错误级别**：合理设置错误级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
4. **通知策略**：避免过度通知，只对关键错误发送飞书通知
5. **历史记录**：定期清理历史记录，避免内存占用过大
6. **重试策略**：对可恢复的错误使用重试机制

---

## 相关文档

- [配置管理器使用示例](./config_manager_examples.md)
- [服务基类使用示例](./service_base_examples.md)
- [监控告警使用示例](./monitoring_examples.md)
