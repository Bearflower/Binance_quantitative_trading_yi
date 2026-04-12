# short-selling-system 容器错误详细报告

## 错误统计

根据日志分析，short-selling-system 容器中存在以下错误：

| 错误类型 | 错误数量 | 严重程度 |
|---------|---------|---------|
| `listing_hours` 变量未定义 | 3720 次 | 🔴 高 |
| 飞书 Webhook URL 无效 | 1 次 | 🟡 中 |
| 持仓量数据获取失败 | 2 次 | 🟡 中 |

**总计错误**: 3723+ 次

---

## 错误 1: `listing_hours` 变量未定义

### 错误信息
```
UnboundLocalError: local variable 'listing_hours' referenced before assignment
2026-03-19 08:45:32 [error] ❌ 处理 CFGUSDT 时出错：local variable 'listing_hours' referenced before assignment
```

### 问题原因
在处理新上市币种时，代码中使用了变量 `listing_hours`，但在使用之前没有进行初始化或赋值。

### 错误发生的代码位置
可能的位置：
- 处理新上市合约的函数中
- 计算币种上线时间的逻辑中

### 修复建议
在 `listing_hours` 变量使用之前添加初始化：
```python
# 在函数开头初始化变量
listing_hours = None

# 或者在使用前检查
if 'listing_hours' in locals() and listing_hours is not None:
    # 使用 listing_hours
    pass
```

---

## 错误 2: 飞书 Webhook URL 无效

### 错误信息
```
2026-03-19 08:58:09 [error] ❌ 飞书消息发送异常：Invalid URL 'your_feishu_webhook_url_here': No scheme supplied. Perhaps you meant https://your_feishu_webhook_url_here?
```

### 问题原因
飞书 webhook URL 配置为 `your_feishu_webhook_url_here`（占位符），而不是实际的 URL。

### 修复建议
1. 检查 `.env` 文件中的 `LARK_WEBHOOK_URL` 配置
2. 将 `your_feishu_webhook_url_here` 替换为实际的飞书 webhook URL
3. URL 必须以 `https://` 开头

### 修复代码示例
```python
# 在 LarkNotifier 类中添加 URL 验证
def _is_valid_url(self, url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    return url.startswith('http://') or url.startswith('https://')

# 在初始化时验证
def __init__(self, webhook_url=None):
    self.webhook_url = webhook_url or os.getenv('LARK_WEBHOOK_URL')
    if self.webhook_url and not self._is_valid_url(self.webhook_url):
        logger.error(f"无效的飞书 webhook URL 格式：{self.webhook_url}")
        self.webhook_url = None
```

---

## 错误 3: 持仓量数据获取失败

### 错误信息
```
2026-03-19 22:03:44 [error] ❌ 获取 EDGEUSDT 持仓量数据失败
2026-03-19 22:03:44 [error] ❌ 获取 EDGEUSDT 持仓量数据失败
```

### 问题原因
在获取 EDGEUSDT 合约的持仓量（open interest）数据时失败，可能是：
- 网络问题
- API 限流
- 该币种数据不存在

### 修复建议
1. 添加更详细的错误日志，记录失败原因
2. 添加重试机制
3. 添加容错处理，即使获取失败也不中断主流程

### 修复代码示例
```python
def get_open_interest(self, symbol: str):
    """获取持仓量数据"""
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {
                "openInterest": data.get("openInterest", "0"),
                "timestamp": data.get("time", 0)
            }
        else:
            logger.warning(f"获取 {symbol} 持仓量失败，状态码：{response.status_code}")
            return None
    except requests.exceptions.Timeout:
        logger.error(f"获取 {symbol} 持仓量超时")
        return None
    except Exception as e:
        logger.error(f"获取 {symbol} 持仓量错误：{str(e)}")
        return None
```

---

## 需要修复的文件列表

基于错误类型，以下文件可能需要修改：

### 1. 处理新上市币种的逻辑文件
- 查找包含 `listing_hours` 的文件
- 通常在 `monitor_new_coins` 或类似模块中

### 2. 飞书通知配置
- 查找 `lark_notifier.py` 或类似文件
- 或 `.env` 配置文件

### 3. 持仓量获取模块
- 查找 `open_interest` 或 `持仓量` 相关代码
- 或 `binance_api.py` 相关文件

---

## 快速修复指南

### 修复 1: 变量初始化
```python
# 在函数开头添加
listing_hours = 0  # 或 None，根据业务逻辑
```

### 修复 2: 飞书 URL 配置
```bash
# 在 .env 文件中
LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的真实hook地址
```

### 修复 3: 持仓量获取容错
```python
# 添加 try-except 包装
try:
    open_interest = get_open_interest(symbol)
except Exception as e:
    logger.error(f"获取 {symbol} 持仓量数据失败：{str(e)}")
    open_interest = None  # 设置默认值，避免后续使用时报错
```

---

## 日志时间范围

- **最早错误时间**: 2026-03-19 08:45:32
- **最新错误时间**: 2026-03-19 22:03:44
- **错误持续时间**: 约 13 小时

---

## 建议的修复优先级

1. **🔴 高优先级**: 修复 `listing_hours` 变量未定义问题 - 这是导致最多错误的问题
2. **🟡 中优先级**: 修复飞书 webhook URL 配置 - 影响通知功能
3. **🟡 中优先级**: 添加持仓量获取容错 - 提高系统稳定性

---

## 相关代码参考

如果你需要参考本项目中已修复的类似代码，可以查看：

- `utils/json_extractor.py` - JSON 提取和错误处理
- `utils/lark_notifier.py` - 飞书通知和 URL 验证
- `utils/technical_indicators.py` - 持仓量数据获取

---

**报告生成时间**: 2026-03-20
**错误总数**: 3723+ 次
