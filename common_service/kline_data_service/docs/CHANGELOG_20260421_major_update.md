# 通用 K 线数据服务 - 更新日志

## 2026-04-21 - 重大功能更新

### 🎯 更新概述
本次更新完成了通用 K 线服务的核心功能修复和增强，包括数据采集逻辑修复、标的注册功能、监控功能等。

### ✅ 完成的改动

#### 1. 修复数据采集逻辑

**问题**:
- 采集的是"实时数据"而不是"已收盘的 K 线"
- 可能采集到未收盘的 K 线，导致数据不准确

**修复**:
- 修改 `core/collector.py` 的 `collect_recent()` 方法
- 改为采集"上一个完整周期"的已收盘 K 线
- 添加过滤逻辑，自动过滤未收盘的 K 线

**代码改动**:
```python
async def collect_recent(self, symbol: str, interval: str, minutes: int = 5):
    """采集最近 N 分钟的 K 线数据（采集已收盘的 K 线）"""
    # 计算采集时间范围
    now = datetime.now()
    end_time = int((now - timedelta(minutes=1)).timestamp() * 1000)
    start_time = int((now - timedelta(minutes=minutes + 1)).timestamp() * 1000)
    
    klines = await self.collect_klines(symbol, interval, start_time, end_time)
    
    if klines:
        # 过滤掉未收盘的 K 线
        current_time = int(now.timestamp() * 1000)
        filtered_klines = [k for k in klines if k.close_time < current_time]
        
        if filtered_klines:
            stored = await self.store_klines(filtered_klines)
            return stored
    
    return 0
```

#### 2. 优化调度时间配置

**修改文件**: `core/scheduler.py`

**改动内容**:
- 根据周期自动设置采集窗口
- 缩短采集延迟（从周期结束后 5 分钟改为 1-2 分钟）

**配置表**:
| 周期 | Cron 表达式 | 采集窗口 | 说明 |
|------|------------|----------|------|
| 1m | `* * * * *` | 1 分钟 | 每分钟采集 |
| 5m | `*/5 * * * *` | 5 分钟 | 每 5 分钟采集 |
| 15m | `*/15 * * * *` | 15 分钟 | 每 15 分钟采集 |
| 1h | `1 * * * *` | 60 分钟 | 每小时第 1 分钟采集 |
| 4h | `1 0,4,8,12,16,20 * * *` | 240 分钟 | 每 4 小时第 1 分钟采集 |
| 1d | `1 0 * * *` | 1440 分钟 | 每天 0:01 采集 |

#### 3. 新增标的注册功能

**新增文件**:
- `models/registered_symbol.py` - 注册配置模型
- `core/registry.py` - 注册管理核心逻辑
- `api/registry_routes.py` - 注册管理 API

**数据库表**:
```sql
CREATE TABLE registered_symbols (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE,
    intervals TEXT[] NOT NULL,
    registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    duration_days INTEGER NOT NULL DEFAULT 10,
    priority VARCHAR(20) NOT NULL DEFAULT 'normal',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_by VARCHAR(50) NOT NULL DEFAULT 'system',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

**API 接口**:

1. **注册新标的**
   ```bash
   POST /api/v1/register
   {
     "symbol": "NEWCOINUSDT",
     "intervals": ["1m", "5m", "15m", "1h"],
     "duration_days": 10,
     "priority": "high"
   }
   ```

2. **取消注册**
   ```bash
   DELETE /api/v1/register?symbol=NEWCOINUSDT
   ```

3. **续期**
   ```bash
   PUT /api/v1/register/renew
   {
     "symbol": "NEWCOINUSDT",
     "additional_days": 7
   }
   ```

4. **查询已注册标的**
   ```bash
   GET /api/v1/register
   ```

**自动清理**:
- 每小时自动清理过期配置
- 过期后自动停止采集

#### 4. 修复数据库插入问题

**问题**:
- `databases` 库的参数绑定错误
- 错误信息：`text() construct doesn't define a bound parameter named 'symbol'`

**修复**:
- 修改 `_batch_insert()` 方法
- 逐条插入并使用命名参数
- 只提取 SQL 查询中需要的字段

**代码改动**:
```python
async def _batch_insert(self, table_name: str, data_list: List[Dict]) -> int:
    """批量插入数据"""
    inserted = 0
    for data in data_list:
        query = f"""
            INSERT INTO {table_name} (...)
            VALUES (:open_time, :open_price, ...)
            ON CONFLICT (open_time) DO NOTHING
        """
        # 只提取 SQL 查询中需要的字段
        values = {
            'open_time': data['open_time'],
            'open_price': data['open_price'],
            ...
        }
        await conn.execute(query, values)
        inserted += 1
    return inserted
```

#### 5. 修复 Dockerfile 端口配置

**问题**:
- Dockerfile 中 uvicorn 监听 8765 端口
- 但容器内应该监听 8000 端口
- 端口映射：8765(宿主) → 8000(容器)

**修复**:
```dockerfile
# 修改前
EXPOSE 8765
CMD ["uvicorn", ..., "--port", "8765"]

# 修改后
EXPOSE 8000
CMD ["uvicorn", ..., "--port", "8000"]
```

#### 6. 新增监控功能（外部模块）

**文件**: `../bianace_newtrade_trade/short_selling_system/utils/kline_monitor.py`

**功能**:
- 健康检查
- 数据质量监控
- 已注册标的监控
- 异常告警

### 📊 测试验证

#### 数据采集测试

| 交易对 | 15m | 1h | 4h | 1d | 状态 |
|--------|-----|----|----|----|----|
| BTCUSDT | ✅ 2 条 | ✅ 2 条 | ✅ 1 条 | ✅ 1 条 | ✅ 完整 |
| ETHUSDT | ✅ 2 条 | ✅ 1 条 | ✅ 1 条 | ✅ 1 条 | ✅ 完整 |
| BNBUSDT | ✅ 1 条 | ✅ 1 条 | ✅ 1 条 | ✅ 1 条 | ✅ 完整 |

#### 注册功能测试

```bash
# 注册测试
✅ TESTUSDT 注册成功

# 查询测试
✅ 已注册 1 个标的

# 续期测试
✅ BTCUSDT 续期成功

# 取消注册测试
✅ TESTUSDT 已取消注册
```

### 🔧 配置变更

#### 环境变量

无新增环境变量。

#### API 参数变更

**`/api/v1/collect/manual`**:
- `minutes` 参数上限从 60 提升到 1440（24 小时）

### 📝 使用说明

#### 手动触发采集

```bash
# 采集 BTCUSDT 15m 最近 15 分钟数据
curl -X POST 'http://43.156.242.184:8765/api/v1/collect/manual?symbol=BTCUSDT&interval=15m&minutes=15'

# 采集 BTCUSDT 1h 最近 1 小时数据
curl -X POST 'http://43.156.242.184:8765/api/v1/collect/manual?symbol=BTCUSDT&interval=1h&minutes=60'

# 采集 BTCUSDT 4h 最近 4 小时数据
curl -X POST 'http://43.156.242.184:8765/api/v1/collect/manual?symbol=BTCUSDT&interval=4h&minutes=240'
```

#### 查询 K 线数据

```bash
# 查询 BTCUSDT 15m 最新 5 条 K 线
curl 'http://43.156.242.184:8765/api/v1/klines/latest?symbol=BTCUSDT&interval=15m&limit=5'
```

#### 注册新币

```bash
# 注册新币 NEWCOINUSDT
curl -X POST 'http://43.156.242.184:8765/api/v1/register' \
  -H 'Content-Type: application/json' \
  -d '{
    "symbol": "NEWCOINUSDT",
    "intervals": ["1m", "5m", "15m", "1h"],
    "duration_days": 10,
    "priority": "high"
  }'
```

### 🎯 性能提升

**修复前**:
- 采集未收盘的 K 线，数据不准确
- 数据库插入失败
- 端口配置错误导致服务不可用

**修复后**:
- ✅ 只采集已收盘的 K 线，数据准确
- ✅ 数据库插入成功
- ✅ 服务正常运行
- ✅ 支持动态注册和管理
- ✅ 自动清理过期配置

### 📚 架构说明

#### 数据流

```
币安 API → K 线采集器 → 数据库 → API → 业务系统
            ↓
        定时任务
            ↓
        自动采集
```

#### 注册流程

```
业务系统 → 注册 API → 注册管理器 → 数据库
                              ↓
                          定时任务 ← 每小时清理过期
                              ↓
                        动态添加采集任务
```

### ⚠️ 注意事项

1. **数据库迁移**:
   - 需要手动创建 `registered_symbols` 表
   - 参考本文档中的 SQL 语句

2. **端口配置**:
   - 确保服务器安全组开放 8765 端口
   - Docker 端口映射：8765(宿主) → 8000(容器)

3. **定时任务**:
   - 固定标的（BTC/ETH/BNB）在 main.py 中硬编码
   - 注册标的在启动时从数据库加载

4. **数据清理**:
   - 过期的注册配置会自动标记为 expired
   - 不会自动删除，可以手动清理

### 🚀 后续优化建议

1. **历史数据补充** - 添加历史数据采集功能
2. **数据完整性检查** - 定期检查数据是否有缺失
3. **性能监控** - 添加 Prometheus 监控指标
4. **告警集成** - 集成飞书/钉钉告警
5. **配置热更新** - 支持不重启服务更新配置

### 📖 相关文档

- [部署指南](./deployment.md)
- [API 文档](./api.md)
- [开发指南](./development.md)

---

**更新日期**: 2026-04-21  
**更新人**: AI Assistant  
**影响范围**: 数据采集、注册管理、监控告警、数据库结构  
**兼容性**: 向后兼容，需要创建新的数据库表
