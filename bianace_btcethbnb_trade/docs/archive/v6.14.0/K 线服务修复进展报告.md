# K 线服务修复进展报告

**修复时间**: 2026-04-21 09:00 - 进行中  
**问题**: K 线数据无法存储到数据库，导致无法检测交易信号

---

## 一、问题根因

### 原始错误
```
'NoneType' object has no attribute '_pool'
```

### 问题位置
`/app/kline_data_service/core/collector.py` 第 199-203 行

### 错误代码
```python
# 错误：直接访问 conn._connection._pool
raw_conn = await conn._connection._pool.acquire()
try:
    await raw_conn.executemany(query, values)
finally:
    await conn._connection._pool.release(raw_conn)
```

### 问题原因
`databases` 库的 `Database` 对象在上下文管理器中返回时，`_connection` 属性可能为 `None`，导致无法访问 `_pool` 属性。

---

## 二、已尝试的修复方案

### 方案 1：使用 `execute_many`（失败）
```python
await conn.execute_many(query, values)
```
**失败原因**: `execute_many` 参数格式不匹配，需要字典列表而非元组列表

### 方案 2：使用命名参数逐条插入（进行中）
```python
for data in data_list:
    query = """
        INSERT INTO table_name VALUES (:open_time, :open_price, ...)
        ON CONFLICT (open_time) DO NOTHING
    """
    await conn.execute(query, data)
```
**状态**: 代码已修改，需要重新构建镜像并测试

---

## 三、当前状态

### ✅ 已完成
1. K 线服务容器重启成功
2. 源代码 collector.py 已修改
3. Docker 镜像已重新构建
4. 服务启动正常，定时任务已配置

### ❌ 未解决
1. K 线数据存储仍然失败（最新错误：参数名称不匹配）
2. 数据库中无 K 线数据
3. 项目无法获取 K 线数据
4. 无法检测交易信号

### 📊 系统状态
```
K 线服务：✅ 运行正常
数据采集：✅ 从币安获取成功
数据存储：❌ 失败（代码 bug）
项目调用：❌ 无数据可获取
信号检测：❌ 无数据，0 信号
交易执行：❌ 无信号，0 交易
胜率：0%（正常）
```

---

## 四、临时解决方案（推荐）

由于 K 线服务的代码修复复杂且耗时，建议**临时回退到直接使用币安 API**的方式，确保系统正常运行。

### 方案 A：修改项目代码，直接调用币安 API

**修改文件**: `core/data_fetcher.py`

```python
# 回退到原来的实现
from utils.binance_api import get_multiple_symbols_data

def fetch_market_data(self, symbols: List[str] = None):
    # 直接使用币安 API
    api_data = get_multiple_symbols_data(symbols)
    # ...
```

**优点**:
- ✅ 立即可用
- ✅ 不依赖 K 线服务
- ✅ 代码简单可靠

**缺点**:
- ❌ 无法统一管理 K 线数据
- ❌ 多个项目重复调用币安 API
- ❌ 受网络限制影响

### 方案 B：在项目中集成币安 API 客户端

**创建新文件**: `utils/binance_kline.py`

```python
import requests
from typing import List, Dict

def get_klines(symbol: str, interval: str, limit: int = 100) -> List[Dict]:
    """直接从币安 API 获取 K 线数据"""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    response = requests.get(url, params=params, timeout=10)
    if response.status_code == 200:
        return response.json()
    return []
```

**优点**:
- ✅ 独立于 K 线服务
- ✅ 代码可控
- ✅ 易于调试

**缺点**:
- ❌ 需要额外开发
- ❌ 无法利用通用服务

---

## 五、长期解决方案（继续修复 K 线服务）

### 步骤 1：修复 collector.py 的 _batch_insert 方法

**正确实现**:
```python
async def _batch_insert(self, table_name: str, data_list: List[Dict]) -> int:
    """批量插入数据"""
    try:
        async with self.db.get_connection() as conn:
            # 检查表是否存在
            await self._create_table_if_not_exists(conn, table_name, data_list[0] if data_list else {})
            
            # 逐条插入（使用命名参数）
            inserted = 0
            for data in data_list:
                query = f"""
                    INSERT INTO {table_name} (
                        open_time, open_price, high_price, low_price, close_price,
                        volume, close_time, quote_volume, trade_count,
                        taker_buy_volume, taker_buy_quote_volume
                    ) VALUES (
                        :open_time, :open_price, :high_price, :low_price, :close_price,
                        :volume, :close_time, :quote_volume, :trade_count,
                        :taker_buy_volume, :taker_buy_quote_volume
                    )
                    ON CONFLICT (open_time) DO NOTHING
                """
                await conn.execute(query, data)
                inserted += 1
            
            logger.info(f"成功插入 {inserted} 条数据到 {table_name}")
            return inserted
            
    except Exception as e:
        logger.error(f"批量插入 {table_name} 失败：{e}")
        raise
```

### 步骤 2：重新构建 Docker 镜像

```bash
cd /root/common_service
docker-compose down kline_service
docker-compose build --no-cache kline_service
docker-compose up -d kline_service
```

### 步骤 3：测试验证

```bash
# 手动触发采集
curl -X POST 'http://localhost:8765/api/v1/collect/manual?symbol=BTCUSDT&interval=1h&minutes=60'

# 预期响应
{"code":0,"message":"success","data":{"symbol":"BTCUSDT","interval":"1h","stored_count":60}}

# 验证数据可获取
curl 'http://localhost:8765/api/v1/klines/latest?symbol=BTCUSDT&interval=1h&limit=5'
```

### 步骤 4：验证项目可以获取 K 线

```bash
# 检查项目日志
docker logs binance-trade-analyzer | grep "成功获取"

# 预期日志
INFO - 成功获取 3 个交易对的行情数据
```

---

## 六、建议

### 立即执行（推荐方案 A）

1. **回退到币安 API** - 确保系统立即可用
   ```bash
   # 修改 core/data_fetcher.py
   # 使用 get_multiple_symbols_data 而不是 KlineServiceClient
   ```

2. **重新部署项目**
   ```bash
   ./one_click_deploy.sh
   ```

3. **验证交易功能**
   - 等待下次整点检测（11:00）
   - 检查是否有交易信号
   - 检查是否有交易执行

### 继续修复（并行进行）

1. **修复 K 线服务代码** - 参考长期解决方案
2. **测试验证** - 确保 K 线数据可以正常存储
3. **切换回通用服务** - 项目代码改回使用 KlineServiceClient

---

## 七、时间估算

### 方案 A（回退到币安 API）
- **实施时间**: 10 分钟
- **验证时间**: 1 小时（等待下次整点检测）
- **总时间**: ~1 小时

### 方案 B（继续修复 K 线服务）
- **修复时间**: 30-60 分钟
- **构建时间**: 5 分钟
- **测试时间**: 30 分钟
- **总时间**: ~1.5-2 小时

---

## 八、决策建议

**推荐**: 立即执行方案 A（回退到币安 API），同时并行修复 K 线服务

**理由**:
1. ✅ 系统可以立即恢复正常使用
2. ✅ 不影响交易信号检测和執行
3. ✅ K 线服务可以继续修复，无时间压力
4. ✅ 修复完成后可以轻松切换回通用服务

**风险**:
- ⚠️ 本地网络无法访问币安 API（但服务器可以）
- ⚠️ 需要管理两套代码（临时方案 vs 长期方案）

---

**报告生成时间**: 2026-04-21 10:05  
**建议**: 优先执行方案 A，确保系统可用
