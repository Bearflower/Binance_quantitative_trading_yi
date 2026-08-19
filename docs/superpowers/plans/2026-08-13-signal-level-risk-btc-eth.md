# btc_eth 信号等级止盈止损 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 btc_eth 策略的全局止盈止损参数拆解为按信号等级（S/A/B/C）独立配置

**Architecture:** 在 config.yaml 中新增 `risk.signal_levels.S/A/B/C` 结构，策略代码中新增 `_get_grade_risk()` 方法按信号等级读取参数，PositionState 新增 `grade` 字段贯穿持仓生命周期。

**Tech Stack:** Python, btc_eth 策略 (4300+ lines), YAML 配置

---

### Task 1: 修改 config.yaml 添加 signal_levels 结构

**Files:**
- Modify: `strategies/btc_eth/config.yaml:137-210`

- [ ] **Step 1: 读取当前 config.yaml 的 risk 部分**

Read `strategies/btc_eth/config.yaml` lines 137-210 to understand current structure.

- [ ] **Step 2: 替换为 signal_levels 结构**

将全局 `risk` 下的以下参数替换为按信号等级拆解的结构：
- `stop_loss_atr_multiplier`
- `partial_take_profit.*`
- `dynamic_trailing.*`
- `time_stop.*`

保持以下参数不变（全局）：
- `close_limit_order.*`
- `stop_limit_order.*`
- `tp_limit_order.*`
- `micro_close.*`
- `max_position_size`

具体结构见设计文档 `docs/superpowers/specs/2026-08-13-signal-level-risk-design.md`。

---

### Task 2: PositionState 添加 grade 字段

**Files:**
- Modify: `strategies/btc_eth/strategy.py:32-69`

- [ ] **Step 1: 在 PositionState 中添加 grade 字段**

在 `__init__` 方法末尾添加：
```python
self.grade: str = ""  # 信号等级，用于动态读取对应的风险参数
```

---

### Task 3: 新增 _get_grade_risk 工具方法

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (在 `BTCEthStrategy` 类中新增方法)

- [ ] **Step 1: 在 `__init__` 方法附近添加 `_get_grade_risk` 方法**

在 `BTCEthStrategy` 类中新增：
```python
def _get_grade_risk(self, grade: str) -> Dict:
    """根据信号等级获取对应的风险参数
    
    Args:
        grade: 信号等级 (S/A/B/C)
    
    Returns:
        该等级的风险参数字典，若 signal_levels 不存在则返回全局 risk 配置
    """
    signal_levels = self.risk_config.get('signal_levels')
    if not signal_levels:
        return self.risk_config  # 向后兼容：无 signal_levels 时使用全局配置
    return signal_levels.get(grade, signal_levels.get('A', self.risk_config))
```

---

### Task 4: 下单时传递 grade 到 PositionState

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (place_order 方法)

- [ ] **Step 1: 找到 grade 确定的位置**

在 `analyze()` 方法中 line 817 附近，`grade = self._determine_grade(score, symbol)` 处。

- [ ] **Step 2: 在创建 PositionState 时传递 grade**

找到 `place_order` 方法中创建 `PositionState` 对象的代码（在 signal 字典中已有 grade），确保：
```python
position = PositionState()
position.grade = signal.get('grade', 'A')
```

---

### Task 5: 修改止损计算使用信号等级参数

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (~line 890)

- [ ] **Step 1: 修改止损参数读取**

将：
```python
sl_atr_mult = self.risk_config['stop_loss_atr_multiplier']
```
改为：
```python
grade_risk = self._get_grade_risk(signal.get('grade', 'A'))
sl_atr_mult = grade_risk['stop_loss_atr_multiplier']
```

---

### Task 6: 修改 _calculate_tp_price 使用信号等级参数

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (~line 1926)

- [ ] **Step 1: 修改方法签名，添加 grade 参数**

将：
```python
async def _calculate_tp_price(self, entry_price, atr, direction, tp_level):
```
改为：
```python
async def _calculate_tp_price(self, entry_price, atr, direction, tp_level, grade='A'):
```

- [ ] **Step 2: 修改内部参数读取**

将：
```python
partial_config = self.risk_config['partial_take_profit']
```
改为：
```python
grade_risk = self._get_grade_risk(grade)
partial_config = grade_risk['partial_take_profit']
```

- [ ] **Step 3: 更新所有调用处**

找到所有 `_calculate_tp_price` 的调用处，传入 `grade` 参数。

---

### Task 7: 修改 _check_partial_take_profit 使用信号等级参数

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (~line 2872)

- [ ] **Step 1: 修改参数读取**

将：
```python
partial_config = self.risk_config['partial_take_profit']
```
改为：
```python
grade_risk = self._get_grade_risk(position.grade)
partial_config = grade_risk['partial_take_profit']
```

---

### Task 8: 修改 _check_time_stop 使用信号等级参数

**Files:**
- Modify: `strategies/btc_eth/strategy.py` (~line 3435)

- [ ] **Step 1: 修改参数读取**

将：
```python
time_stop_config = self.risk_config['time_stop']
```
改为：
```python
grade_risk = self._get_grade_risk(position.grade)
time_stop_config = grade_risk['time_stop']
```

---

### Task 9: 更新 AI-Tuner 白名单

**Files:**
- Modify: `ai_tuner/config.yaml` (btc_eth 策略的 param_whitelist)

- [ ] **Step 1: 替换全局风险参数为信号等级参数**

从 whitelist 中移除旧的全局风险参数：
- `risk.stop_loss_atr_multiplier`
- `risk.partial_take_profit.*`
- `risk.chandelier_stop.*`
- `risk.time_stop.*`

添加新的信号等级参数（每个参数 × 4 个等级），具体列表见设计文档。

---

### Task 10: 编写测试

**Files:**
- Create: `strategies/btc_eth/tests/`（如果已有，添加测试用例）

- [ ] **Step 1: 测试 _get_grade_risk 方法**

测试用例：
1. 有 signal_levels 时返回对应等级配置
2. 不存在的等级回退到 A 级
3. 无 signal_levels 时返回全局配置（向后兼容）

- [ ] **Step 2: 测试 PositionState grade 字段持久化**

- [ ] **Step 3: 测试各信号等级的参数差异化**

---

### Task 11: 代码检测 + 幻觉测试

- [ ] 检查所有 import 语句
- [ ] 检查所有 API 调用参数
- [ ] 检查配置项引用
- [ ] 检查向后兼容逻辑

---

### Task 12: 部署到服务器

- [ ] 生成 VERSION 文件
- [ ] 打包部署
- [ ] 验证容器内代码
- [ ] 确认日志无错误