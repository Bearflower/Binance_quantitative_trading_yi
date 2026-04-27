---
name: "通用模块调用指南"
description: "详细说明如何识别、提炼和调用通用模块（通知服务、K 线服务等）。Invoke when 需要开发新功能、重构现有代码、或识别可复用的通用模块时。"
---

# 通用模块调用指南

## 📚 目录

1. [通用模块概述](#通用模块概述)
2. [如何识别通用模块](#如何识别通用模块)
3. [如何提炼通用模块](#如何提炼通用模块)
4. [通用模块调用方式](#通用模块调用方式)
5. [实战案例](#实战案例)

---

## 🎯 通用模块概述

### 什么是通用模块

通用模块是指在多个项目或系统中**重复出现**、**功能相似**、**可以统一实现**的代码模块。

### 当前项目的通用模块

1. **通知服务模块** (`common_service/notification_service`)
   - 功能：统一发送飞书通知
   - 服务地址：`http://43.156.242.184:8766/api/v1`
   - 支持项目：5 个业务系统

2. **K 线数据服务** (`common_service/kline_data_service`)
   - 功能：统一获取和管理 K 线数据
   - 服务地址：`http://43.156.242.184:8765/api/v1`
   - 支持项目：需要行情数据的所有系统

3. **数据库服务** (`common_service/database`)
   - 功能：统一的数据库连接和模型
   - 数据库：PostgreSQL
   - 支持项目：所有需要持久化的系统

4. **Redis 服务** (`common_service/redis`)
   - 功能：统一的缓存和消息队列
   - 服务：Redis
   - 支持项目：所有需要缓存/队列的系统

---

## 🔍 如何识别通用模块

### 识别标准

#### 1. 重复性检查
在多个项目中搜索相似代码：
```bash
# 搜索飞书通知代码
grep -r "webhook" /Users/yl/vscode/*/

# 搜索 K 线获取代码
grep -r "kline" /Users/yl/vscode/*/

# 搜索数据库连接代码
grep -r "postgresql" /Users/yl/vscode/*/
```

**判断标准**：
- ✅ 如果 3 个以上项目有相似代码 → 应该提炼为通用模块
- ⚠️ 如果 2 个项目有相似代码 → 考虑提炼
- ❌ 如果仅 1 个项目使用 → 暂时不需要

#### 2. 功能相似性检查

检查不同项目中实现相同功能的代码：

**通知功能示例**：
- 项目 A：直接调用飞书 webhook
- 项目 B：直接调用飞书 webhook
- 项目 C：直接调用飞书 webhook

**结论**：3 个项目都在做同样的事 → 应该提炼为通用通知服务

**K 线获取示例**：
- 项目 A：调用币安 API 获取 K 线
- 项目 B：调用币安 API 获取 K 线
- 项目 C：调用币安 API 获取 K 线

**结论**：3 个项目都在做同样的事 → 应该提炼为通用 K 线服务

#### 3. 配置重复性检查

检查不同项目的配置文件：

```bash
# 检查环境变量
grep "LARK_WEBHOOK" /Users/yl/vscode/*/.env

# 检查 API Key
grep "BINANCE_API_KEY" /Users/yl/vscode/*/.env
```

**判断标准**：
- ✅ 如果多个项目配置相同的变量 → 应该集中管理
- ⚠️ 如果配置相似但不同 → 考虑参数化

### 识别流程图

```
开始
  ↓
在多个项目中搜索相似代码
  ↓
发现重复代码？
  ├─ 是 (3+ 项目) → 识别为通用模块候选 ✓
  ├─ 可能 (2 项目) → 评估提炼价值
  └─ 否 → 不需要提炼
  ↓
检查功能是否相同
  ↓
功能相同？
  ├─ 是 → 确认为通用模块 ✓
  └─ 否 → 不是通用模块
  ↓
检查配置是否重复
  ↓
配置重复？
  ├─ 是 → 配置集中管理 ✓
  └─ 否 → 保持独立
```

---

## 🏗️ 如何提炼通用模块

### 提炼步骤

#### 第一步：需求分析

1. **确定模块职责**
   - 这个模块做什么？
   - 哪些功能应该包含？
   - 哪些功能不应该包含？

2. **确定接口设计**
   - 输入参数是什么？
   - 输出结果是什么？
   - 错误如何处理？

3. **确定配置需求**
   - 需要哪些环境变量？
   - 需要哪些常量配置？
   - 如何保证向后兼容？

#### 第二步：架构设计

**推荐架构**：
```
common_service/
├── notification_service/    # 通知服务
│   ├── src/
│   │   ├── main.py         # FastAPI 入口
│   │   ├── api/
│   │   │   └── routes.py   # API 路由
│   │   ├── core/
│   │   │   ├── sender.py   # 发送逻辑
│   │   │   └── queue.py    # 消息队列
│   │   └── middleware/
│   │       └── rate_limiter.py  # 限流中间件
│   ├── Dockerfile
│   └── requirements.txt
├── kline_data_service/     # K 线服务
│   └── ...
└── docker-compose.yml       # 统一编排
```

#### 第三步：代码实现

**核心原则**：
1. **单一职责**：一个模块只做一件事
2. **接口清晰**：输入输出明确
3. **错误处理**：完善的异常捕获
4. **日志记录**：详细的运行日志
5. **配置分离**：代码和配置分离

**示例：通知服务实现**

```python
# common_service/notification_service/src/api/routes.py
from fastapi import APIRouter
from ..core.sender import FeishuSender
from ..core.queue import MessageQueue

router = APIRouter()
sender = FeishuSender()
queue = MessageQueue()

@router.post("/send")
async def send_message(project: str, message: str, type: str, level: str):
    """
    发送通知的通用接口
    
    Args:
        project: 项目标识 (btc_eth, stock, grid, etc.)
        message: 消息内容
        type: 消息类型 (text, markdown)
        level: 通知级别 (info, warning, error)
    
    Returns:
        {"code": 0, "message": "Message queued", "data": {"msg_id": "..."}}
    """
    try:
        # 1. 构建消息
        msg_data = {
            "project": project,
            "message": message,
            "type": type,
            "level": level
        }
        
        # 2. 加入队列（异步发送）
        msg_id = await queue.enqueue(msg_data)
        
        return {
            "code": 0,
            "message": "Message queued",
            "data": {"msg_id": msg_id}
        }
    except Exception as e:
        return {
            "code": -1,
            "message": str(e),
            "data": {}
        }
```

#### 第四步：测试验证

**测试清单**：
- [ ] 单元测试：测试核心功能
- [ ] 集成测试：测试服务间调用
- [ ] 压力测试：测试并发性能
- [ ] 兼容性测试：测试向后兼容

#### 第五步：部署上线

**部署步骤**：
1. 构建 Docker 镜像
2. 配置环境变量
3. 启动容器
4. 健康检查
5. 日志监控

---

## 📞 通用模块调用方式

### 1. 通知服务调用

#### REST API 调用（推荐）

**适用场景**：任何语言、任何框架

```python
import requests

NOTIFICATION_URL = "http://43.156.242.184:8766/api/v1"

def send_notification(project: str, message: str, level: str = "info"):
    """发送通知"""
    response = requests.post(
        f"{NOTIFICATION_URL}/send",
        json={
            "project": project,
            "message": message,
            "type": "text",
            "level": level
        },
        timeout=10
    )
    
    if response.status_code == 200:
        result = response.json()
        if result.get('code') == 0:
            print(f"✅ 通知发送成功：{result['data']['msg_id']}")
            return True
        else:
            print(f"❌ 通知发送失败：{result['message']}")
            return False
    else:
        print(f"❌ HTTP 错误：{response.status_code}")
        return False

# 使用示例
send_notification("btc_eth", "BTC 突破 70000 美元！", level="warning")
```

#### Python SDK 调用

**适用场景**：Python 项目

```python
# 在项目中使用改造后的模块
from utils.lark_notifier_v2 import LarkNotifier

notifier = LarkNotifier()
notifier.send_notification(
    project="btc_eth",
    message="分析任务完成",
    level="info"
)
```

#### Shell 脚本调用

**适用场景**：Shell 脚本、定时任务

```bash
#!/bin/bash

NOTIFICATION_URL="http://43.156.242.184:8766/api/v1/send"

send_notification() {
    local project="$1"
    local message="$2"
    local level="${3:-info}"
    
    curl -s -X POST "$NOTIFICATION_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"project\": \"$project\",
            \"message\": \"$message\",
            \"type\": \"text\",
            \"level\": \"$level\"
        }"
}

# 使用示例
send_notification "inspection" "服务器巡检完成" "info"
```

### 2. K 线服务调用

#### REST API 调用

```python
import requests

KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"

def get_klines(symbol: str, interval: str, limit: int = 100):
    """
    获取 K 线数据
    
    Args:
        symbol: 交易对 (BTCUSDT, ETHUSDT)
        interval: K 线周期 (1m, 5m, 1h, 1d)
        limit: 数量限制
    
    Returns:
        K 线数据列表
    """
    response = requests.get(
        f"{KLINE_SERVICE_URL}/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        },
        timeout=10
    )
    
    if response.status_code == 200:
        result = response.json()
        if result.get('code') == 0:
            return result['data']['klines']
        else:
            print(f"❌ 获取 K 线失败：{result['message']}")
            return []
    else:
        print(f"❌ HTTP 错误：{response.status_code}")
        return []

# 使用示例
klines = get_klines("BTCUSDT", "1h", limit=100)
for kline in klines:
    print(f"时间：{kline[0]}, 开盘：{kline[1]}, 收盘：{kline[4]}")
```

### 3. 数据库服务调用

#### 使用共享配置

```python
# 所有项目共享同一套数据库配置
from shared.core.config import settings

# 数据库连接
DATABASE_URL = settings.DATABASE_URL  # postgresql://...

# 使用 databases 库
import databases
database = databases.Database(DATABASE_URL)

# 查询示例
async def get_positions(user_id: int):
    query = "SELECT * FROM positions WHERE user_id = :user_id"
    results = await database.fetch_all(query, {"user_id": user_id})
    return results
```

### 4. Redis 服务调用

#### 使用共享 Redis

```python
# 使用 redis.asyncio
import redis.asyncio as redis

# 共享 Redis 连接
redis_client = redis.from_url(
    settings.REDIS_URL,  # redis://43.156.242.184:6379
    encoding="utf-8",
    decode_responses=True
)

# 缓存示例
async def cache_key(key: str, value: str, expire: int = 3600):
    await redis_client.setex(key, expire, value)

# 获取示例
async def get_cached(key: str) -> str:
    return await redis_client.get(key)
```

---

## 📋 实战案例

### 案例 1：将项目 A 的飞书通知改造为通用服务

#### 改造前

```python
# 项目 A 原有代码
import requests

class FeishuNotifier:
    def __init__(self):
        self.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    
    def send_message(self, content: str):
        payload = {
            "msg_type": "text",
            "content": {"text": content}
        }
        response = requests.post(self.webhook_url, json=payload)
        return response.json()
```

#### 改造后

```python
# 使用通用通知服务
import requests

class FeishuNotifier:
    def __init__(self):
        self.notification_url = "http://43.156.242.184:8766/api/v1/send"
        self.project = "project_a"  # 项目标识
    
    def send_message(self, content: str, level: str = "info"):
        payload = {
            "project": self.project,
            "message": content,
            "type": "text",
            "level": level
        }
        response = requests.post(self.notification_url, json=payload)
        return response.json()
```

#### 改造要点
1. ✅ 保持类名和接口不变（向后兼容）
2. ✅ 内部实现改为调用通用服务
3. ✅ 添加项目标识参数
4. ✅ 添加通知级别参数

### 案例 2：将项目 B 的 K 线获取改造为通用服务

#### 改造前

```python
# 项目 B 原有代码
import requests
import os

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')

def get_klines(symbol, interval, limit=100):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    response = requests.get(url, params=params, headers=headers)
    return response.json()
```

#### 改造后

```python
# 使用通用 K 线服务
import requests

KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"

def get_klines(symbol, interval, limit=100):
    url = f"{KLINE_SERVICE_URL}/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    response = requests.get(url, params=params)
    result = response.json()
    if result.get('code') == 0:
        return result['data']['klines']
    else:
        raise Exception(result.get('message'))
```

#### 改造要点
1. ✅ 不再直接调用币安 API
2. ✅ 改为调用通用 K 线服务
3. ✅ API Key 由通用服务统一管理
4. ✅ 返回数据格式统一

---

## 🎯 最佳实践

### 1. 命名规范

**项目标识命名**：
- ✅ `btc_eth` - 使用下划线分隔
- ✅ `new_coin` - 简洁明了
- ❌ `BTCETH` - 不要全大写
- ❌ `btc-eth` - 不要使用中划线

**通知级别**：
- `info` - 普通通知
- `warning` - 警告通知
- `error` - 错误通知

### 2. 错误处理

```python
try:
    result = send_notification(...)
    if not result:
        logger.error("通知发送失败")
except Exception as e:
    logger.error(f"通知异常：{e}")
    # 降级处理：记录到本地日志
```

### 3. 性能优化

```python
# 使用连接池
import requests
from requests.adapters import HTTPAdapter

session = requests.Session()
session.mount('http://', HTTPAdapter(pool_connections=10, pool_maxsize=20))

# 复用 session 发送通知
def send_notification(message: str):
    response = session.post(url, json=data, timeout=10)
    return response.json()
```

### 4. 监控告警

```python
# 添加重试机制
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def send_notification(message: str):
    response = requests.post(url, json=data, timeout=10)
    response.raise_for_status()
    return response.json()
```

---

## 📊 检查清单

### 识别通用模块
- [ ] 在 3 个以上项目中搜索到相似代码
- [ ] 功能相同或高度相似
- [ ] 配置重复或相似
- [ ] 有统一管理的价值

### 提炼通用模块
- [ ] 明确模块职责和边界
- [ ] 设计清晰的接口
- [ ] 实现错误处理
- [ ] 添加日志记录
- [ ] 编写单元测试
- [ ] 编写使用文档

### 调用通用模块
- [ ] 了解服务地址和端口
- [ ] 知道项目标识是什么
- [ ] 正确使用 API 接口
- [ ] 处理可能的错误
- [ ] 添加超时和重试

---

## 🔗 相关文档

- 通知服务 API 文档：`/Users/yl/vscode/common_service/docs/快速参考.md`
- K 线服务文档：`/Users/yl/vscode/common_service/kline_data_service/README.md`
- 部署文档：`/Users/yl/vscode/common_service/docs/部署配置文档.md`

---

**文档版本**: v1.0  
**最后更新**: 2026-04-20  
**维护者**: AI Assistant
