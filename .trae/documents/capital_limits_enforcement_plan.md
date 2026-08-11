# 资金分配限制执行方案（长期）

## 一、现状分析

### 现有资金分配系统
- `ai_tuner/allocation/config_updater._update_strategy_configs()` 每月将分配结果写入各策略 `config.yaml` 的 `capital_limits` 字段
- 格式：`{ monthly_limit, allocated_ratio, allocation_month, updated_at }`
- **但没有任何策略读取这个字段**，分配结果只存在于配置文件中，无实际约束力

### 各策略仓位计算现状

| 策略 | 资金来源 | 仓位公式 | 问题 |
|------|---------|---------|------|
| **MTPCS** | `availableBalance`（全账户可用余额） | `usable_balance * position_ratio %`，再波动率调整 | 用全账户余额，无视分配 |
| **新币做空** | `get_account_balance()['USDT']`（全账户USDT余额） | `single_position_margin * leverage` 固定值 | 本身用固定保证金，但余额检查用了全账户 |
| **HRS** | `totalMarginBalance`（全账户总保证金） | `balance * 2% / stop_loss_percent` | 用全账户余额，无视分配 |

### 配置更新路径
- `ai_tuner/config.yaml` -> `strategies` 列表中有 `config_path` 字段
- btc_eth: `strategies/btc_eth/config.yaml`
- new_coin: `strategies/new_coin/config.yaml`
- hrs: `strategies/hrs/config.yaml`

---

## 二、设计方案

### 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    shared/capital_manager.py                     │
│                                                                  │
│  CapitalManager                                                  │
│  ├─ __init__(config_path)   ← 策略传入自己的 config.yaml 路径     │
│  ├─ get_allocated_capital() → USDT 金额                          │
│  ├─ get_allocated_ratio()   → 分配比例                           │
│  └─ is_allocated()          → bool（是否已分配）                  │
│                                                                  │
│  策略初始化时创建 CapitalManager 实例，开仓时用它替换全账户余额    │
└─────────────────────────────────────────────────────────────────┘
```

### 核心设计原则

1. **向后兼容**：`capital_limits` 未设置时，退回使用全账户余额（现有行为不变）
2. **最小侵入**：只修改各策略的资金来源，不重构仓位计算逻辑本身
3. **配置驱动**：`capital_limits` 由月度资金分配系统自动写入，策略只负责读取
4. **无锁设计**：`CapitalManager` 每次调用时重新读取配置文件，避免缓存不一致

---

## 三、详细实现

### 3.1 创建 `shared/capital_manager.py`（由 `python-engineer` 负责）

新建文件，实现 `CapitalManager` 类：

```python
class CapitalManager:
    """
    资金分配管理器
    
    从策略的 config.yaml 中读取 capital_limits 配置，
    提供策略可用的分配资金上限。
    
    用法：
        capital_mgr = CapitalManager("strategies/btc_eth/config.yaml")
        allocated = capital_mgr.get_allocated_capital()
        if allocated is None:
            # 使用全账户余额（回退行为）
            balance = get_full_balance()
        else:
            balance = allocated
    """
    
    def __init__(self, config_path: str):
        self.config_path = config_path
    
    def get_allocated_capital(self) -> Optional[float]:
        """
        读取分配资金上限
        
        Returns:
            float: 分配资金 USDT 金额
            None: 未配置 capital_limits，调用方应使用全账户余额
        """
        # 读取 config.yaml
        # 检查 capital_limits.monthly_limit 是否存在
        # 存在则返回，不存在返回 None
    
    def get_allocated_ratio(self) -> Optional[float]:
        """返回分配比例"""
    
    def is_allocated(self) -> bool:
        """capital_limits 是否已配置"""
```

**关键实现细节：**
- 每次调用都重新读取 YAML 文件，确保获取最新分配
- 使用 `yaml.safe_load` 读取
- 路径解析：相对于项目根目录或绝对路径
- 异常处理：文件不存在、格式错误时返回 None

### 3.2 修改 MTPCS 策略（由 `python-engineer` 负责）

**文件：** `strategies/btc_eth/strategy.py`

**改点 1：初始化时创建 CapitalManager（约 L1757 附近）**

```python
# 在 __init__ 或 _calculate_position_size 方法中
from shared.capital_manager import CapitalManager

# 计算策略配置文件路径
config_dir = os.path.dirname(os.path.abspath(__file__))
self.capital_mgr = CapitalManager(os.path.join(config_dir, "config.yaml"))
```

**改点 2：替换 `_calculate_position_size` 中的资金来源（L1757-L1758）**

```python
# 原有代码：
account_info = await self.binance.get_account_info()
available_balance = Decimal(str(account_info['availableBalance']))

# 改为：
allocated = self.capital_mgr.get_allocated_capital()
if allocated is not None:
    available_balance = Decimal(str(allocated))
else:
    account_info = await self.binance.get_account_info()
    available_balance = Decimal(str(account_info['availableBalance']))
```

**原因：** MTPCS 的 `position_size = usable_balance * position_ratio`，其中 `usable_balance = available_balance * (1 - safety_margin_ratio)`。将 `available_balance` 替换为分配金额后，`position_ratio` 等后续逻辑无需改动，自动限仓。

### 3.3 修改新币做空策略（由 `python-engineer` 负责）

**文件：** `strategies/new_coin/executor.py`

**改点 1：初始化时创建 CapitalManager**

```python
from shared.capital_manager import CapitalManager

# 在 __init__ 中
self.capital_mgr = CapitalManager(config_path)  # 需要 caller 传入 config_path
```

**改点 2：修改 `_get_account_balance` 方法（L251-L260）**

```python
async def _get_account_balance(self) -> Decimal:
    """获取可用资金（优先使用分配金额）"""
    allocated = self.capital_mgr.get_allocated_capital()
    if allocated is not None:
        return Decimal(str(allocated))
    # 回退：从 Binance API 获取
    try:
        balance = await self.binance_api.get_account_balance()
        return balance.get('USDT', Decimal('0'))
    except Exception as e:
        logger.error(f"获取账户余额失败: {e}")
        return Decimal('0')
```

**注意：** 新币做空策略的 `_calculate_position_size` 使用固定 `single_position_margin * leverage`，不依赖余额大小。但 `_get_account_balance` 的返回值用于余额检查（L158 `if balance <= 0`），所以替换后不影响仓位计算，只影响余额健康检查。

### 3.4 修改 HRS 策略（由 `python-engineer` 负责）

**文件：** `strategies/hrs/strategy.py`

**改点 1：初始化时创建 CapitalManager**

```python
from shared.capital_manager import CapitalManager

# 在 strategy 的 __init__ 中
config_dir = os.path.dirname(os.path.abspath(__file__))
self.capital_mgr = CapitalManager(os.path.join(config_dir, "config.yaml"))
```

**改点 2：替换 balance 来源（L383-L385）**

```python
# 原有代码：
account_info = await self.binance_client.get_account_info()
balance = float(account_info.get("totalMarginBalance", 0))

# 改为：
allocated = self.capital_mgr.get_allocated_capital()
if allocated is not None:
    balance = allocated
else:
    account_info = await self.binance_client.get_account_info()
    balance = float(account_info.get("totalMarginBalance", 0))
```

**影响：** 替换后，`risk_manager.calculate_position_size(balance, ...)` 中的 `balance` 变为分配金额。仓位计算变为 `max_loss = allocated * 2%`，`position_value = max_loss / stop_loss_percent`，自动按分配资金限仓。

### 3.5 验证 config_updater 写入路径正确性（由 `code-document-curator` 负责）

**文件：** `ai_tuner/allocation/config_updater.py`

检查 `_update_strategy_configs` 方法中：
- `strategy_paths` 映射是否覆盖了所有 3 个策略（btc_eth, new_coin, hrs）
- `config_path` 是否使用绝对路径，是否能被各策略的 `CapitalManager` 正确读取

**检查结果：** 从 `ai_tuner/config.yaml` 的 `strategies` 配置看，路径是相对路径（如 `strategies/btc_eth/config.yaml`），而 `config_updater` 中需要确保使用绝对路径写入。`ConfigOperator.apply_changes` 内部会处理路径解析，需要确认。

---

## 四、各智能体/技能职责分配

| 环节 | 负责方 | 具体任务 |
|------|--------|---------|
| 架构设计 | `backend-architect` | 审核本方案，确认 CapitalManager 设计是否合理，确认各策略改动点 |
| 编码实现 | `python-engineer` | 1. 创建 `shared/capital_manager.py` 2. 修改 3 个策略的资金来源 3. 处理异常和边界情况 |
| 代码检测 | `code-specification-inspector` | 检查硬编码、异常处理、路径解析、向后兼容性 |
| 代码审查 | `TRAE-code-review` 技能 | 审查代码质量、边界条件、可维护性 |
| 文档对照 | `code-document-curator` | 检查 `config_updater.py` 写入路径是否正确，检查 `docs/` 下文档是否需要更新 |
| 强制测试 | `api-test-pro` | 模拟各策略的仓位计算，验证分配金额生效；验证未分配时回退行为正常 |
| 部署 | `服务器自动化部署` 技能 | 部署到服务器，验证 `capital_limits` 在策略配置中存在且被正确读取 |

---

## 五、边界情况处理

### 5.1 首次部署（capital_limits 未设置）
- 所有策略的 `get_allocated_capital()` 返回 None
- 策略自动回退到使用全账户余额（现有行为）
- 当月度资金分配首次执行后，`capital_limits` 自动写入各策略配置

### 5.2 分配金额为 0
- 如果 `monthly_limit = 0`，表示该策略本月不分配资金
- 策略应返回不可交易（`balance = 0`，仓位计算返回 0，不执行开仓）

### 5.3 配置文件不存在
- `CapitalManager` 应返回 None，不抛异常
- 策略回退到全账户余额

### 5.4 配置文件格式错误
- YAML 解析异常时 catch 并返回 None
- 日志记录错误，不影响策略运行

### 5.5 多实例并发读取
- 每次调用都重新读取文件，不缓存
- 避免多线程/多进程间缓存不一致问题

---

## 六、验证方案

### 6.1 单元测试
- `CapitalManager` 读取正常配置 -> 返回正确金额
- `CapitalManager` 无配置 -> 返回 None
- `CapitalManager` 配置不存在 -> 返回 None
- `CapitalManager` 格式错误 -> 返回 None

### 6.2 集成测试
- 模拟 `capital_limits` 已设置，各策略开仓时使用分配金额
- 模拟 `capital_limits` 未设置，各策略回退到全账户余额
- 模拟 `monthly_limit = 0`，策略不可开仓

### 6.3 部署验证
- 检查各策略 config.yaml 中 `capital_limits` 字段是否存在
- 手动触发一次月度资金分配，确认写入成功
- 触发各策略开仓，检查日志中仓位计算是否使用了分配金额

---

## 七、回滚方案

如果出现问题，需要回退到使用全账户余额：
1. 从各策略中移除 `CapitalManager` 的引入和调用
2. 恢复 `balance` 获取代码为直接从 Binance API 获取
3. 重新部署