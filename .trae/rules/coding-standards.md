# 编码标准与规范

## 语言与注释

- 用中文思考和回答。代码注释、日志输出也一律用中文。
- 对外暴露的 API 名称、变量名可使用英文，但注释必须用中文。

## 项目文档引用

- 项目需求：根目录 [README.md](../../README.md)（精简版），完整需求见 `docs/plans/项目需求迭代文档.md`
- 技术方案：`docs/` 目录下的文档，通过 `docs/README.md` 索引查询
- 文档存储：需求类、报告类、部署类、方案类、设计类的文档统一存放在 `docs/` 目录

## 禁止硬编码（强制）🚫

所有业务逻辑参数、评分、阈值、延迟时间等必须通过配置文件或算法计算得出，严禁在代码中直接写死固定值。

```python
# ❌ 禁止：硬编码阈值
if score > 0.8:
    place_order()

# ✅ 正确：从配置读取
if score > config.get("trade.score_threshold"):
    place_order()
```

代码检测员必须检查此项。

## 禁止服务器回测（强制）🚫

严禁在服务器上直接运行回测。回测必须在本机执行，数据来源：

1. 在服务器端筛选目标数据后，下载到本地进行回测
2. 通过本地代码从服务器获取数据后，在本地进行回测

```bash
# ❌ 禁止：SSH 到服务器执行回测脚本
ssh root@server "python backtest/run.py"

# ✅ 正确：从服务器拉取数据到本地，再本地回测
scp root@server:/data/btc_klines.csv ./data/
python backtest/run.py --data ./data/btc_klines.csv
```

原因：回测消耗大量 CPU/内存，会与生产交易程序抢占资源，可能导致交易延迟或服务中断。

## 代码质量一致性（强制）🚫

### 禁止重复代码

**严禁复制粘贴代码。** 相同或高度相似的逻辑必须提取为公共函数/模块，不得出现两段以上逻辑相同的代码。

```python
# ❌ 禁止：重复代码
def calculate_sma_5m(prices):
    return sum(prices[-5:]) / 5

def calculate_sma_15m(prices):
    return sum(prices[-15:]) / 15

# ✅ 正确：提取通用函数
def calculate_sma(prices, period):
    return sum(prices[-period:]) / period
```

检测标准：
- 连续 5 行以上相同的代码块视为重复代码
- 逻辑结构相同、仅变量名不同的代码视为重复代码
- 代码检测员必须检查此项，发现重复代码需标记为"违规"并退回重构

### 禁止幽灵参数

**每个已定义的参数必须被使用。** 存在已定义但未使用的参数（幽灵参数）会误导后续维护者，增加理解成本。

```python
# ❌ 禁止：幽灵参数
def calculate_score(data, debug=False, threshold=0.5):  # debug 从未使用
    return data * threshold

# ✅ 正确：删除未使用参数，或用 _ 前缀标记
def calculate_score(data, threshold=0.5):
    return data * threshold

# ✅ 或：用 _ 前缀明确表示"本函数未使用，为兼容接口保留"
def calculate_score(data, threshold=0.5, _debug=None):
    return data * threshold
```

检测标准：
- 函数定义中所有参数必须在函数体内被引用至少一次
- 回调函数、接口兼容场景可用 `_` 前缀标记未使用参数
- 代码检测员必须检查此项

### 代码可读性

- 单个函数不超过 50 行（超过需拆分为子函数）
- 单行不超过 120 个字符
- 同一文件内代码风格保持一致（命名风格、缩进、空行）

## 框架与依赖

- 优先使用项目已有依赖，新增依赖需在 `requirements.txt` 或 `pyproject.toml` 中声明
- 禁止使用已废弃的 API 或库版本

---

**最后更新：** 2026-06-01