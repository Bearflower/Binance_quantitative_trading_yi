# 部署确认报告

---
## 2026-09-15 追加部署（激活移动止损：止盈成交检测）

### 变更内容
- **修复移动止损从未生效的 bug**：原代码中移动止损已存在（MTPCS/HRS 同源）但 `target2_reached` 从未被标记，导致移动止损永不激活，实际退化为纯固定止盈
- 新增「止盈成交检测」机制：通过对比交易所实际持仓数量与上次跟踪数量，检测 TP1/TP2 条件单成交（100%→70% 标记 target1_reached；70%→30% 标记 target2_reached 并激活移动止损；→0 全部平仓）
- 激活后启用已有双机制移动止损：回撤阶梯动态止损（shared/dynamic_trailing.py，MTPCS 同源）+ 最低价反弹 1.5×ATR（HRS 风格），锁住更多利润
- 新增 `trading.position_detection` 配置块（enabled / qty_tolerance_ratio / qty_tolerance_absolute / zero_qty_threshold）
- 改动文件：`strategies/new_coin/executor.py`（新增 `_get_exchange_position_qty`/`detect_take_profit_fills`/`clear_position_tracking`）、`strategies/new_coin/strategy.py`、`strategies/new_coin/config.yaml`

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | Up (healthy) | ✅ |
| 2 镜像ID | 容器 fc6ea8 == 本次构建 `trading_system-new-coin-strategy:latest` | ✅ |
| 4 文件MD5 | 三方一致（本地=服务器=容器内） | ✅ |
| 5 日志错误 | 启动后 200 行无 error/exception | ✅ |

### MD5（本地=服务器=容器内）
| 文件 | MD5 |
|------|-----|
| executor.py | d7b4527f099fd909ca79cc6221183d25 |
| strategy.py | 8a629c71fe160f4c7d11a573fd3bfd91 |
| config.yaml | e1bc92708defc0359e9f7f023460dd04 |

### 测试
- ✅ 新增 `tests/test_strategies/test_take_profit_fill_detect.py`（14 用例）
- ✅ 全量回归 272 passed
- ✅ 部署方式：按需重建（仅 new-coin-strategy，未触碰其他容器），`--no-cache` 防部署幻觉
- 部署 ID: `9D2DB354`

---

## 基本信息
- 部署时间: 2026-09-03 12:53 (Asia/Shanghai)
- 目标服务器: 43.156.242.184
- 项目名称: trading_system
- 容器名称: trading_system-new_coin
- 部署范围: 仅 new_coin 容器（新增总持仓保证金上限机制）

## 版本信息
- 部署方式: 定向部署（仅 new_coin 服务，未重建其他容器）
- 变更: total_position_margin_limit 配置 + executor 5.5.1 检查 + capital_manager.get_total_margin_limit

## 变更内容
- strategies/new_coin/config.yaml: trading 段新增 `total_position_margin_limit: 150`（总持仓保证金上限）
- strategies/new_coin/executor.py: 开仓前新增 5.5.1 总持仓保证金检查（保证金=仓位价值/杠杆，超限跳过开仓），与 capital_limits 并存取更严格
- shared/capital_manager.py: 新增 `get_total_margin_limit()`，每次调用动态读取配置文件 trading.total_position_margin_limit，禁止硬编码

## 验证结果

### 第一层：容器运行状态
- ✅ Up (healthy)

### 第二层：镜像与容器
- ✅ new_coin 镜像以 --no-cache 重建，容器已 Recreate 并启动

### 第三层/第四层：容器内代码 MD5 对比
| 文件 | 本地 MD5 | 容器内 MD5 | 结果 |
|------|----------|-----------|------|
| strategies/new_coin/config.yaml | 58536e18df85a3beea296a04da847dd6 | 58536e18df85a3beea296a04da847dd6 | ✅ |
| strategies/new_coin/executor.py | da6ec439a7a8b38d6ba71faf36d19685 | da6ec439a7a8b38d6ba71faf36d19685 | ✅ |
| shared/capital_manager.py | 2df681ad776b951d549cff312bad7930 | 2df681ad776b951d549cff312bad7930 | ✅ |

### 第五层：功能验证
- ✅ 容器内配置确认：`total_position_margin_limit: 150` 存在
- ✅ 策略初始化无 error/exception/traceback，交易执行器初始化正常（leverage=2, max_positions=3, single_position_margin=50）

## 最终结论
✅ **部署成功！new_coin 容器已运行本次新代码，代码级验证通过。**

---
## 2026-09-07 追加部署（时间止损前置复核）

### 变更内容
- 新增 `trading.time_stop_review` 多因素复核机制：持仓满72h未达第一目标时，先综合评分（趋势/反转形态/量能/情绪）判断空头逻辑是否仍成立，成立则继续持有，否则止损100%
- 增加豁免规则：距第一目标跌幅≥70% 豁免时间止损，继续持有到目标
- 数据异常时按 `bias_hold`（默认偏向继续持有）决策
- 改动文件：`strategies/new_coin/executor.py`、`strategies/new_coin/config.yaml`

### 验证结果
- ✅ 本地/服务器 MD5 一致（executor `dc52a62b…`、config `eed98441…`）
- ✅ 容器内 `_time_stop_review`/`_compute_review_score` 存在，`time_stop_review.enabled=true`
- ✅ 容器 Up (healthy)，启动无 error/exception
- ✅ 75 项测试通过（含 12 项新增复核专项测试）

---
## 2026-09-04 追加部署（移除持仓数量上限）

### 变更内容
- 移除 new_coin「持仓数量上限」机制（原 `trading.max_positions: 3`），改由**总持仓保证金上限（月度分配 monthly_limit 动态约束）**控制持仓规模
- 改动文件：`strategies/new_coin/executor.py`、`strategies/new_coin/strategy.py`、`strategies/new_coin/config.yaml`

### 验证结果
- ✅ 本地/服务器 MD5 一致（executor `7008423f…`、strategy `378068ac…`、config `b1f802aa…`）
- ✅ 容器内实际代码无 max_positions 残留（仅历史 .bak 备份含旧内容）
- ✅ 容器 Up (healthy)，启动无 error/exception
- ✅ 26 项测试通过

---
## 2026-09-03 追加部署（动态月度来源）

### 变更内容
- shared/capital_manager.py: `get_total_margin_limit()` 改为**优先读取 `capital_limits.monthly_limit`（月度资金分配金额，每月由 AI 动态更新），未配置时回退 `trading.total_position_margin_limit`（150）**

### 验证结果
- ✅ 容器内 MD5 `91fdac2b24fa705226eedb9690d2d27e` 与本地一致
- ✅ 功能验证：未配置 monthly 时返回 150 回退值
- ✅ 容器 Up (healthy)，启动无 error/exception