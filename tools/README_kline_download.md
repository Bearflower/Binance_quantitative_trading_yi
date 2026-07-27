# K线数据下载工具使用说明

## 功能概述

本工具用于下载币安永续合约的K线数据，支持以下功能：

- ✅ 从币安API下载K线数据
- ✅ 支持多个时间频率（1h, 15m, 5m）
- ✅ 支持断点续传和增量更新
- ✅ 自动处理API限流
- ✅ 进度条显示
- ✅ 生成下载报告

## 快速开始

### 1. 配置环境变量

确保已设置以下环境变量：

```bash
export BINANCE_API_KEY="your_api_key_here"
export BINANCE_API_SECRET="your_api_secret_here"
```

或者在 `.env` 文件中配置：

```env
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
```

### 2. 准备新币列表

编辑 `data/new_coin_listings.json` 文件，添加需要下载的交易对：

```json
[
  {
    "symbol": "BTCUSDT",
    "listing_time": "2026-01-01 00:00:00",
    "description": "比特币永续合约"
  },
  {
    "symbol": "ETHUSDT",
    "listing_time": "2026-01-01 00:00:00",
    "description": "以太坊永续合约"
  }
]
```

**字段说明**：
- `symbol`: 交易对名称（必须大写）
- `listing_time`: 上线时间（格式：YYYY-MM-DD HH:MM:SS）
- `description`: 描述信息（可选）

### 3. 运行下载脚本

```bash
# 进入项目根目录
cd /Users/yl/vscode/Binance_quantitative_trading

# 运行脚本
python tools/download_new_coin_klines.py
```

## 数据格式

### CSV文件格式

下载的K线数据保存在 `data/klines/` 目录下，文件命名格式：`{symbol}_{interval}.csv`

例如：`BTCUSDT_1h.csv`

**CSV字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| open_time | int | 开盘时间戳（毫秒） |
| open | decimal | 开盘价 |
| high | decimal | 最高价 |
| low | decimal | 最低价 |
| close | decimal | 收盘价 |
| volume | decimal | 成交量 |
| quote_volume | decimal | 成交额 |
| trades | int | 成交笔数 |
| taker_buy_volume | decimal | 主动买入成交量 |

### 示例数据

```csv
open_time,open,high,low,close,volume,quote_volume,trades,taker_buy_volume
1704067200000,42000.00,42500.00,41800.00,42300.00,1234.56,52000000.00,5678,600.00
1704070800000,42300.00,42800.00,42100.00,42600.00,1456.78,62000000.00,6789,700.00
```

## 功能详解

### 1. 断点续传

脚本会自动检查已下载的数据，只下载新的K线数据：

- 首次运行：从上线时间开始下载到当前时间
- 后续运行：从最后一条数据的时间开始下载到当前时间

### 2. 增量更新

每次运行脚本时，会自动：

1. 检查已下载的数据
2. 计算需要下载的新数据
3. 合并新旧数据
4. 去重并排序
5. 保存到文件

### 3. API限流处理

脚本内置了以下机制来避免触发API限流：

- **频率控制**：每分钟最多1200次请求
- **批量下载**：每次最多1500根K线
- **延迟等待**：批次间自动添加延迟

### 4. 进度显示

下载过程中会显示进度条：

```
BTCUSDT_1h: 100%|██████████| 1500/1500 [00:15<00:00, 100.00根/s]
```

### 5. 下载报告

每次运行结束后，会在 `data/klines/` 目录下生成下载报告：

```
download_report_20260509_143025.txt
```

报告内容包括：
- 总交易对数
- 成功/失败数量
- 总K线数量
- 失败的交易对列表

## 高级用法

### 修改时间频率

编辑 `tools/download_new_coin_klines.py` 文件中的 `SUPPORTED_INTERVALS`：

```python
SUPPORTED_INTERVALS = ['1h', '15m', '5m', '1d', '4h']
```

### 自定义数据目录

修改脚本中的参数：

```python
async with KlineDownloader(
    api_key=api_key,
    api_secret=api_secret,
    data_dir="/path/to/your/data",
    listing_file="/path/to/your/listing.json"
) as downloader:
    await downloader.download_all(intervals=['1h'])
```

### 单独下载某个交易对

```python
result = await downloader.download_symbol_klines(
    symbol="BTCUSDT",
    listing_time=datetime(2026, 1, 1, 0, 0, 0),
    intervals=['1h', '15m']
)
```

## 常见问题

### Q1: 提示"请设置环境变量"

**A**: 确保已正确设置 `BINANCE_API_KEY` 和 `BINANCE_API_SECRET` 环境变量。

### Q2: 下载速度很慢

**A**: 这是正常的，因为：
- 币安API有频率限制（每分钟1200次）
- 脚本会自动添加延迟避免触发限流
- 历史数据量大时需要分批下载

### Q3: 某个交易对下载失败

**A**: 可能的原因：
- 交易对不存在或已下线
- 上线时间设置错误
- 网络问题

解决方法：
1. 检查交易对名称是否正确
2. 检查上线时间是否合理
3. 查看日志文件了解详细错误信息

### Q4: 如何验证数据完整性

**A**: 可以通过以下方式验证：

1. 检查CSV文件行数
2. 检查时间戳是否连续
3. 对比币安官网数据

## 注意事项

1. **API密钥安全**：不要将API密钥提交到版本控制系统
2. **数据备份**：定期备份下载的数据
3. **存储空间**：确保有足够的磁盘空间存储数据
4. **网络稳定**：下载过程中保持网络连接稳定

## 相关文档

- [新币做空策略 V4.0 完整版](../docs/requirements/new_coin/新币做空策略 V4.0 完整版.md)
- [通用模块调用指南](../.trae/skills/通用模块调用指南)
- [项目README](../README.md)

## 更新日志

### v1.0.0 (2026-05-09)
- ✅ 初始版本发布
- ✅ 支持多时间频率下载
- ✅ 支持断点续传
- ✅ 支持增量更新
- ✅ 自动生成下载报告

---

**维护者**: 资深Python工程师
**创建时间**: 2026-05-09
**最后更新**: 2026-05-09
