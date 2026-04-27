# 配置管理器使用示例

本文档详细介绍如何使用配置管理器（ConfigManager）进行配置读取、验证和管理。

## 目录

1. [基本配置读取](#基本配置读取)
2. [环境变量覆盖](#环境变量覆盖)
3. [配置验证](#配置验证)
4. [类型转换示例](#类型转换示例)
5. [向后兼容接口](#向后兼容接口)
6. [最佳实践](#最佳实践)

---

## 基本配置读取

### 示例 1：读取简单配置

```python
from config.config_manager import ConfigManager

# 创建配置管理器实例
config = ConfigManager()

# 读取字符串配置
symbol = config.get('trading.default_symbol')
print(f"默认交易对: {symbol}")

# 读取整数配置
max_positions = config.get('account.max_positions')
print(f"最大持仓数: {max_positions}")

# 读取浮点数配置
risk_ratio = config.get('position_sizing.risk_ratio')
print(f"风险比例: {risk_ratio}")
```

**输出示例：**
```
默认交易对: BTCUSDT
最大持仓数: 2
风险比例: 0.02
```

### 示例 2：读取嵌套配置

```python
from config.config_manager import ConfigManager

config = ConfigManager()

# 读取嵌套配置（使用点号分隔）
tp_config = config.get('risk_management.take_profit_levels')
print(f"止盈配置: {tp_config}")

# 读取嵌套配置中的特定字段
tp1_ratio = config.get('risk_management.take_profit_levels.tp1_ratio')
print(f"TP1 平仓比例: {tp1_ratio}")
```

**输出示例：**
```
止盈配置: {'tp1_multiplier': 1.5, 'tp2_multiplier': 2.5, 'tp1_ratio': 0.3, 'tp2_ratio': 0.3, 'tp3_ratio': 0.4}
TP1 平仓比例: 0.3
```

### 示例 3：使用默认值

```python
from config.config_manager import ConfigManager

config = ConfigManager()

# 读取不存在的配置，使用默认值
timeout = config.get('api.timeout', default=30)
print(f"API 超时时间: {timeout}秒")

# 读取存在的配置，忽略默认值
symbol = config.get('trading.default_symbol', default='ETHUSDT')
print(f"交易对: {symbol}")
```

**输出示例：**
```
API 超时时间: 30秒
交易对: BTCUSDT
```

---

## 环境变量覆盖

### 示例 4：环境变量优先级

```python
import os
from config.config_manager import ConfigManager

# 设置环境变量
os.environ['TRADING_DEFAULT_SYMBOL'] = 'ETHUSDT'
os.environ['ACCOUNT_MAX_POSITIONS'] = '3'

# 创建配置管理器
config = ConfigManager()

# 读取配置（环境变量优先级高于配置文件）
symbol = config.get('trading.default_symbol')
max_positions = config.get('account.max_positions')

print(f"交易对: {symbol}")  # 输出: ETHUSDT（来自环境变量）
print(f"最大持仓: {max_positions}")  # 输出: 3（来自环境变量）
```

### 示例 5：环境变量命名规则

```python
import os
from config.config_manager import ConfigManager

# 环境变量命名规则：大写 + 下划线
# 配置键 'risk_management.max_daily_loss' 对应环境变量 'RISK_MANAGEMENT_MAX_DAILY_LOSS'
os.environ['RISK_MANAGEMENT_MAX_DAILY_LOSS'] = '50'

config = ConfigManager()
max_loss = config.get('risk_management.max_daily_loss')

print(f"最大日损失: {max_loss}U")  # 输出: 50U
```

---

## 配置验证

### 示例 6：必填配置验证

```python
from config.config_manager import ConfigManager
from utils.exceptions import ConfigurationError

config = ConfigManager()

try:
    # 读取必填配置（如果不存在会抛出异常）
    api_key = config.get('binance.api_key', required=True)
    print(f"API Key: {api_key[:8]}...")
except ConfigurationError as e:
    print(f"配置错误: {e}")
```

**输出示例（配置不存在时）：**
```
配置错误: 必需的配置项 'binance.api_key' 不存在
```

### 示例 7：配置类型验证

```python
from config.config_manager import ConfigManager
from decimal import Decimal

config = ConfigManager()

# 读取并验证类型
max_positions = config.get('account.max_positions', expected_type=int)
print(f"最大持仓数（整数）: {max_positions}")

risk_amount = config.get('position_sizing.risk_amount', expected_type=Decimal)
print(f"风险金额（Decimal）: {risk_amount}")
```

---

## 类型转换示例

### 示例 8：自动类型转换

```python
from config.config_manager import ConfigManager
from decimal import Decimal

config = ConfigManager()

# 字符串转整数
max_positions = config.get('account.max_positions')
print(f"类型: {type(max_positions)}, 值: {max_positions}")

# 字符串转浮点数
risk_ratio = config.get('position_sizing.risk_ratio')
print(f"类型: {type(risk_ratio)}, 值: {risk_ratio}")

# 字符串转 Decimal
risk_amount = config.get('position_sizing.risk_amount', expected_type=Decimal)
print(f"类型: {type(risk_amount)}, 值: {risk_amount}")
```

**输出示例：**
```
类型: <class 'int'>, 值: 2
类型: <class 'float'>, 值: 0.02
类型: <class 'decimal.Decimal'>, 值: 10
```

### 示例 9：布尔值转换

```python
from config.config_manager import ConfigManager

config = ConfigManager()

# 布尔值配置
testnet = config.get('binance.testnet', expected_type=bool)
print(f"测试网模式: {testnet}")

# 支持的布尔值格式
# True: true, True, 1, yes, on
# False: false, False, 0, no, off
```

---

## 向后兼容接口

### 示例 10：兼容旧代码

```python
from config.strategy_params import StrategyParams, get_params

# 使用旧接口（向后兼容）
params = get_params()

# 读取配置
risk_amount = params.get('position_sizing.risk_amount')
print(f"风险金额: {risk_amount}U")

# 读取嵌套配置
leverage = params.get('position_sizing.leverage_by_grade.A')
print(f"A 级杠杆: {leverage}x")
```

### 示例 11：混合使用新旧接口

```python
from config.config_manager import ConfigManager
from config.strategy_params import get_params

# 新接口
config = ConfigManager()
max_positions = config.get('account.max_positions')

# 旧接口
params = get_params()
risk_amount = params.get('position_sizing.risk_amount')

print(f"最大持仓: {max_positions}")
print(f"风险金额: {risk_amount}U")
```

---

## 最佳实践

### 示例 12：集中管理配置

```python
from config.config_manager import ConfigManager

class TradingConfig:
    """交易配置集中管理类"""
    
    def __init__(self):
        self.config = ConfigManager()
    
    @property
    def max_positions(self):
        """最大持仓数"""
        return self.config.get('account.max_positions')
    
    @property
    def risk_amount(self):
        """单笔风险金额"""
        from decimal import Decimal
        return self.config.get('position_sizing.risk_amount', expected_type=Decimal)
    
    @property
    def default_symbol(self):
        """默认交易对"""
        return self.config.get('trading.default_symbol', default='BTCUSDT')
    
    def get_leverage(self, grade: str) -> int:
        """根据信号等级获取杠杆"""
        return self.config.get(f'position_sizing.leverage_by_grade.{grade}', default=3)

# 使用示例
trading_config = TradingConfig()
print(f"最大持仓: {trading_config.max_positions}")
print(f"风险金额: {trading_config.risk_amount}U")
print(f"S 级杠杆: {trading_config.get_leverage('S')}x")
```

### 示例 13：配置缓存

```python
from config.config_manager import ConfigManager
from functools import lru_cache

class CachedConfig:
    """带缓存的配置管理"""
    
    def __init__(self):
        self.config = ConfigManager()
    
    @lru_cache(maxsize=128)
    def get(self, key: str, default=None):
        """缓存配置读取结果"""
        return self.config.get(key, default=default)

# 使用示例
cached_config = CachedConfig()

# 第一次读取（从文件读取）
symbol1 = cached_config.get('trading.default_symbol')

# 第二次读取（从缓存读取）
symbol2 = cached_config.get('trading.default_symbol')

print(f"交易对: {symbol1}")
```

### 示例 14：配置更新监听

```python
from config.config_manager import ConfigManager
import time

class ConfigWatcher:
    """配置变更监听器"""
    
    def __init__(self):
        self.config = ConfigManager()
        self.last_modified = time.time()
    
    def check_update(self):
        """检查配置是否更新"""
        current_time = time.time()
        if current_time - self.last_modified > 60:  # 每分钟检查一次
            self.config.reload()
            self.last_modified = current_time
            print("配置已重新加载")
    
    def get(self, key: str, default=None):
        """获取配置（自动检查更新）"""
        self.check_update()
        return self.config.get(key, default=default)

# 使用示例
watcher = ConfigWatcher()
while True:
    symbol = watcher.get('trading.default_symbol')
    print(f"当前交易对: {symbol}")
    time.sleep(10)
```

---

## 注意事项

1. **环境变量优先级**：环境变量 > 配置文件
2. **类型安全**：使用 `expected_type` 参数确保类型正确
3. **必填配置**：使用 `required=True` 确保关键配置存在
4. **默认值**：为可选配置提供合理的默认值
5. **缓存策略**：频繁读取的配置考虑使用缓存
6. **配置验证**：启动时验证所有关键配置

---

## 相关文档

- [异常处理使用示例](./exception_handling_examples.md)
- [服务基类使用示例](./service_base_examples.md)
- [核心模块 API 文档](../api/核心模块API文档.md)
