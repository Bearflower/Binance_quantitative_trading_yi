# API接口设计文档

**文档版本**: v1.1
**最后更新**: 2026-05-09
**作者**: 代码图书馆长
**审核人**: 待定

---

## 文档修订历史

| 版本 | 日期 | 修改人 | 修改内容 | 审核人 |
|------|------|--------|----------|--------|
| v1.0 | 2026-05-05 | 需求文档专家 | 初始版本创建 | 待定 |
| v1.1 | 2026-05-09 | 代码图书馆长 | 新增通知服务多项目Webhook接口说明，补充回测模拟器接口概述 | 待定 |

---

## 1. 概述

### 1.1 文档目的

本文档详细描述了统一交易系统的API接口设计，包括共享模块接口、K线服务接口、通知服务接口、交易接口等，为开发人员提供接口开发的详细指导。

### 1.2 接口设计原则

- **RESTful风格**: 遵循RESTful API设计规范
- **统一格式**: 统一的请求和响应格式
- **版本控制**: 支持API版本管理
- **错误处理**: 统一的错误码和错误信息
- **文档完善**: 每个接口都有详细说明

### 1.3 接口分类

- **共享模块接口**: 提供基础功能的接口
- **K线服务接口**: 提供K线数据服务的接口
- **通知服务接口**: 提供通知服务的接口
- **交易接口**: 提供交易功能的接口

---

## 2. 接口规范

### 2.1 请求格式

**基础URL**: `http://localhost:8000/api/v1`

**请求头**:
```
Content-Type: application/json
Authorization: Bearer <token>  # 如果需要认证
```

**请求方法**:
- `GET`: 查询资源
- `POST`: 创建资源
- `PUT`: 更新资源
- `DELETE`: 删除资源

### 2.2 响应格式

**成功响应**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        // 响应数据
    },
    "timestamp": 1714944000000
}
```

**错误响应**:
```json
{
    "code": 400,
    "message": "参数错误",
    "error": {
        "field": "symbol",
        "detail": "交易对不能为空"
    },
    "timestamp": 1714944000000
}
```

### 2.3 错误码定义

| 错误码 | 说明 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未授权 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 409 | 资源冲突 |
| 429 | 请求过于频繁 |
| 500 | 服务器内部错误 |
| 503 | 服务不可用 |

### 2.4 分页格式

**请求参数**:
```
?page=1&page_size=20
```

**响应格式**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "items": [],
        "total": 100,
        "page": 1,
        "page_size": 20,
        "total_pages": 5
    },
    "timestamp": 1714944000000
}
```

---

## 3. 共享模块接口

### 3.1 配置管理接口

#### 3.1.1 获取配置

**接口**: `GET /config/{strategy}`

**描述**: 获取指定策略的配置信息

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "strategy": "btc_eth",
        "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
        "intervals": ["1h", "4h"],
        "risk_config": {
            "max_position": 0.1,
            "stop_loss": 0.05,
            "take_profit": 0.1
        },
        "notify_config": {
            "channels": ["feishu", "telegram"],
            "alert_levels": ["warning", "error", "critical"]
        }
    },
    "timestamp": 1714944000000
}
```

#### 3.1.2 更新配置

**接口**: `PUT /config/{strategy}`

**描述**: 更新指定策略的配置信息

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**请求体**:
```json
{
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "risk_config": {
        "max_position": 0.15
    }
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "配置更新成功",
    "data": {
        "updated_fields": ["symbols", "risk_config.max_position"]
    },
    "timestamp": 1714944000000
}
```

### 3.2 工具接口

#### 3.2.1 时间转换

**接口**: `POST /utils/time/convert`

**描述**: 时间戳与日期时间互相转换

**请求体**:
```json
{
    "timestamp": 1714944000000,
    "format": "datetime"  // timestamp 或 datetime
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "timestamp": 1714944000000,
        "datetime": "2024-05-05 20:00:00",
        "timezone": "UTC+8"
    },
    "timestamp": 1714944000000
}
```

#### 3.2.2 计算盈亏

**接口**: `POST /utils/pnl/calculate`

**描述**: 计算交易盈亏

**请求体**:
```json
{
    "entry_price": 65000.0,
    "exit_price": 66000.0,
    "quantity": 0.1,
    "side": "BUY",
    "fee_rate": 0.001
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "pnl": 100.0,
        "pnl_percent": 1.54,
        "fee": 13.1,
        "net_pnl": 86.9
    },
    "timestamp": 1714944000000
}
```

---

## 4. K线服务接口

### 4.1 获取K线数据

#### 4.1.1 获取历史K线

**接口**: `GET /klines`

**描述**: 获取历史K线数据

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| symbol | string | 是 | 交易对 |
| interval | string | 是 | K线周期 |
| start_time | long | 否 | 开始时间戳(毫秒) |
| end_time | long | 否 | 结束时间戳(毫秒) |
| limit | int | 否 | 返回数量，默认500 |

**请求示例**:
```
GET /klines?symbol=BTCUSDT&interval=1h&limit=100
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "klines": [
            {
                "open_time": 1714944000000,
                "close_time": 1714947599999,
                "open_price": 65000.0,
                "high_price": 65500.0,
                "low_price": 64800.0,
                "close_price": 65200.0,
                "volume": 1234.56,
                "quote_volume": 80345678.9,
                "trades_count": 5678
            }
        ],
        "count": 100
    },
    "timestamp": 1714944000000
}
```

#### 4.1.2 获取最新K线

**接口**: `GET /klines/latest`

**描述**: 获取最新K线数据

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| symbol | string | 是 | 交易对 |
| interval | string | 是 | K线周期 |

**请求示例**:
```
GET /klines/latest?symbol=BTCUSDT&interval=1h
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "kline": {
            "open_time": 1714944000000,
            "close_time": 1714947599999,
            "open_price": 65000.0,
            "high_price": 65500.0,
            "low_price": 64800.0,
            "close_price": 65200.0,
            "volume": 1234.56,
            "quote_volume": 80345678.9,
            "trades_count": 5678
        }
    },
    "timestamp": 1714944000000
}
```

### 4.2 K线订阅

#### 4.2.1 订阅K线更新

**接口**: `WebSocket /ws/klines`

**描述**: 订阅K线实时更新

**订阅消息**:
```json
{
    "action": "subscribe",
    "symbol": "BTCUSDT",
    "interval": "1h"
}
```

**推送消息**:
```json
{
    "event": "kline_update",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "kline": {
            "open_time": 1714944000000,
            "close_time": 1714947599999,
            "open_price": 65000.0,
            "high_price": 65500.0,
            "low_price": 64800.0,
            "close_price": 65200.0,
            "volume": 1234.56,
            "quote_volume": 80345678.9,
            "trades_count": 5678
        }
    },
    "timestamp": 1714944000000
}
```

#### 4.2.2 取消订阅

**取消订阅消息**:
```json
{
    "action": "unsubscribe",
    "symbol": "BTCUSDT",
    "interval": "1h"
}
```

### 4.3 K线缓存管理

#### 4.3.1 预加载K线

**接口**: `POST /klines/preload`

**描述**: 预加载历史K线数据到缓存

**请求体**:
```json
{
    "symbol": "BTCUSDT",
    "interval": "1h",
    "days": 30
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "预加载成功",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "loaded_count": 720,
        "time_range": {
            "start": 1712342400000,
            "end": 1714944000000
        }
    },
    "timestamp": 1714944000000
}
```

#### 4.3.2 清理缓存

**接口**: `DELETE /klines/cache`

**描述**: 清理K线缓存

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| symbol | string | 否 | 交易对，不指定则清理所有 |
| interval | string | 否 | K线周期，不指定则清理所有 |

**请求示例**:
```
DELETE /klines/cache?symbol=BTCUSDT&interval=1h
```

**响应示例**:
```json
{
    "code": 200,
    "message": "缓存清理成功",
    "data": {
        "cleared_count": 720
    },
    "timestamp": 1714944000000
}
```

---

## 5. 通知服务接口

### 5.1 发送通知

#### 5.1.1 发送消息

**接口**: `POST /notifications/send`

**描述**: 发送通知消息

**请求体**:
```json
{
    "message": "交易执行成功",
    "channel": "feishu",  // feishu, telegram, email
    "level": "info",  // info, warning, error, critical
    "details": {
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": 0.1,
        "price": 65000.0
    }
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "通知发送成功",
    "data": {
        "notification_id": "notif_123456",
        "channel": "feishu",
        "sent_at": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 5.1.2 发送交易通知

**接口**: `POST /notifications/trade`

**描述**: 发送交易通知（格式化消息）

**请求体**:
```json
{
    "strategy": "btc_eth",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": 0.1,
    "price": 65000.0,
    "status": "FILLED",
    "pnl": 100.0
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "交易通知发送成功",
    "data": {
        "notification_id": "notif_123457",
        "message": "【交易通知】策略: btc_eth\n交易对: BTCUSDT\n方向: BUY\n数量: 0.1\n价格: 65000.0\n状态: FILLED\n盈亏: 100.0 USDT",
        "sent_at": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 5.1.3 发送告警通知

**接口**: `POST /notifications/alert`

**描述**: 发送告警通知

**请求体**:
```json
{
    "alert_type": "position_risk",
    "level": "warning",  // info, warning, error, critical
    "title": "持仓风险告警",
    "message": "BTCUSDT持仓接近止损线",
    "details": {
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "current_price": 64000.0,
        "stop_loss_price": 63500.0,
        "position_pnl": -500.0
    }
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "告警通知发送成功",
    "data": {
        "notification_id": "notif_123458",
        "alert_id": "alert_789",
        "sent_at": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 5.1.4 多项目 Webhook 直接发送（2026-05-09 新增）⭐

**接口**: `POST /notifications/send` (NotificationClient Python 接口)

**描述**: 根据 project 参数自动路由到对应项目的专属飞书 Webhook

**Python 调用示例**:
```python
from shared.notification import NotificationClient

# 初始化客户端（启用直接 Webhook 模式）
client = NotificationClient(
    service_url="http://notification-service:8766/api/v1",
    use_direct_webhook=True  # 启用多项目 Webhook 路由
)

# 发送到网格策略专属飞书群
await client.send(
    message="【网格信号灯】ETHUSDT 震荡市场...",
    level="info",
    project="grid"  # 自动路由到 FEISHU_WEBHOOK_GRID
)

# 发送到 BTC/ETH 策略专属飞书群
await client.send(
    message="BTCUSDT 触发A级做多信号",
    level="warning",
    project="btc_eth"
)

# 发送到新币做空策略专属飞书群
await client.send(
    message="新币做空策略触发止损",
    level="error",
    project="new_coin"
)
```

**Webhook 路由规则**:
| project 参数 | 目标环境变量 | 说明 |
|-------------|-------------|------|
| `grid` | `FEISHU_WEBHOOK_GRID` | 网格交易策略专属群 |
| `btc_eth` | `FEISHU_WEBHOOK_BTC_ETH` | BTC/ETH策略专属群 |
| `new_coin` | `FEISHU_WEBHOOK_NEW_COIN` | 新币做空策略专属群 |
| 其他/未配置 | `FEISHU_WEBHOOK` | 默认兜底 Webhook |

**消息格式化**:
```python
# 发送到飞书Webhook的负载格式
payload = {
    "msg_type": "text",
    "content": {
        "text": f"{level_emoji} {message}"  # 如 "⚠️ BTCUSDT 触发A级做多信号"
    }
}
```

**适用场景**:
- ✅ 多策略消息隔离：不同策略的通知互不干扰
- ✅ 降低通知服务依赖：直接调用飞书 Webhook，绕过中间服务
- ✅ 灵活扩展：新增策略只需添加对应环境变量

### 5.2 通知历史

#### 5.2.1 查询通知历史

**接口**: `GET /notifications/history`

**描述**: 查询通知历史记录

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |
| channel | string | 否 | 通知渠道 |
| level | string | 否 | 通知级别 |
| start_time | long | 否 | 开始时间戳 |
| end_time | long | 否 | 结束时间戳 |
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页数量，默认20 |

**请求示例**:
```
GET /notifications/history?strategy=btc_eth&level=warning&page=1&page_size=20
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "items": [
            {
                "notification_id": "notif_123458",
                "strategy": "btc_eth",
                "channel": "feishu",
                "level": "warning",
                "title": "持仓风险告警",
                "message": "BTCUSDT持仓接近止损线",
                "sent_at": 1714944000000
            }
        ],
        "total": 50,
        "page": 1,
        "page_size": 20,
        "total_pages": 3
    },
    "timestamp": 1714944000000
}
```

---

## 6. 交易接口

### 6.1 账户接口

#### 6.1.1 获取账户信息

**接口**: `GET /account/info`

**描述**: 获取账户信息

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "account_type": "SPOT",
        "can_trade": true,
        "can_withdraw": true,
        "can_deposit": true,
        "balances": [
            {
                "asset": "BTC",
                "free": 0.5,
                "locked": 0.1
            },
            {
                "asset": "USDT",
                "free": 10000.0,
                "locked": 500.0
            }
        ],
        "permissions": ["SPOT"]
    },
    "timestamp": 1714944000000
}
```

#### 6.1.2 获取资产余额

**接口**: `GET /account/balance/{asset}`

**描述**: 获取指定资产余额

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| asset | string | 是 | 资产名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "asset": "BTC",
        "free": 0.5,
        "locked": 0.1,
        "total": 0.6
    },
    "timestamp": 1714944000000
}
```

### 6.2 订单接口

#### 6.2.1 创建订单

**接口**: `POST /orders`

**描述**: 创建新订单

**请求体**:
```json
{
    "strategy": "btc_eth",
    "symbol": "BTCUSDT",
    "side": "BUY",  // BUY, SELL
    "order_type": "LIMIT",  // LIMIT, MARKET, STOP_LOSS, STOP_LOSS_LIMIT
    "quantity": 0.1,
    "price": 65000.0,  // LIMIT订单必填
    "stop_price": 64000.0,  // 止损订单必填
    "time_in_force": "GTC"  // GTC, IOC, FOK
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "订单创建成功",
    "data": {
        "order_id": 123456789,
        "client_order_id": "web_123456789",
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "status": "NEW",
        "price": 65000.0,
        "quantity": 0.1,
        "executed_qty": 0.0,
        "create_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 6.2.2 查询订单

**接口**: `GET /orders/{order_id}`

**描述**: 查询订单详情

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| order_id | long | 是 | 订单ID |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "order_id": 123456789,
        "client_order_id": "web_123456789",
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "status": "FILLED",
        "price": 65000.0,
        "quantity": 0.1,
        "executed_qty": 0.1,
        "avg_price": 65000.0,
        "commission": 0.0001,
        "commission_asset": "BTC",
        "create_time": 1714944000000,
        "update_time": 1714944100000,
        "transact_time": 1714944100000
    },
    "timestamp": 1714944000000
}
```

#### 6.2.3 查询未完成订单

**接口**: `GET /orders/open`

**描述**: 查询未完成订单

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |
| symbol | string | 否 | 交易对 |

**请求示例**:
```
GET /orders/open?strategy=btc_eth&symbol=BTCUSDT
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "orders": [
            {
                "order_id": 123456789,
                "strategy": "btc_eth",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "status": "NEW",
                "price": 64000.0,
                "quantity": 0.1,
                "executed_qty": 0.0,
                "create_time": 1714944000000
            }
        ],
        "count": 1
    },
    "timestamp": 1714944000000
}
```

#### 6.2.4 撤销订单

**接口**: `DELETE /orders/{order_id}`

**描述**: 撤销订单

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| order_id | long | 是 | 订单ID |

**响应示例**:
```json
{
    "code": 200,
    "message": "订单撤销成功",
    "data": {
        "order_id": 123456789,
        "status": "CANCELED",
        "executed_qty": 0.0,
        "cancel_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 6.2.5 查询订单历史

**接口**: `GET /orders/history`

**描述**: 查询订单历史

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |
| symbol | string | 否 | 交易对 |
| status | string | 否 | 订单状态 |
| start_time | long | 否 | 开始时间戳 |
| end_time | long | 否 | 结束时间戳 |
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页数量，默认20 |

**请求示例**:
```
GET /orders/history?strategy=btc_eth&symbol=BTCUSDT&status=FILLED&page=1&page_size=20
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "items": [
            {
                "order_id": 123456789,
                "strategy": "btc_eth",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "status": "FILLED",
                "price": 65000.0,
                "quantity": 0.1,
                "executed_qty": 0.1,
                "avg_price": 65000.0,
                "create_time": 1714944000000
            }
        ],
        "total": 100,
        "page": 1,
        "page_size": 20,
        "total_pages": 5
    },
    "timestamp": 1714944000000
}
```

### 6.3 持仓接口

#### 6.3.1 查询持仓

**接口**: `GET /positions`

**描述**: 查询持仓信息

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |
| symbol | string | 否 | 交易对 |

**请求示例**:
```
GET /positions?strategy=btc_eth
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "positions": [
            {
                "strategy": "btc_eth",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quantity": 0.1,
                "avg_price": 65000.0,
                "current_price": 66000.0,
                "unrealized_pnl": 100.0,
                "realized_pnl": 50.0,
                "total_pnl": 150.0,
                "entry_time": 1714944000000
            }
        ],
        "total_pnl": 150.0
    },
    "timestamp": 1714944000000
}
```

### 6.4 交易订阅

#### 6.4.1 订阅订单更新

**接口**: `WebSocket /ws/orders`

**描述**: 订阅订单状态更新

**订阅消息**:
```json
{
    "action": "subscribe",
    "strategy": "btc_eth"
}
```

**推送消息**:
```json
{
    "event": "order_update",
    "data": {
        "order_id": 123456789,
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "status": "FILLED",
        "price": 65000.0,
        "quantity": 0.1,
        "executed_qty": 0.1,
        "avg_price": 65000.0,
        "commission": 0.0001,
        "commission_asset": "BTC",
        "transact_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 6.4.2 订阅成交更新

**接口**: `WebSocket /ws/trades`

**描述**: 订阅成交记录更新

**订阅消息**:
```json
{
    "action": "subscribe",
    "strategy": "btc_eth"
}
```

**推送消息**:
```json
{
    "event": "trade_update",
    "data": {
        "trade_id": 987654321,
        "order_id": 123456789,
        "strategy": "btc_eth",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "price": 65000.0,
        "quantity": 0.1,
        "commission": 0.0001,
        "commission_asset": "BTC",
        "transact_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

---

## 7. 策略管理接口

### 7.1 策略状态接口

#### 7.1.1 获取策略状态

**接口**: `GET /strategies/{strategy}/state`

**描述**: 获取策略运行状态

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "strategy": "btc_eth",
        "status": "running",  // running, stopped, error
        "start_time": 1714944000000,
        "uptime": 3600,
        "last_update": 1714947600000,
        "metrics": {
            "total_trades": 10,
            "win_rate": 0.7,
            "total_pnl": 500.0,
            "max_drawdown": 0.05
        }
    },
    "timestamp": 1714944000000
}
```

#### 7.1.2 保存策略状态

**接口**: `POST /strategies/{strategy}/state`

**描述**: 保存策略状态数据

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**请求体**:
```json
{
    "state_type": "grid_levels",
    "state_data": {
        "levels": [64000, 64500, 65000, 65500, 66000],
        "active_orders": ["order_1", "order_2"]
    }
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "状态保存成功",
    "data": {
        "strategy": "btc_eth",
        "state_type": "grid_levels",
        "version": 2
    },
    "timestamp": 1714944000000
}
```

### 7.2 策略控制接口

#### 7.2.1 启动策略

**接口**: `POST /strategies/{strategy}/start`

**描述**: 启动策略

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "策略启动成功",
    "data": {
        "strategy": "btc_eth",
        "status": "running",
        "start_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 7.2.2 停止策略

**接口**: `POST /strategies/{strategy}/stop`

**描述**: 停止策略

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 是 | 策略名称 |

**响应示例**:
```json
{
    "code": 200,
    "message": "策略停止成功",
    "data": {
        "strategy": "btc_eth",
        "status": "stopped",
        "stop_time": 1714944000000
    },
    "timestamp": 1714944000000
}
```

---

## 8. 技术指标接口

### 8.1 计算指标

#### 8.1.1 计算移动平均

**接口**: `POST /indicators/ma`

**描述**: 计算移动平均指标

**请求体**:
```json
{
    "symbol": "BTCUSDT",
    "interval": "1h",
    "type": "SMA",  // SMA, EMA
    "period": 20,
    "limit": 100
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "indicator": "SMA",
        "period": 20,
        "values": [
            {
                "time": 1714944000000,
                "value": 65000.5
            }
        ]
    },
    "timestamp": 1714944000000
}
```

#### 8.1.2 计算RSI

**接口**: `POST /indicators/rsi`

**描述**: 计算RSI指标

**请求体**:
```json
{
    "symbol": "BTCUSDT",
    "interval": "1h",
    "period": 14,
    "limit": 100
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "indicator": "RSI",
        "period": 14,
        "values": [
            {
                "time": 1714944000000,
                "value": 65.3
            }
        ]
    },
    "timestamp": 1714944000000
}
```

#### 8.1.3 计算MACD

**接口**: `POST /indicators/macd`

**描述**: 计算MACD指标

**请求体**:
```json
{
    "symbol": "BTCUSDT",
    "interval": "1h",
    "fast_period": 12,
    "slow_period": 26,
    "signal_period": 9,
    "limit": 100
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "symbol": "BTCUSDT",
        "interval": "1h",
        "indicator": "MACD",
        "values": [
            {
                "time": 1714944000000,
                "macd": 150.5,
                "signal": 140.2,
                "histogram": 10.3
            }
        ]
    },
    "timestamp": 1714944000000
}
```

---

## 9. 监控接口

### 9.1 系统监控

#### 9.1.1 获取系统状态

**接口**: `GET /monitor/system`

**描述**: 获取系统运行状态

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "status": "healthy",
        "uptime": 86400,
        "cpu_usage": 45.5,
        "memory_usage": 60.2,
        "disk_usage": 55.8,
        "active_strategies": 3,
        "active_connections": 5,
        "api_calls_today": 12345,
        "last_update": 1714944000000
    },
    "timestamp": 1714944000000
}
```

#### 9.1.2 获取API统计

**接口**: `GET /monitor/api/stats`

**描述**: 获取API调用统计

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| start_time | long | 否 | 开始时间戳 |
| end_time | long | 否 | 结束时间戳 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "total_calls": 12345,
        "success_rate": 99.5,
        "avg_latency": 85.3,
        "endpoints": [
            {
                "endpoint": "/klines",
                "calls": 5000,
                "avg_latency": 45.2,
                "error_rate": 0.1
            },
            {
                "endpoint": "/orders",
                "calls": 3000,
                "avg_latency": 120.5,
                "error_rate": 0.5
            }
        ]
    },
    "timestamp": 1714944000000
}
```

### 9.2 告警管理

#### 9.2.1 查询告警

**接口**: `GET /alerts`

**描述**: 查询告警记录

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| strategy | string | 否 | 策略名称 |
| level | string | 否 | 告警级别 |
| is_resolved | boolean | 否 | 是否已解决 |
| page | int | 否 | 页码 |
| page_size | int | 否 | 每页数量 |

**响应示例**:
```json
{
    "code": 200,
    "message": "success",
    "data": {
        "items": [
            {
                "alert_id": "alert_789",
                "strategy": "btc_eth",
                "alert_type": "position_risk",
                "level": "warning",
                "title": "持仓风险告警",
                "message": "BTCUSDT持仓接近止损线",
                "is_resolved": false,
                "create_time": 1714944000000
            }
        ],
        "total": 10,
        "page": 1,
        "page_size": 20,
        "total_pages": 1
    },
    "timestamp": 1714944000000
}
```

#### 9.2.2 解决告警

**接口**: `PUT /alerts/{alert_id}/resolve`

**描述**: 标记告警为已解决

**请求参数**:
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| alert_id | string | 是 | 告警ID |

**请求体**:
```json
{
    "resolved_by": "admin",
    "resolution_note": "已调整止损价格"
}
```

**响应示例**:
```json
{
    "code": 200,
    "message": "告警已解决",
    "data": {
        "alert_id": "alert_789",
        "is_resolved": true,
        "resolved_at": 1714944000000,
        "resolved_by": "admin"
    },
    "timestamp": 1714944000000
}
```

---

## 10. 接口调用示例

### 10.1 Python示例

```python
import requests
import json

class TradingAPIClient:
    """交易API客户端"""

    def __init__(self, base_url="http://localhost:8000/api/v1"):
        self.base_url = base_url
        self.session = requests.Session()

    def get_klines(self, symbol, interval, limit=100):
        """获取K线数据"""
        url = f"{self.base_url}/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        response = self.session.get(url, params=params)
        return response.json()

    def create_order(self, strategy, symbol, side, order_type, quantity, price=None):
        """创建订单"""
        url = f"{self.base_url}/orders"
        data = {
            "strategy": strategy,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity
        }
        if price:
            data["price"] = price

        response = self.session.post(url, json=data)
        return response.json()

    def send_notification(self, message, channel="feishu", level="info"):
        """发送通知"""
        url = f"{self.base_url}/notifications/send"
        data = {
            "message": message,
            "channel": channel,
            "level": level
        }
        response = self.session.post(url, json=data)
        return response.json()

# 使用示例
if __name__ == "__main__":
    client = TradingAPIClient()

    # 获取K线数据
    klines = client.get_klines("BTCUSDT", "1h", limit=100)
    print(json.dumps(klines, indent=2))

    # 创建订单
    order = client.create_order(
        strategy="btc_eth",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=0.1,
        price=65000.0
    )
    print(json.dumps(order, indent=2))

    # 发送通知
    notification = client.send_notification(
        message="订单创建成功",
        channel="feishu",
        level="info"
    )
    print(json.dumps(notification, indent=2))
```

### 10.2 WebSocket示例

```python
import websocket
import json

class KlineWebSocket:
    """K线WebSocket客户端"""

    def __init__(self, base_url="ws://localhost:8000/api/v1/ws/klines"):
        self.base_url = base_url
        self.ws = None

    def on_message(self, ws, message):
        """消息回调"""
        data = json.loads(message)
        print(f"收到K线更新: {json.dumps(data, indent=2)}")

    def on_error(self, ws, error):
        """错误回调"""
        print(f"WebSocket错误: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        """关闭回调"""
        print("WebSocket连接已关闭")

    def on_open(self, ws):
        """打开回调"""
        print("WebSocket连接已建立")
        # 订阅K线
        subscribe_msg = {
            "action": "subscribe",
            "symbol": "BTCUSDT",
            "interval": "1h"
        }
        ws.send(json.dumps(subscribe_msg))

    def connect(self):
        """建立连接"""
        self.ws = websocket.WebSocketApp(
            self.base_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever()

# 使用示例
if __name__ == "__main__":
    client = KlineWebSocket()
    client.connect()
```

---

## 11. 接口测试

### 11.1 测试用例

#### 11.1.1 获取K线数据测试

```python
def test_get_klines():
    """测试获取K线数据"""
    client = TradingAPIClient()

    # 正常情况
    response = client.get_klines("BTCUSDT", "1h", limit=100)
    assert response["code"] == 200
    assert "data" in response
    assert "klines" in response["data"]

    # 异常情况：缺少必填参数
    response = client.get_klines("", "1h")
    assert response["code"] == 400

    # 异常情况：无效的交易对
    response = client.get_klines("INVALID", "1h")
    assert response["code"] == 404
```

#### 11.1.2 创建订单测试

```python
def test_create_order():
    """测试创建订单"""
    client = TradingAPIClient()

    # 正常情况
    response = client.create_order(
        strategy="btc_eth",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=0.1,
        price=65000.0
    )
    assert response["code"] == 200
    assert "order_id" in response["data"]

    # 异常情况：余额不足
    response = client.create_order(
        strategy="btc_eth",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity=1000.0,
        price=65000.0
    )
    assert response["code"] == 400
```

### 11.2 性能测试

```python
import time
import concurrent.futures

def test_api_performance():
    """测试API性能"""
    client = TradingAPIClient()

    # 单次请求延迟
    start_time = time.time()
    for _ in range(100):
        client.get_klines("BTCUSDT", "1h", limit=10)
    end_time = time.time()

    avg_latency = (end_time - start_time) / 100 * 1000
    print(f"平均延迟: {avg_latency:.2f}ms")
    assert avg_latency < 100  # 平均延迟应小于100ms

    # 并发请求测试
    def make_request():
        return client.get_klines("BTCUSDT", "1h", limit=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(make_request) for _ in range(100)]
        results = [f.result() for f in futures]

    success_count = sum(1 for r in results if r["code"] == 200)
    success_rate = success_count / 100 * 100
    print(f"成功率: {success_rate:.2f}%")
    assert success_rate >= 99  # 成功率应大于99%
```

---

## 12. 验收标准

### 12.1 功能验收

- [ ] 所有接口正常响应
- [ ] 请求参数校验正确
- [ ] 错误处理正确
- [ ] 响应格式正确
- [ ] 分页功能正常
- [ ] WebSocket连接正常

### 12.2 性能验收

- [ ] API平均延迟 < 100ms
- [ ] API成功率 ≥ 99.5%
- [ ] 并发支持 ≥ 100 QPS
- [ ] WebSocket延迟 < 50ms

### 12.3 安全验收

- [ ] 接口认证正常
- [ ] 权限控制正确
- [ ] 参数校验严格
- [ ] SQL注入防护
- [ ] XSS防护

---

## 13. 附录

### 13.1 接口清单

详细接口清单见：[接口清单.xlsx](./接口清单.xlsx)

### 13.2 Postman集合

Postman测试集合见：[trading_api_postman_collection.json](./trading_api_postman_collection.json)

### 13.3 Swagger文档

Swagger API文档地址：`http://localhost:8000/docs`

---

**文档结束**
