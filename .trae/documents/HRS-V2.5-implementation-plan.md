# HRS V2.5 候选池扩容 + LV-RM 独立化 — 实施计划

---

## 一、任务概述

**目标**：将 HRS 策略从 V2.4 升级到 V2.5，核心变更包括：
1. 候选池筛选逻辑从 AND（4 条件必须全部满足）改为 **OR-2（任意 2 条件即可入池）**
2. 动态阈值分位数从 20/80 分位统一改为 **50 分位（中位数）**
3. 涨跌幅阈值对称统一为 **8%（绝对值）**
4. LV-RM 扫描范围从依赖候选池落选币种改为 **全市场流动性币种独立扫描**
5. 候选池为空时策略不再休眠，改为 **双轨策略（LV-RM 继续运行 + 扫描退避）**
6. 补充风险分析、回退方案、数据库字段

**涉及文件**：
- `strategies/hrs/config.yaml` — 配置参数修改
- `strategies/hrs/candidate_pool.py` — 核心逻辑重构
- `strategies/hrs/strategy.py` — 主循环逻辑修改
- `strategies/hrs/scoring_engine.py` — 无变更（确认）
- 文档文件更新

---

## 二、当前状态分析

### 2.1 候选池当前逻辑（V2.4）

```
scan_and_update() 流程：
1. 获取全市场数据
2. 涨跌幅初筛 → short_candidates/ long_candidates（仅基于 price_change）
3. 动态阈值计算（80/20/70/30 分位）
4. 对每个初筛候选调用 _validate_short/long_candidate() → 3 个 AND 条件
5. 最终候选池 = 涨跌幅 AND 费率 AND OI/市值 AND EMA 偏离
```

### 2.2 LV-RM 当前逻辑（V2.4）

```
get_low_volatility_candidates():
1. 从 _eliminated_symbols（落选币种）中筛选
2. 过滤 |涨跌幅| < 5%
3. 返回低波动币种列表

_eliminated_symbols 生成逻辑：
1. scan_and_update() 中 price_change 初筛通过但未进入最终候选池的币种
2. 如果候选池为空，_eliminated_symbols == 所有初筛通过的币种
```

### 2.3 主循环当前逻辑（V2.4）

```
_execute_cycle():
1. 检查候选池是否为空
2. 如果为空且 _is_sleep_on_empty() → 整个策略休眠到下次扫描
3. LV-RM 检查在 _execute_cycle() 内部，候选池为空时不会执行
```

---

## 三、实施阶段与智能体/技能分配

| 阶段 | 内容 | 负责智能体/技能 | 预计工作量 |
|------|------|----------------|-----------|
| **S0** | 输出最终方案文档 | 调度者（你） | 1 个文档 |
| **S1** | 配置参数修改 | 调度者直接执行 | ~10 行 |
| **S2** | candidate_pool.py 核心重构 | `python-engineer` | ~120 行 |
| **S3** | strategy.py 主循环修改 | `python-engineer` | ~50 行 |
| **S4** | 代码规范检测 | `code-specification-inspector` | 1 次检查 |
| **S5** | 功能测试 | `api-test-pro` | 1 轮测试 |
| **S6** | 代码审查与文档对照 | `code-specification-inspector` + `code-document-curator` | 1 轮审查 |
| **S7** | 文档更新 | `code-document-curator` | 更新文档 |
| **S8** | 部署与代码级验证 | `服务器自动化部署` 技能 | 部署到服务器 |

---

## 四、详细任务清单

### S0：输出最终方案文档

- [ ] 输出 HRS V2.5 最终合并版文档
- [ ] 文档路径：`docs/requirements/HRS/混合反转策略（HRS）V2.5（最终版）.md`

### S1：配置参数修改（config.yaml）

**变更清单**：

| 配置路径 | 旧值 | 新值 | 变更类型 |
|---------|------|------|---------|
| `candidate_pool.logic` | 不存在 | `"or_any_2"` | 新增 |
| `candidate_pool.dynamic_thresholds.funding_rate_percentile_short` | 0.80 | 0.50 | 修改值 |
| `candidate_pool.dynamic_thresholds.funding_rate_percentile_long` | 0.20 | 0.50 | 修改值 |
| `candidate_pool.dynamic_thresholds.oi_market_cap_percentile_short` | 0.80 | 0.50 | 修改值 |
| `candidate_pool.dynamic_thresholds.oi_market_cap_percentile_long` | 0.20 | 0.50 | 修改值 |
| `candidate_pool.dynamic_thresholds.ema20_deviation_percentile_short` | 0.70 | 0.50 | 修改值 |
| `candidate_pool.dynamic_thresholds.ema20_deviation_percentile_long` | 0.30 | 0.50 | 修改值 |
| `candidate_pool.long.price_change_24h` | -0.06 | -0.08 | 修改值 |
| `lv_rm.scan.max_price_change_24h` | 0.05 | 0.08 | 修改值 |
| `lv_rm.scan.scan_source` | 不存在 | `"all_liquid"` | 新增 |
| `candidate_pool.empty_backoff` | 不存在 | 新增配置段 | 新增 |

### S2：candidate_pool.py 核心重构

#### S2.1 `_validate_short_candidate()` → `_count_short_conditions()`

**变更**：
- 返回类型从 `bool` 改为 `int`（0-4，表示满足的条件数）
- 不再返回 `bool`，改为返回 `conditions_met` 计数
- 内部逻辑：逐条件判断，计数达标条件

**当前代码**（L633-L710）：
```python
async def _validate_short_candidate(self, symbol: str, ticker: Dict[str, Any]) -> bool:
    # ... 逐条件检查，遇到不满足的返回 False
    # 所有条件满足返回 True
```

**目标代码**：
```python
async def _count_short_conditions(self, symbol: str, ticker: Dict[str, Any]) -> int:
    """
    V2.5: 统计做空候选条件达标数
    返回 0-4，表示 4 个维度中满足的条件数量
    """
    conditions_met = 0
    
    # 条件 A: 涨跌幅
    if self._check_price_change_short(symbol, ticker):
        conditions_met += 1
    
    # 条件 B: 费率
    if self._check_funding_rate_short(symbol):
        conditions_met += 1
    
    # 条件 C: OI/市值
    if self._check_oi_market_cap_short(symbol):
        conditions_met += 1
    
    # 条件 D: EMA20 偏离
    if self._check_ema_deviation_short(symbol):
        conditions_met += 1
    
    return conditions_met
```

#### S2.2 `_validate_long_candidate()` → `_count_long_conditions()`

与 S2.1 对称，改做多方向。

#### S2.3 `scan_and_update()` 重构

**变更**：
- 筛选逻辑从 AND 改为 OR-2
- 不再按涨跌幅初筛后走验证，改为遍历所有流动性币种，对每个币种分别评估 4 个维度
- 条件计数 ≥ 2 则入池

**当前代码**（L498-L627）：
```python
async def scan_and_update(self) -> None:
    # 1. 获取全市场数据
    # 2. 涨跌幅初筛 → short_candidates, long_candidates
    # 3. 对 short_candidates 逐个调用 _validate_short_candidate() → AND 过滤
    # 4. 对 long_candidates 逐个调用 _validate_long_candidate() → AND 过滤
```

**目标代码**：
```python
async def scan_and_update(self) -> None:
    # 1. 获取全市场数据（不变）
    # 2. 计算动态阈值（不变，但值改为 50 分位）
    # 3. 遍历所有流动性币种
    #    for each symbol:
    #       short_count = await self._count_short_conditions(symbol, ticker)
    #       if short_count >= 2:  → 做空候选池
    #       long_count = await self._count_long_conditions(symbol, ticker)
    #       if long_count >= 2:  → 做多候选池
    # 4. 更新候选池
```

#### S2.4 新增 `get_lv_rm_scan_range()` 方法

**当前代码**（L813-L842）：
```python
def get_low_volatility_candidates(self) -> List[str]:
    # 从 _eliminated_symbols 中筛选
    max_price_change = ...  # 0.05
    low_vol_symbols = [s for s in self._eliminated_symbols if ...]
    return low_vol_symbols
```

**目标代码**：
```python
async def get_lv_rm_scan_range(self) -> List[str]:
    """
    V2.5: 获取 LV-RM 独立扫描范围
    从全市场流动性币种中筛选 |涨跌幅| < 8% 的币种
    """
    lv_rm_config = self.config.get("lv_rm", {}).get("scan", {})
    max_price_change = lv_rm_config.get("max_price_change_24h", 0.08)
    
    tickers = await self.market_data.get_all_tickers()
    lv_rm_symbols = []
    
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        if self._should_exclude(symbol):
            continue
        if not self._check_liquidity(ticker):
            continue
        
        # 涨跌幅过滤（从配置读取，禁止硬编码）
        price_change = float(ticker.get("priceChangePercent", 0))
        if abs(price_change) < max_price_change * 100:
            lv_rm_symbols.append(symbol)
    
    return lv_rm_symbols
```

#### S2.5 修改 `get_low_volatility_candidates()` 调用

改为调用 `get_lv_rm_scan_range()` 获取扫描范围。

#### S2.6 新增空池退避逻辑

在 `__init__` 中新增：
- `empty_backoff_config` 配置读取
- `consecutive_empty` 计数
- `_check_empty_backoff()` 方法

### S3：strategy.py 主循环逻辑修改

#### S3.1 修改 `_execute_cycle()` 候选池为空分支

**当前代码**（L664-L667）：
```python
active_symbols = self.candidate_pool.get_active_symbols()
if not active_symbols:
    logger.debug("无活跃候选币种")
    await self._monitor_positions()
    return
```

**目标代码**：
```python
active_symbols = self.candidate_pool.get_active_symbols()
if not active_symbols:
    logger.debug("无活跃候选币种，仅执行 LV-RM 检查")
    await self._run_lv_rm_only_cycle()
    await self._monitor_positions()
    return
```

#### S3.2 修改 `_is_sleep_on_empty()` 

**当前代码**（L543-L564）：
```python
async def _is_sleep_on_empty(self) -> bool:
    # 判断候选池是否为空且需要休眠
    # 返回 True 时整个策略休眠
```

**目标代码**：
```python
async def _is_sleep_on_empty(self) -> bool:
    """
    V2.5: 候选池为空时不再休眠整个策略
    仅影响标准模式/EMM 评分，LV-RM 继续运行
    """
    return False  # 不再因候选池为空而休眠
```

#### S3.3 修改 `_check_lv_rm_entries()` 扫描来源

**当前代码**（L749-L759）：
```python
async def _check_lv_rm_entries(self) -> None:
    low_vol_symbols = self.candidate_pool.get_low_volatility_candidates()
    # ...
```

**目标代码**：
```python
async def _check_lv_rm_entries(self) -> None:
    low_vol_symbols = await self.candidate_pool.get_lv_rm_scan_range()
    # ...
```

#### S3.4 新增 `_run_lv_rm_only_cycle()` 方法

```python
async def _run_lv_rm_only_cycle(self) -> None:
    """
    V2.5: 候选池为空时的轻量级循环
    仅执行 LV-RM 扫描，跳过标准模式/EMM
    """
    logger.info("候选池为空，运行 LV-RM 独立检查")
    await self._check_lv_rm_entries()
```

### S4：scoring_engine.py — 确认无变更

评分引擎的 `score_lv_rm()` 方法（L559-L740）独立于候选池，只接收参数，无需修改。

### S5：文档更新

- `docs/requirements/HRS/混合反转策略（HRS）V2.5（最终版）.md` — 输出最终文档
- 更新 `docs/requirements/HRS/` 目录下的 README 索引

---

## 五、依赖关系与执行顺序

```
S0 (最终文档) → 用户确认
  ↓
S1 (config.yaml 修改) ──────────┐
  ↓                             │
S2 (candidate_pool.py 重构) ────┤
  ↓                             │
S3 (strategy.py 修改) ─────────┤
  ↓                             │
S4 (代码规范检测) ←─────────────┘
  ↓
S5 (功能测试)
  ↓
S6 (代码审查 + 文档对照)
  ↓
S7 (文档更新)
  ↓
S8 (部署 + 代码级验证)
```

**并行可能性**：S1 可以独立执行（只改 config.yaml），S2 和 S3 有依赖关系（S2 完成后才能测试 S3 的调用）。

---

## 六、验证步骤

### 6.1 单元级验证

| 验证点 | 方法 | 预期结果 |
|--------|------|---------|
| OR-2 条件计数 | 模拟 4 个币种，分别满足 0/1/2/3/4 个条件 | 只有 ≥2 条件的入池 |
| 50 分位阈值 | 模拟 10 个币种的分位数计算 | 中位数 = 第 5/6 个值的平均 |
| LV-RM 独立扫描 | 候选池为空时调用 `get_lv_rm_scan_range()` | 返回全市场低波动币种（非空） |
| 空池退避 | 连续空池 3 次 | 第 4 次扫描间隔变为 2 小时 |

### 6.2 集成级验证

| 验证点 | 方法 | 预期结果 |
|--------|------|---------|
| 主循环不因空池休眠 | 候选池为空时运行策略 | 日志显示 `LV-RM 独立检查`，无休眠 |
| 空池时 LV-RM 仍触发 | 候选池为空但 LV-RM 条件满足 | 产生 LV-RM 信号 |
| 候选池非空时正常评分 | 候选池有币种 | 标准模式 + LV-RM 同时运行 |

### 6.3 部署后验证

| 层级 | 验证内容 | 验证方式 |
|------|---------|---------|
| 容器状态 | 容器运行中 | `docker ps` |
| 镜像一致性 | 容器镜像 ID == 最新构建镜像 | `docker inspect` |
| VERSION 文件 | 容器内 DEPLOY_ID == 本地 | `cat /app/VERSION` |
| 关键文件 MD5 | 容器内文件 MD5 == 本地 | `md5sum` 对比（L4 核心验证） |
| 日志无错误 | 容器日志无 error/exception | `docker logs` |

---

## 七、回退方案

| 触发条件 | 回退动作 | 回退目标 |
|----------|----------|----------|
| 上线后 3 天候选池仍 < 3 个 | 分位数从 50 降至 30（做多）/ 70（做空） | 改 config 即可 |
| 上线后 7 天胜率低于 45% | 恢复分位数到 V2.4 值（80/20/70/30） | 改 config 即可 |
| LV-RM 触发 > 3 次/天连续 3 天 | max_price_change_24h 从 8% 降至 5% | 改 config 即可 |
| 单日总开仓 > 5 个（风控熔断） | 紧急暂停新开仓 24 小时 | 手动干预 |

---

## 八、任务清单（Todo）

### S0：输出最终文档
- [ ] 输出 HRS V2.5 最终合并版文档

### S1：配置参数修改
- [ ] 修改 config.yaml 中 9 个分位数/涨跌幅参数值
- [ ] 新增 `candidate_pool.logic` 和 `lv_rm.scan.scan_source` 配置项
- [ ] 新增 `candidate_pool.empty_backoff` 配置段

### S2：candidate_pool.py 核心重构
- [ ] `_validate_short_candidate()` → `_count_short_conditions()`（返回 int）
- [ ] `_validate_long_candidate()` → `_count_long_conditions()`（返回 int）
- [ ] `scan_and_update()` 重构（AND → OR-2）
- [ ] 新增 `get_lv_rm_scan_range()` 方法
- [ ] 修改 `get_low_volatility_candidates()` 调用
- [ ] 新增空池退避逻辑

### S3：strategy.py 主循环修改
- [ ] 修改 `_execute_cycle()` 空候选池分支
- [ ] 修改 `_is_sleep_on_empty()` 返回 False
- [ ] 修改 `_check_lv_rm_entries()` 扫描来源
- [ ] 新增 `_run_lv_rm_only_cycle()` 方法

### S4：代码规范检测
- [ ] 调用 `code-specification-inspector` 检查规范
- [ ] 修复发现的违规问题

### S5：功能测试
- [ ] 调用 `api-test-pro` 执行功能验证
- [ ] 检查候选池筛选逻辑正确性
- [ ] 检查 LV-RM 独立扫描正确性
- [ ] 检查空池退避逻辑

### S6：代码审查与文档对照
- [ ] 调用 `code-specification-inspector` 深度审查
- [ ] 调用 `code-document-curator` 文档对照

### S7：文档更新
- [ ] 调用 `code-document-curator` 更新文档
- [ ] 更新 README 索引

### S8：部署与代码级验证
- [ ] 调用 `服务器自动化部署` 技能了解部署配置
- [ ] 打包部署到服务器
- [ ] 执行五层验证（容器状态 → 镜像 ID → VERSION → MD5 → 日志）
- [ ] 生成部署确认报告