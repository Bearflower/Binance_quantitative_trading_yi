# 服务基类使用示例

本文档详细介绍如何使用服务基类（BaseService）创建自定义服务，包括状态管理、错误处理、配置访问等功能。

## 目录

1. [创建自定义服务](#创建自定义服务)
2. [状态管理](#状态管理)
3. [错误处理](#错误处理)
4. [配置访问](#配置访问)
5. [上下文管理器](#上下文管理器)

---

## 创建自定义服务

### 示例 1：基本服务类

```python
from services.base import BaseService

class MyService(BaseService):
    """自定义服务示例"""
    
    def _initialize(self):
        """初始化服务（子类必须实现）"""
        # 加载配置
        self.max_retries = self.get_config_value('api.max_retries', default=3)
        
        # 初始化组件
        self.api_client = None
        
        # 记录初始化完成
        self.log_info("服务初始化完成")

# 使用示例
service = MyService(service_name="MyService")
print(f"服务状态: {service.state}")
```

### 示例 2：带参数的服务类

```python
from services.base import BaseService

class TradingService(BaseService):
    """交易服务"""
    
    def __init__(self, symbol: str, testnet: bool = False, **kwargs):
        """
        初始化交易服务
        
        Args:
            symbol: 交易对
            testnet: 是否使用测试网
        """
        self.symbol = symbol
        self.testnet = testnet
        super().__init__(**kwargs)
    
    def _initialize(self):
        """初始化服务"""
        # 根据交易对加载配置
        self.max_position = self.get_config_value(
            f'trading.{self.symbol}.max_position',
            default=0.01
        )
        
        # 初始化 API 客户端
        if self.testnet:
            self.log_info(f"使用测试网: {self.symbol}")
        else:
            self.log_info(f"使用主网: {self.symbol}")

# 使用示例
service = TradingService(symbol='BTCUSDT', testnet=True, service_name='TradingService')
```

### 示例 3：服务方法装饰器

```python
from services.base import BaseService, service_method

class DataService(BaseService):
    """数据服务"""
    
    def _initialize(self):
        """初始化"""
        self.cache = {}
    
    @service_method()
    def get_market_data(self, symbol: str):
        """获取市场数据（自动记录状态）"""
        # 检查缓存
        if symbol in self.cache:
            self.log_info(f"从缓存获取 {symbol} 数据")
            return self.cache[symbol]
        
        # 获取数据
        data = self._fetch_from_api(symbol)
        self.cache[symbol] = data
        
        return data
    
    def _fetch_from_api(self, symbol: str):
        """从 API 获取数据"""
        # 模拟 API 调用
        return {'symbol': symbol, 'price': 50000}

# 使用示例
service = DataService(service_name='DataService')
data = service.get_market_data('BTCUSDT')
print(f"市场数据: {data}")
```

---

## 状态管理

### 示例 4：服务状态流转

```python
from services.base import BaseService, ServiceState

class StatefulService(BaseService):
    """有状态的服务"""
    
    def _initialize(self):
        """初始化"""
        print(f"当前状态: {self.state}")  # 初始化中
    
    def start(self):
        """启动服务"""
        if self.state != ServiceState.READY:
            raise RuntimeError(f"服务未就绪，当前状态: {self.state}")
        
        self.set_state(ServiceState.RUNNING)
        self.log_info("服务已启动")
    
    def stop(self):
        """停止服务"""
        if self.state != ServiceState.RUNNING:
            raise RuntimeError(f"服务未运行，当前状态: {self.state}")
        
        self.set_state(ServiceState.STOPPED)
        self.log_info("服务已停止")

# 使用示例
service = StatefulService(service_name='StatefulService')
print(f"初始化后状态: {service.state}")  # 就绪

service.start()
print(f"启动后状态: {service.state}")  # 运行中

service.stop()
print(f"停止后状态: {service.state}")  # 已停止
```

### 示例 5：状态检查

```python
from services.base import BaseService, ServiceState

class SafeService(BaseService):
    """安全服务（带状态检查）"""
    
    def _initialize(self):
        """初始化"""
        pass
    
    def process(self, data: dict):
        """处理数据（仅在运行状态执行）"""
        # 检查服务状态
        if not self.is_running():
            raise RuntimeError("服务未运行，无法处理数据")
        
        # 处理数据
        result = self._do_process(data)
        return result
    
    def _do_process(self, data: dict):
        """实际处理逻辑"""
        return {'processed': True, 'data': data}

# 使用示例
service = SafeService(service_name='SafeService')

try:
    service.process({'test': 'data'})
except RuntimeError as e:
    print(f"错误: {e}")  # 服务未运行
```

---

## 错误处理

### 示例 6：自动错误处理

```python
from services.base import BaseService

class RobustService(BaseService):
    """健壮的服务（自动错误处理）"""
    
    def _initialize(self):
        """初始化"""
        pass
    
    def risky_operation(self, data: dict):
        """风险操作（自动捕获异常）"""
        try:
            # 可能失败的操作
            if not data.get('required_field'):
                raise ValueError("缺少必需字段")
            
            return {'success': True}
        
        except Exception as e:
            # 使用服务基类的错误处理
            self.handle_error(e, context={'data': data, 'operation': 'risky_operation'})
            return {'success': False, 'error': str(e)}

# 使用示例
service = RobustService(service_name='RobustService')

# 正常调用
result = service.risky_operation({'required_field': 'value'})
print(f"结果: {result}")

# 错误调用（自动处理）
result = service.risky_operation({})
print(f"结果: {result}")
```

### 示例 7：错误恢复

```python
from services.base import BaseService

class ResilientService(BaseService):
    """弹性服务（带错误恢复）"""
    
    def _initialize(self):
        """初始化"""
        self.retry_count = 0
        self.max_retries = 3
    
    def fetch_data_with_retry(self, url: str):
        """获取数据（带重试）"""
        while self.retry_count < self.max_retries:
            try:
                data = self._fetch_data(url)
                self.retry_count = 0  # 重置重试计数
                return data
            
            except Exception as e:
                self.retry_count += 1
                self.handle_error(
                    e,
                    context={'url': url, 'retry': self.retry_count},
                    level='WARNING'
                )
                
                if self.retry_count >= self.max_retries:
                    self.set_state(self.STATE_ERROR)
                    raise
        
        return None
    
    def _fetch_data(self, url: str):
        """获取数据"""
        # 模拟 API 调用
        import random
        if random.random() < 0.5:
            raise ConnectionError("网络错误")
        return {'url': url, 'data': 'success'}

# 使用示例
service = ResilientService(service_name='ResilientService')
try:
    data = service.fetch_data_with_retry('https://api.example.com/data')
    print(f"数据: {data}")
except Exception as e:
    print(f"重试失败: {e}")
```

---

## 配置访问

### 示例 8：配置读取

```python
from services.base import BaseService
from decimal import Decimal

class ConfigurableService(BaseService):
    """可配置的服务"""
    
    def _initialize(self):
        """初始化"""
        # 读取必需配置
        self.api_key = self.get_config_value(
            'api.key',
            required=True
        )
        
        # 读取可选配置（带默认值）
        self.timeout = self.get_config_value(
            'api.timeout',
            default=30
        )
        
        # 读取并转换类型
        self.risk_amount = self.get_config_value(
            'position_sizing.risk_amount',
            default=10,
            expected_type=Decimal
        )
        
        self.log_info(f"配置加载完成: timeout={self.timeout}s, risk={self.risk_amount}U")

# 使用示例
try:
    service = ConfigurableService(service_name='ConfigurableService')
except Exception as e:
    print(f"配置错误: {e}")
```

### 示例 9：动态配置更新

```python
from services.base import BaseService

class DynamicConfigService(BaseService):
    """动态配置服务"""
    
    def _initialize(self):
        """初始化"""
        self._load_config()
    
    def _load_config(self):
        """加载配置"""
        self.max_positions = self.get_config_value('account.max_positions', default=2)
        self.log_info(f"最大持仓数: {self.max_positions}")
    
    def reload_config(self):
        """重新加载配置"""
        self.log_info("重新加载配置...")
        self._load_config()
    
    def update_position_limit(self, new_limit: int):
        """更新持仓限制"""
        old_limit = self.max_positions
        self.max_positions = new_limit
        self.log_info(f"持仓限制已更新: {old_limit} -> {new_limit}")

# 使用示例
service = DynamicConfigService(service_name='DynamicConfigService')
print(f"当前持仓限制: {service.max_positions}")

service.update_position_limit(3)
print(f"更新后持仓限制: {service.max_positions}")
```

---

## 上下文管理器

### 示例 10：使用 with 语句

```python
from services.base import BaseService

class ResourceService(BaseService):
    """资源管理服务"""
    
    def _initialize(self):
        """初始化"""
        self.connection = None
    
    def __enter__(self):
        """进入上下文"""
        self.log_info("打开资源连接")
        self.connection = self._open_connection()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文"""
        if self.connection:
            self.log_info("关闭资源连接")
            self._close_connection()
        
        if exc_type:
            self.handle_error(exc_val, context={'operation': 'context_manager'})
        
        return False  # 不抑制异常
    
    def _open_connection(self):
        """打开连接"""
        return {'status': 'connected'}
    
    def _close_connection(self):
        """关闭连接"""
        self.connection = None
    
    def use_resource(self):
        """使用资源"""
        if not self.connection:
            raise RuntimeError("资源未连接")
        return {'result': 'success'}

# 使用示例
with ResourceService(service_name='ResourceService') as service:
    result = service.use_resource()
    print(f"使用资源: {result}")
```

### 示例 11：嵌套上下文

```python
from services.base import BaseService

class TransactionService(BaseService):
    """事务服务"""
    
    def _initialize(self):
        """初始化"""
        self.in_transaction = False
    
    def begin_transaction(self):
        """开始事务"""
        return self._TransactionContext(self)
    
    class _TransactionContext:
        """事务上下文"""
        
        def __init__(self, service):
            self.service = service
        
        def __enter__(self):
            self.service.in_transaction = True
            self.service.log_info("事务开始")
            return self
        
        def __exit__(self, exc_type, exc_val, exc_tb):
            self.service.in_transaction = False
            if exc_type:
                self.service.log_error("事务回滚")
            else:
                self.service.log_info("事务提交")
            return False

# 使用示例
service = TransactionService(service_name='TransactionService')

with service.begin_transaction():
    service.log_info("执行事务操作...")
    # 如果这里发生异常，事务会自动回滚
```

---

## 最佳实践

### 示例 12：完整的服务实现

```python
from services.base import BaseService, service_method, ServiceState
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

class CompleteService(BaseService):
    """完整的服务实现示例"""
    
    def __init__(self, symbol: str, **kwargs):
        """
        初始化服务
        
        Args:
            symbol: 交易对
        """
        self.symbol = symbol
        super().__init__(**kwargs)
    
    def _initialize(self):
        """初始化服务"""
        # 1. 加载配置
        self._load_config()
        
        # 2. 初始化组件
        self._init_components()
        
        # 3. 验证状态
        self._validate_initialization()
        
        self.log_info(f"服务初始化完成: {self.symbol}")
    
    def _load_config(self):
        """加载配置"""
        self.max_position = self.get_config_value(
            f'trading.{self.symbol}.max_position',
            default=Decimal('0.01'),
            expected_type=Decimal
        )
        
        self.risk_amount = self.get_config_value(
            'position_sizing.risk_amount',
            default=Decimal('10'),
            expected_type=Decimal
        )
    
    def _init_components(self):
        """初始化组件"""
        # 初始化 API 客户端、数据库连接等
        self.api_client = None
        self.db_connection = None
    
    def _validate_initialization(self):
        """验证初始化"""
        if not self.symbol:
            raise ValueError("交易对不能为空")
        
        if self.max_position <= 0:
            raise ValueError("最大持仓必须大于 0")
    
    @service_method()
    def execute_trade(self, signal: dict):
        """执行交易"""
        # 1. 验证信号
        self._validate_signal(signal)
        
        # 2. 执行交易
        result = self._do_trade(signal)
        
        # 3. 记录结果
        self._log_trade_result(result)
        
        return result
    
    def _validate_signal(self, signal: dict):
        """验证信号"""
        if signal.get('symbol') != self.symbol:
            raise ValueError(f"信号交易对不匹配: {signal.get('symbol')} != {self.symbol}")
    
    def _do_trade(self, signal: dict):
        """执行交易"""
        # 实际交易逻辑
        return {
            'symbol': self.symbol,
            'status': 'FILLED',
            'quantity': self.max_position
        }
    
    def _log_trade_result(self, result: dict):
        """记录交易结果"""
        self.log_info(f"交易完成: {result}")
    
    def cleanup(self):
        """清理资源"""
        self.log_info("清理资源...")
        # 关闭连接、释放资源等
        self.set_state(ServiceState.STOPPED)

# 使用示例
try:
    service = CompleteService(symbol='BTCUSDT', service_name='CompleteService')
    
    # 执行交易
    result = service.execute_trade({
        'symbol': 'BTCUSDT',
        'direction': 'LONG',
        'price': 50000
    })
    print(f"交易结果: {result}")
    
finally:
    service.cleanup()
```

---

## 注意事项

1. **初始化顺序**：确保在 `__init__` 中调用 `super().__init__()`
2. **状态管理**：使用 `set_state()` 而不是直接修改 `self.state`
3. **错误处理**：使用 `handle_error()` 统一处理异常
4. **日志记录**：使用 `log_info/warning/error` 方法记录日志
5. **配置访问**：使用 `get_config_value()` 读取配置
6. **资源清理**：实现 `cleanup()` 方法释放资源

---

## 相关文档

- [配置管理器使用示例](./config_manager_examples.md)
- [异常处理使用示例](./exception_handling_examples.md)
- [数据仓库使用示例](./repository_examples.md)
