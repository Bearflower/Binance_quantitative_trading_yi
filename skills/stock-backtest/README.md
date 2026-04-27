# Stock-Backtest 技能文档

## 📁 技能位置

```
.trae/skills/stock-backtest/SKILL.md
```

## 🎯 技能概述

这是一个**完整的股票数据导出和本地回测技能**，专门用于从服务器 PostgreSQL 数据库导出真实 K 线数据到本地，并使用形态检测策略进行回测。

## ✨ 核心特性

### 1. SSH 远程数据导出
- ✅ 通过 SSH 连接到远程服务器
- ✅ 在 Docker 容器内执行 Python 脚本
- ✅ Base64 编码避免 SSH 命令转义问题
- ✅ 从 PostgreSQL 数据库查询 K 线数据
- ✅ 自动保存到本地 CSV 文件

### 2. 形态检测回测
- ✅ 基于"大跌→缩量→放量→回踩"四步检测
- ✅ 形态评分系统（0-100 分）
- ✅ 支持多日期检测
- ✅ 详细的指标输出

### 3. 完整的分析报告
- ✅ 走势分析（涨跌幅、成交量等）
- ✅ 形态检测结果
- ✅ 投资建议
- ✅ Markdown 格式报告

## 🚀 使用方式

### 基础使用

```bash
# 1. 从服务器导出数据
python3 export_from_server_base64.py

# 2. 运行形态检测
python3 check_stock_from_exported.py

# 3. 生成检测报告
python3 generate_report.py
```

### 自定义股票代码

```python
# 修改 export_from_server_base64.py 中的代码
code = '600519'  # 改为贵州茅台

# 或者在命令行指定
python3 export_from_server_base64.py --code 600519
```

### 批量检测

```python
stocks = ['603529', '600519', '000858', '002415']

for code in stocks:
    # 导出
    df = export_via_base64(code=code, days=300)
    
    # 检测
    results = check_pattern(code=code)
    
    # 生成报告
    generate_report(code=code, match_results=results)
```

## 📊 输出文件

### 数据文件
- **位置**: `data/backtest/{code}_server_data.csv`
- **格式**: CSV
- **列**: date, open, high, low, close, volume, amount

### 报告文件
- **位置**: `{code}_检测报告.md`
- **格式**: Markdown
- **内容**: 检测概要、走势分析、形态检测结果

## 🔧 配置说明

### 服务器配置

编辑 `export_from_server_base64.py`:

```python
server_ip = "43.156.242.184"      # 服务器 IP
server_user = "root"              # SSH 用户
ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem" # SSH 密钥路径

# 数据库配置
os.environ['DB_HOST'] = '10.3.0.12'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'stockfilter'
os.environ['DB_USER'] = 'stockfilter_user'
os.environ['DB_PASSWORD'] = 'Stock@2024'
```

### 形态参数

编辑 `config.yaml`:

```yaml
pattern:
  drop_threshold: 0.15        # 跌幅阈值
  volume_shrink_ratio: 0.6    # 缩量阈值
  surge_volume_ratio: 1.5     # 放量倍数
  surge_price_ratio: 0.05     # 放量涨幅
  retrace_ratio: 0.5          # 回踩幅度
```

## 🎯 适用场景

1. **从服务器数据库导出股票数据**
   - 有 PostgreSQL 数据库存储 K 线数据
   - 需要通过 SSH 远程访问
   - 数据在 Docker 容器内

2. **本地回测**
   - 使用真实历史数据
   - 形态检测策略验证
   - 参数优化

3. **批量分析**
   - 多只股票批量检测
   - 生成统一报告
   - 对比分析

## 📝 技术亮点

### 1. Base64 编码传输
```python
# 避免 SSH 命令转义问题
encoded_script = base64.b64encode(python_script.encode()).decode()
docker_cmd = f"echo {encoded_script} | base64 -d | docker exec -i stockfilter-app python3 -"
```

### 2. 自动化数据解析
```python
# 自动解析 CSV 格式数据
lines = result.stdout.strip().split('\n')
data = [parse_line(line) for line in lines]
df = pd.DataFrame(data)
```

### 3. 智能形态检测
```python
# 四步检测流程
is_match, detail = detector.check_pattern(df_check)
if is_match:
    score = scorer.score(detail, pattern_params)
```

## ⚠️ 注意事项

1. **SSH 密钥权限**
   ```bash
   chmod 600 /Users/yl/vscode/inspection_automation/docs/only.pem
   ```

2. **Docker 容器状态**
   ```bash
   # 在服务器上检查容器状态
   docker ps | grep stockfilter-app
   ```

3. **数据库连接**
   - 确保数据库用户有查询权限
   - 检查数据库网络连通性

4. **数据质量**
   - 检查数据完整性
   - 验证日期范围
   - 确认数据量充足（至少 200 条）

## 🔄 与其他技能的区别

| 技能 | 数据源 | 回测方式 | 适用场景 |
|------|--------|---------|---------|
| **stock-backtest** | PostgreSQL 服务器 | 本地 CSV 回测 | A 股形态检测 |
| 币安回测 | 币安 API | JSON 数据 | 加密货币合约 |

## 📚 相关资源

- [回测系统完整文档](../backtest/README.md)
- [形态检测策略说明](../股票形态筛选系统.md)
- [603529 检测案例](../603529_真实数据检测报告.md)

## 🎉 总结

这个技能将我们开发的完整回测系统封装成了一个可复用的模块，具有以下优势：

✅ **完整流程**: 从数据导出到形态检测一站式解决  
✅ **真实数据**: 直接从服务器 PostgreSQL 数据库获取真实 K 线  
✅ **易于使用**: 简单的命令行操作，无需复杂配置  
✅ **高度可配置**: 支持自定义参数、股票代码、时间范围  
✅ **详细报告**: 自动生成 Markdown 格式检测报告  
✅ **批量支持**: 可批量检测多只股票  

**现在你可以在任何需要的时候使用这个技能进行股票回测！** 🚀
