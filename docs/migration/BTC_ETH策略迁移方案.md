# 主流币种趋势回调确认策略(MTPCS) 迁移方案

**文档版本**: v1.0
**最后更新**: 2026-05-05
**作者**: 需求文档专家
**审核人**: 待定

---

## 文档修订历史

| 版本 | 日期 | 修改人 | 修改内容 | 审核人 |
|------|------|--------|----------|--------|
| v1.0 | 2026-05-05 | 需求文档专家 | 初始版本创建 | 待定 |

---

## 1. 迁移概述

### 1.1 迁移目标

将 `bianace_btcethbnb_trade` 项目中的主流币种趋势回调确认策略(MTPCS)（原 BTC/ETH/BNB 交易策略）迁移到统一交易系统 `Binance_quantitative_trading`，实现：

- **代码复用**: 共享核心模块，减少重复代码
- **统一管理**: 统一配置、日志、监控
- **渐进迁移**: 保留原有项目作为备份，逐步迁移
- **功能对等**: 确保迁移后功能完全一致

### 1.2 迁移范围

| 模块 | 原路径 | 目标路径 | 迁移类型 |
|------|--------|----------|----------|
| 评分引擎 | `core/scoring/v612.py` | `strategies/btc_eth/strategy.py` | 策略层 |
| 信号检测 | `core/signal/detector.py` | `strategies/btc_eth/strategy.py` | 策略层 |
| K线服务 | `utils/kline_service.py` | `shared/kline_service.py` | 共享模块 |
| 通知服务 | `utils/lark_notifier.py` | `shared/notification.py` | 共享模块 |
| 交易API | `utils/binance_trade_api.py` | `shared/binance_api.py` | 共享模块 |
| 数据库服务 | `models/database.py` | `shared/database.py` | 共享模块 |
| 配置管理 | `config/` | `strategies/btc_eth/config.yaml` | 策略层 |

### 1.3 迁移策略

采用**渐进式迁移**策略，分三个阶段：

1. **第一阶段**: 共享模块迁移（K线服务、通知服务、交易API、数据库）
2. **第二阶段**: 策略核心迁移（评分引擎、信号检测、交易执行）
3. **第三阶段**: 配置和部署迁移（配置文件、Docker、监控）

---

## 2. 关键迁移点

### 2.1 K线数据服务迁移

#### 2.1.1 原有实现分析

原有项目使用通用 K 线服务客户端：

```python
# 原路径: /Users/yl/vscode/bianace_btcethbnb_trade/utils/kline_service.py
class KlineServiceClient:
    """通用 K 线服务客户端"""
    
    def __init__(self, service_url: Optional[str] = None):
        self.service_url = service_url or KLINE_SERVICE_URL
        
    def get_latest_klines(self, symbol: str, interval: str, limit: int = 100):
        """获取最新 K 线数据"""
        # HTTP 调用通用服务
        ...
```

**特点**:
- 通过 HTTP 调用通用 K 线服务
- 服务地址: `http://43.156.242.184:8765/api/v1`
- 支持获取 K 线、技术指标、币种列表

#### 2.1.2 目标实现设计

统一交易系统共享模块设计：

```python
# 目标路径: /Users/yl/vscode/Binance_quantitative_trading/shared/kline_service.py
class KlineService:
    """K线服务 - 统一接口"""
    
    def __init__(self, binance_api: BinanceAPI, db: DatabaseService):
        self.binance_api = binance_api
        self.db = db
        self._cache = {}
        
    def get_klines(self, symbol: str, interval: str, 
                   start_time: datetime = None,
                   end_time: datetime = None) -> pd.DataFrame:
        """获取K线数据（优先从缓存获取）"""
        # 1. 检查缓存
        cache_key = f"{symbol}_{interval}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 2. 从数据库获取
        klines = self.db.get_klines(symbol, interval, start_time, end_time)
        if klines:
            return self._dataframe_from_klines(klines)
        
        # 3. 从 API 获取
        klines = self.binance_api.get_klines(symbol, interval, limit=100)
        return self._dataframe_from_klines(klines)
```

#### 2.1.3 迁移步骤

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 创建共享模块 | 在 `shared/kline_service.py` 中实现统一接口 |
| 2 | 兼容性适配 | 保留原有 HTTP 调用方式作为备用 |
| 3 | 策略层调用 | 修改策略代码，使用共享模块 |
| 4 | 测试验证 | 确保数据获取正常 |

#### 2.1.4 代码修改示例

**修改前（策略代码）**:
```python
from utils.kline_service import KlineServiceClient

kline_client = KlineServiceClient()
klines = kline_client.get_latest_klines('BTCUSDT', '1h', 100)
```

**修改后（策略代码）**:
```python
import sys
sys.path.insert(0, '/app/shared')
from kline_service import KlineService

kline_service = KlineService(binance_api, db)
klines = kline_service.get_klines('BTCUSDT', '1h')
```

### 2.2 通知服务迁移

#### 2.2.1 原有实现分析

```python
# 原路径: /Users/yl/vscode/bianace_btcethbnb_trade/utils/lark_notifier.py
class LarkNotifier:
    """飞书通知类"""
    
    def __init__(self, webhook_url=None):
        self.notification_service_url = os.getenv(
            'NOTIFICATION_SERVICE_URL', 
            'http://43.156.242.184:8766/api/v1'
        )
        self.project = os.getenv('NOTIFICATION_PROJECT', 'btc_eth_bnb')
        
    def send_text_message(self, content, level='info'):
        """发送文本消息"""
        payload = {
            "project": self.project,
            "message": content,
            "type": "text",
            "level": level
        }
        response = requests.post(f"{self.notification_service_url}/send", json=payload)
        ...
```

**特点**:
- 通过 HTTP 调用通用通知服务
- 支持多项目隔离（project 参数）
- 支持消息级别（info/warning/error）

#### 2.2.2 目标实现设计

```python
# 目标路径: /Users/yl/vscode/Binance_quantitative_trading/shared/notification.py
class NotificationService:
    """通知服务 - 统一接口"""
    
    def __init__(self, config: dict):
        self.config = config
        self.service_url = config.get('notification_service_url')
        
    def send_message(self, message: str, channel: str = 'feishu', 
                     level: str = 'info', project: str = None):
        """发送消息"""
        project = project or self.config.get('project', 'trading_system')
        payload = {
            "project": project,
            "message": message,
            "type": "text",
            "level": level
        }
        return self._send_request(payload)
        
    def send_trade_notification(self, trade: dict):
        """发送交易通知"""
        message = self._format_trade_message(trade)
        return self.send_message(message, level='info')
```

#### 2.2.3 迁移步骤

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 创建共享模块 | 在 `shared/notification.py` 中实现统一接口 |
| 2 | 保持兼容 | 保留原有 LarkNotifier 接口 |
| 3 | 策略层调用 | 修改策略代码，使用共享模块 |
| 4 | 测试验证 | 确保通知发送正常 |

### 2.3 交易模块迁移

#### 2.3.1 原有实现分析

```python
# 原路径: /Users/yl/vscode/bianace_btcethbnb_trade/utils/binance_trade_api.py
class BinanceTradeAPI:
    """币安交易 API 封装类"""
    
    def __init__(self, api_key: str = None, secret_key: str = None, 
                 base_url: str = "https://papi.binance.com", testnet: bool = False):
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.secret_key = secret_key or os.getenv('BINANCE_SECRET_KEY')
        self.base_url = base_url
        ...
        
    def place_um_order(self, symbol: str, side: str, position_side: str, 
                      order_type: str, quantity: Decimal, price: Decimal = None,
                      ...):
        """UM 合约下单"""
        ...
```

**特点**:
- 完整的 PM 账户 API 封装
- 支持限流和重试机制
- 支持精度自动处理

#### 2.3.2 目标实现设计

```python
# 目标路径: /Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py
class BinanceAPI:
    """Binance API 客户端 - 统一接口"""
    
    _instance = None  # 单例模式
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, api_key: str = None, secret_key: str = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.secret_key = secret_key or os.getenv('BINANCE_SECRET_KEY')
        self._initialized = True
        ...
```

#### 2.3.3 迁移步骤

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 创建共享模块 | 在 `shared/binance_api.py` 中实现统一接口 |
| 2 | 单例模式 | 确保全局共享一个 API 客户端实例 |
| 3 | 连接池 | 实现连接池管理 |
| 4 | 策略层调用 | 修改策略代码，使用共享模块 |

### 2.4 数据库迁移

#### 2.4.1 原有实现分析

```python
# 原路径: /Users/yl/vscode/bianace_btcethbnb_trade/models/database.py
class DatabaseManager:
    """数据库管理类"""
    
    def __init__(self, db_url: str = None):
        self.db_url = db_url or os.getenv('DATABASE_URL')
        # PostgreSQL 连接池
        _connection_pool = pool.SimpleConnectionPool(1, 10, dsn=self.db_url)
        ...
        
    def save_trade(self, order_data: Dict[str, Any], ...):
        """保存交易记录"""
        query = """INSERT INTO trades (...) VALUES (...)"""
        ...
```

**特点**:
- 使用 PostgreSQL 数据库
- 连接池管理
- 完整的 CRUD 操作

#### 2.4.2 目标实现设计

统一交易系统数据库设计已在 `docs/architecture/数据库设计.md` 中定义，主要表结构：

| 表名 | 用途 | 说明 |
|------|------|------|
| `trading.klines` | K线数据 | 支持多交易对、多周期 |
| `trading.orders` | 订单记录 | 支持多策略 |
| `trading.trades` | 成交记录 | 关联订单 |
| `trading.positions` | 持仓信息 | 支持多策略 |
| `trading.strategy_state` | 策略状态 | JSON 格式存储 |

#### 2.4.3 迁移步骤

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 创建共享模块 | 在 `shared/database.py` 中实现统一接口 |
| 2 | 数据迁移 | 将原有数据迁移到新表结构 |
| 3 | 策略层调用 | 修改策略代码，使用共享模块 |
| 4 | 数据验证 | 确保数据完整性 |

### 2.5 评分引擎迁移

#### 2.5.1 原有实现分析

```python
# 原路径: /Users/yl/vscode/bianace_btcethbnb_trade/core/scoring/v612.py
class ScoringEngineV612(ScoringEngineBase):
    """评分引擎 v6.12 - 方案 A 稳健型"""
    
    def score(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """执行评分"""
        # 1. 一票否决检查
        veto_reason = self._check_veto(data)
        
        # 2. 数据完整性检查
        is_valid, confidence = self._check_data_integrity(data)
        
        # 3. 市场状态过滤
        market_state = self._check_market_state(data)
        
        # 4. 6 维度评分
        trend_strength = self._score_trend_strength(indicators)
        trend_consistency = self._score_trend_consistency(indicators)
        pattern = self._score_pattern(indicators)
        volume = self._score_volume(indicators)
        momentum = self._score_momentum(indicators)
        risk = self._score_risk(symbol, data)
        
        # 5. 加权总分
        total_score = ...
        ...
```

**特点**:
- 6维度评分体系（趋势强度、趋势一致性、形态质量、成交量、动量、风险）
- 动态评分参数（从配置文件读取）
- 一票否决机制

#### 2.5.2 目标实现设计

```python
# 目标路径: /Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py
class BTCETHStrategy(BaseStrategy):
    """BTC/ETH 策略"""
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.scoring_engine = ScoringEngineV612(config)
        
    def on_kline(self, kline: dict):
        """K线更新回调"""
        # 获取技术指标
        indicators = self.kline_service.get_indicators(kline['symbol'], '1h')
        
        # 执行评分
        score_result = self.scoring_engine.score(kline['symbol'], {
            'indicators': indicators,
            'funding_rate': self.get_funding_rate(kline['symbol'])
        })
        
        # 交易决策
        if score_result['grade'] in ['S', 'A']:
            self.execute_trade(score_result)
```

---

## 3. 迁移步骤

### 3.1 第一阶段：共享模块迁移（2天）

#### 3.1.1 步骤1：创建共享模块框架

```bash
# 创建共享模块目录
cd /Users/yl/vscode/Binance_quantitative_trading
mkdir -p shared
touch shared/__init__.py
touch shared/kline_service.py
touch shared/notification.py
touch shared/binance_api.py
touch shared/database.py
touch shared/indicators.py
touch shared/utils.py
```

#### 3.1.2 步骤2：实现K线服务

```python
# shared/kline_service.py
#!/usr/bin/env python3
"""
K线服务 - 统一接口
提供K线数据获取、缓存、技术指标计算功能
"""

import os
import logging
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)

# 通用 K 线服务配置
KLINE_SERVICE_URL = os.getenv('KLINE_SERVICE_URL', 'http://43.156.242.184:8765/api/v1')


class KlineService:
    """K线服务 - 统一接口"""
    
    def __init__(self, db=None, binance_api=None):
        """
        初始化 K 线服务
        
        Args:
            db: 数据库服务实例
            binance_api: Binance API 实例
        """
        self.db = db
        self.binance_api = binance_api
        self.service_url = os.getenv('KLINE_SERVICE_URL', KLINE_SERVICE_URL)
        self._cache = {}
        logger.info(f"K线服务初始化完成：{self.service_url}")
    
    def get_klines(self, symbol: str, interval: str, 
                   start_time: datetime = None,
                   end_time: datetime = None,
                   limit: int = 100) -> Optional[List[Dict[str, Any]]]:
        """
        获取K线数据（优先从缓存获取）
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            start_time: 开始时间
            end_time: 结束时间
            limit: 获取数量
            
        Returns:
            K线数据列表
        """
        # 1. 检查缓存
        cache_key = f"{symbol}_{interval}"
        if cache_key in self._cache:
            logger.debug(f"从缓存获取K线数据：{cache_key}")
            return self._cache[cache_key]
        
        # 2. 从通用服务获取
        klines = self._get_from_service(symbol, interval, limit)
        if klines:
            self._cache[cache_key] = klines
            return klines
        
        # 3. 从 API 获取（备用）
        if self.binance_api:
            klines = self.binance_api.get_klines(symbol, interval, limit)
            if klines:
                return klines
        
        return None
    
    def _get_from_service(self, symbol: str, interval: str, limit: int):
        """从通用服务获取K线数据"""
        try:
            url = f"{self.service_url}/klines/latest"
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    klines = result.get('data', [])
                    logger.info(f"获取 {symbol} {interval} K线数据 {len(klines)} 条")
                    return klines
                    
            return None
            
        except Exception as e:
            logger.error(f"获取K线数据异常：{e}")
            return None
    
    def get_indicators(self, symbol: str, interval: str, period: int = 100):
        """获取技术指标"""
        try:
            url = f"{self.service_url}/indicators"
            params = {
                "symbol": symbol,
                "interval": interval,
                "period": period
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result.get('data')
                    
            return None
            
        except Exception as e:
            logger.error(f"获取技术指标异常：{e}")
            return None
```

#### 3.1.3 步骤3：实现通知服务

```python
# shared/notification.py
#!/usr/bin/env python3
"""
通知服务 - 统一接口
支持飞书、Telegram等多渠道通知
"""

import os
import logging
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# 通用通知服务配置
NOTIFICATION_SERVICE_URL = os.getenv('NOTIFICATION_SERVICE_URL', 'http://43.156.242.184:8766/api/v1')


class NotificationService:
    """通知服务 - 统一接口"""
    
    def __init__(self, config: dict = None):
        """
        初始化通知服务
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.service_url = self.config.get('notification_service_url', NOTIFICATION_SERVICE_URL)
        self.project = self.config.get('project', 'trading_system')
        self.timeout = 10
        logger.info(f"通知服务初始化完成，项目：{self.project}")
    
    def send_message(self, message: str, channel: str = 'feishu', 
                     level: str = 'info', project: str = None) -> Dict[str, Any]:
        """
        发送消息
        
        Args:
            message: 消息内容
            channel: 通知渠道
            level: 消息级别
            project: 项目标识
            
        Returns:
            发送结果
        """
        project = project or self.project
        payload = {
            "project": project,
            "message": message,
            "type": "text",
            "level": level
        }
        
        try:
            response = requests.post(
                f"{self.service_url}/send",
                json=payload,
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"消息发送成功：{result.get('data', {}).get('msg_id', 'N/A')}")
                    return {"status": "success", "data": result.get('data')}
                    
            return {"status": "error", "message": "发送失败"}
            
        except Exception as e:
            logger.error(f"发送消息异常：{e}")
            return {"status": "error", "message": str(e)}
    
    def send_trade_notification(self, trade: dict):
        """发送交易通知"""
        message = f"""
📊 交易通知

交易对: {trade.get('symbol')}
方向: {trade.get('side')}
数量: {trade.get('quantity')}
价格: {trade.get('price')}
状态: {trade.get('status')}
时间: {trade.get('time')}
        """
        return self.send_message(message, level='info')
    
    def send_alert(self, alert_type: str, message: str, level: str = 'warning'):
        """发送告警通知"""
        return self.send_message(f"⚠️ [{alert_type}] {message}", level=level)
```

#### 3.1.4 步骤4：实现交易API

```python
# shared/binance_api.py
#!/usr/bin/env python3
"""
Binance API 客户端 - 统一接口
封装 Binance REST API 和 WebSocket
"""

import os
import logging
import hashlib
import hmac
import time
import requests
from decimal import Decimal
from typing import Optional, Dict, List, Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class BinanceAPI:
    """Binance API 客户端 - 单例模式"""
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, api_key: str = None, secret_key: str = None,
                 base_url: str = "https://papi.binance.com"):
        if hasattr(self, '_initialized') and self._initialized:
            return
            
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.secret_key = secret_key or os.getenv('BINANCE_SECRET_KEY')
        self.base_url = base_url
        
        if not self.api_key or not self.secret_key:
            raise ValueError("API Key 和 Secret Key 不能为空")
        
        self._initialized = True
        logger.info("Binance API 客户端初始化完成")
    
    def _generate_signature(self, query_string: str) -> str:
        """生成 HMAC SHA256 签名"""
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        return {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    
    def _make_request(self, method: str, endpoint: str, 
                     params: Dict[str, Any] = None, 
                     signed: bool = False) -> Dict[str, Any]:
        """发送 API 请求"""
        url = f"{self.base_url}{endpoint}"
        
        if signed:
            params = params or {}
            params['timestamp'] = int(time.time() * 1000)
            query_string = urlencode(params)
            params['signature'] = self._generate_signature(query_string)
        
        headers = self._get_headers()
        
        try:
            if method == 'GET':
                response = requests.get(url, params=params, headers=headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, data=params, headers=headers, timeout=10)
            else:
                raise ValueError(f"不支持的 HTTP 方法：{method}")
            
            return response.json()
            
        except Exception as e:
            logger.error(f"API 请求失败：{e}")
            raise
    
    # ==================== 公共接口 ====================
    
    def get_server_time(self) -> int:
        """获取服务器时间"""
        data = self._make_request('GET', '/papi/v1/time')
        return data['serverTime']
    
    def get_ticker_price(self, symbol: str) -> Decimal:
        """获取最新价格"""
        params = {'symbol': symbol}
        data = self._make_request('GET', '/api/v3/ticker/price', params)
        return Decimal(data['price'])
    
    def get_klines(self, symbol: str, interval: str, limit: int = 100):
        """获取K线数据"""
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        return self._make_request('GET', '/papi/v1/um/klines', params)
    
    # ==================== 账户接口 ====================
    
    def get_account_info(self) -> Dict[str, Any]:
        """获取账户信息"""
        return self._make_request('GET', '/papi/v1/balance', signed=True)
    
    def get_umfut_balance(self, asset: str = 'USDT') -> Decimal:
        """获取 U 本位合约账户余额"""
        account_info = self.futures_account()
        for acc_asset in account_info.get('assets', []):
            if acc_asset.get('asset') == asset:
                cross_wallet_balance = Decimal(acc_asset.get('crossWalletBalance', '0'))
                initial_margin = Decimal(acc_asset.get('initialMargin', '0'))
                return cross_wallet_balance - initial_margin
        return Decimal('0')
    
    def futures_account(self) -> Dict[str, Any]:
        """获取 U 本位合约账户信息"""
        return self._make_request('GET', '/papi/v1/um/account', signed=True)
    
    # ==================== 交易接口 ====================
    
    def place_order(self, symbol: str, side: str, order_type: str,
                   quantity: Decimal, price: Decimal = None,
                   position_side: str = 'BOTH', **kwargs) -> Dict[str, Any]:
        """创建订单"""
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': str(quantity),
            'positionSide': position_side
        }
        
        if price:
            params['price'] = str(price)
            params['timeInForce'] = kwargs.get('time_in_force', 'GTC')
        
        return self._make_request('POST', '/papi/v1/um/order', params, signed=True)
    
    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        """撤销订单"""
        params = {
            'symbol': symbol,
            'orderId': order_id
        }
        return self._make_request('DELETE', '/papi/v1/um/order', params, signed=True)
    
    def get_open_orders(self, symbol: str = None) -> List[Dict[str, Any]]:
        """获取未完成订单"""
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/papi/v1/um/openOrders', params, signed=True)
    
    # ==================== 持仓接口 ====================
    
    def get_position_risk(self, symbol: str = None) -> List[Dict[str, Any]]:
        """获取持仓风险"""
        params = {}
        if symbol:
            params['symbol'] = symbol
        return self._make_request('GET', '/papi/v1/um/positionRisk', params, signed=True)
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定持仓"""
        positions = self.get_position_risk(symbol)
        for pos in positions:
            if pos['symbol'] == symbol and Decimal(pos['positionAmt']) != 0:
                return pos
        return None
```

#### 3.1.5 步骤5：实现数据库服务

```python
# shared/database.py
#!/usr/bin/env python3
"""
数据库服务 - 统一接口
PostgreSQL 数据库操作封装
"""

import os
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


class DatabaseService:
    """数据库服务 - 统一接口"""
    
    def __init__(self, db_url: str = None):
        """
        初始化数据库服务
        
        Args:
            db_url: 数据库连接 URL
        """
        self.db_url = db_url or os.getenv(
            'DATABASE_URL', 
            'postgresql://bianace_user:Bianace%402024@postgres-db:5432/trading_platform'
        )
        
        # 初始化连接池
        self._pool = pool.SimpleConnectionPool(
            1, 10,
            dsn=self.db_url,
            cursor_factory=RealDictCursor
        )
        
        logger.info(f"数据库服务初始化完成")
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接"""
        conn = None
        try:
            conn = self._pool.getconn()
            with conn.cursor() as cursor:
                cursor.execute("SET search_path TO trading, public")
            yield conn
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"数据库操作失败：{e}")
            raise
        finally:
            if conn:
                self._pool.putconn(conn)
    
    # ==================== K线数据操作 ====================
    
    def save_klines(self, symbol: str, interval: str, klines: List[Dict[str, Any]]):
        """保存K线数据"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                for kline in klines:
                    query = """
                        INSERT INTO trading.klines (
                            symbol, interval, open_time, close_time,
                            open_price, high_price, low_price, close_price,
                            volume, quote_volume, trades_count
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
                            close_price = EXCLUDED.close_price,
                            volume = EXCLUDED.volume
                    """
                    cursor.execute(query, (
                        symbol, interval, kline['open_time'], kline['close_time'],
                        kline['open_price'], kline['high_price'], 
                        kline['low_price'], kline['close_price'],
                        kline['volume'], kline['quote_volume'], kline['trades_count']
                    ))
                conn.commit()
        logger.info(f"保存K线数据：{symbol} {interval} {len(klines)} 条")
    
    def get_klines(self, symbol: str, interval: str, 
                   start_time: datetime = None,
                   end_time: datetime = None,
                   limit: int = 100) -> List[Dict[str, Any]]:
        """查询K线数据"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT * FROM trading.klines
                    WHERE symbol = %s AND interval = %s
                """
                params = [symbol, interval]
                
                if start_time:
                    query += " AND open_time >= %s"
                    params.append(int(start_time.timestamp() * 1000))
                
                if end_time:
                    query += " AND open_time < %s"
                    params.append(int(end_time.timestamp() * 1000))
                
                query += " ORDER BY open_time DESC LIMIT %s"
                params.append(limit)
                
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 订单操作 ====================
    
    def save_order(self, order: Dict[str, Any]):
        """保存订单记录"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    INSERT INTO trading.orders (
                        order_id, strategy, symbol, side, order_type, status,
                        price, quantity, executed_qty, create_time
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        executed_qty = EXCLUDED.executed_qty
                """
                cursor.execute(query, (
                    order['orderId'], order.get('strategy', 'btc_eth'),
                    order['symbol'], order['side'], order['type'], order['status'],
                    order.get('price'), order.get('origQty'), order.get('executedQty'),
                    order.get('updateTime', int(datetime.now().timestamp() * 1000))
                ))
                conn.commit()
        logger.info(f"订单记录已保存：{order['orderId']}")
    
    def get_orders(self, strategy: str, symbol: str = None, 
                   limit: int = 100) -> List[Dict[str, Any]]:
        """查询订单记录"""
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                query = "SELECT * FROM trading.orders WHERE strategy = %s"
                params = [strategy]
                
                if symbol:
                    query += " AND symbol = %s"
                    params.append(symbol)
                
                query += " ORDER BY create_time DESC LIMIT %s"
                params.append(limit)
                
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 策略状态操作 ====================
    
    def save_strategy_state(self, strategy: str, state_type: str, state_data: Dict[str, Any]):
        """保存策略状态"""
        import json
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    INSERT INTO trading.strategy_state (strategy, state_type, state_data)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (strategy, state_type) DO UPDATE SET
                        state_data = EXCLUDED.state_data,
                        updated_at = CURRENT_TIMESTAMP
                """
                cursor.execute(query, (strategy, state_type, json.dumps(state_data)))
                conn.commit()
        logger.info(f"策略状态已保存：{strategy}/{state_type}")
    
    def get_strategy_state(self, strategy: str, state_type: str) -> Optional[Dict[str, Any]]:
        """获取策略状态"""
        import json
        with self.get_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT state_data FROM trading.strategy_state
                    WHERE strategy = %s AND state_type = %s
                """
                cursor.execute(query, (strategy, state_type))
                result = cursor.fetchone()
                if result:
                    return json.loads(result['state_data'])
                return None
```

### 3.2 第二阶段：策略核心迁移（3天）

#### 3.2.1 步骤1：创建策略目录结构

```bash
# 创建策略目录
cd /Users/yl/vscode/Binance_quantitative_trading
mkdir -p strategies/btc_eth
touch strategies/btc_eth/__init__.py
touch strategies/btc_eth/config.yaml
touch strategies/btc_eth/main.py
touch strategies/btc_eth/strategy.py
touch strategies/btc_eth/requirements.txt
touch strategies/btc_eth/Dockerfile
```

#### 3.2.2 步骤2：创建策略配置文件

```yaml
# strategies/btc_eth/config.yaml
# BTC/ETH 策略配置

# 策略基本信息
strategy:
  name: btc_eth
  version: "1.0.0"
  description: "BTC/ETH/BNB 趋势跟踪策略"

# 交易对配置
symbols:
  - BTCUSDT
  - ETHUSDT
  - BNBUSDT

# 评分引擎配置
scoring:
  weights:
    trend: 30
    pattern: 30
    momentum: 20
    risk: 20
  grade_thresholds:
    S: 75
    A: 60
    B: 45
    C: 30
  position_ratio:
    base: 0.3
    max: 0.6
    coefficient: 0.3
  veto:
    max_funding_rate: 0.0008
    max_volatility: 0.06

# 交易配置
trading:
  leverage: 20
  max_positions: 2
  single_position_margin: 30  # USDT
  stop_loss_atr_multiplier: 2.0
  take_profit_atr_multiplier: 3.0

# K线配置
kline:
  intervals:
    - 1d
    - 4h
    - 1h
  default_limit: 100

# 通知配置
notification:
  project: btc_eth
  channels:
    - feishu

# 数据库配置
database:
  schema: trading

# 日志配置
logging:
  level: INFO
  file: logs/btc_eth_strategy.log
```

#### 3.2.3 步骤3：创建策略主程序

```python
# strategies/btc_eth/main.py
#!/usr/bin/env python3
"""
BTC/ETH 策略主程序
"""

import os
import sys
import logging
import yaml
from datetime import datetime

# 添加共享模块路径
sys.path.insert(0, '/app/shared')

from kline_service import KlineService
from notification import NotificationService
from binance_api import BinanceAPI
from database import DatabaseService
from strategy import BTCETHStrategy

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("BTC/ETH 策略启动")
    logger.info("=" * 60)
    
    # 加载配置
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    config = load_config(config_path)
    logger.info(f"配置加载完成：{config['strategy']['name']}")
    
    # 初始化共享模块
    binance_api = BinanceAPI()
    db = DatabaseService()
    kline_service = KlineService(db=db, binance_api=binance_api)
    notification = NotificationService(config.get('notification', {}))
    
    # 初始化策略
    strategy = BTCETHStrategy(
        config=config,
        binance_api=binance_api,
        db=db,
        kline_service=kline_service,
        notification=notification
    )
    
    # 运行策略
    try:
        strategy.run()
    except KeyboardInterrupt:
        logger.info("策略手动停止")
    except Exception as e:
        logger.error(f"策略运行异常：{e}", exc_info=True)
        notification.send_alert("STRATEGY_ERROR", str(e), level='error')
    finally:
        strategy.stop()
        logger.info("策略已停止")


if __name__ == '__main__':
    main()
```

#### 3.2.4 步骤4：创建策略核心逻辑

```python
# strategies/btc_eth/strategy.py
#!/usr/bin/env python3
"""
BTC/ETH 策略核心逻辑
"""

import logging
from typing import Dict, Any, Optional
from decimal import Decimal
from datetime import datetime

logger = logging.getLogger(__name__)


class BTCETHStrategy:
    """BTC/ETH 策略"""
    
    def __init__(self, config: dict, binance_api, db, kline_service, notification):
        """
        初始化策略
        
        Args:
            config: 配置字典
            binance_api: Binance API 实例
            db: 数据库服务实例
            kline_service: K线服务实例
            notification: 通知服务实例
        """
        self.config = config
        self.binance_api = binance_api
        self.db = db
        self.kline_service = kline_service
        self.notification = notification
        
        self.strategy_name = config['strategy']['name']
        self.symbols = config['symbols']
        self.running = False
        
        # 初始化评分引擎
        from core.scoring.v612 import ScoringEngineV612
        self.scoring_engine = ScoringEngineV612()
        
        logger.info(f"策略初始化完成：{self.strategy_name}")
    
    def run(self):
        """运行策略"""
        self.running = True
        logger.info("策略开始运行")
        
        # 恢复策略状态
        self._restore_state()
        
        # 主循环
        while self.running:
            try:
                self._execute_cycle()
                # 等待下一次执行
                import time
                time.sleep(60)  # 每分钟执行一次
            except Exception as e:
                logger.error(f"执行周期异常：{e}")
    
    def stop(self):
        """停止策略"""
        self.running = False
        self._save_state()
        logger.info("策略已停止")
    
    def _execute_cycle(self):
        """执行一个周期"""
        logger.info(f"开始执行周期：{datetime.now()}")
        
        for symbol in self.symbols:
            try:
                self._analyze_symbol(symbol)
            except Exception as e:
                logger.error(f"分析 {symbol} 异常：{e}")
    
    def _analyze_symbol(self, symbol: str):
        """分析单个交易对"""
        logger.info(f"分析交易对：{symbol}")
        
        # 1. 获取K线数据
        klines = self.kline_service.get_klines(symbol, '1h', limit=100)
        if not klines:
            logger.warning(f"获取K线数据失败：{symbol}")
            return
        
        # 2. 获取技术指标
        indicators = self.kline_service.get_indicators(symbol, '1h')
        if not indicators:
            logger.warning(f"获取技术指标失败：{symbol}")
            return
        
        # 3. 获取资金费率
        funding_rate = self._get_funding_rate(symbol)
        
        # 4. 执行评分
        score_result = self.scoring_engine.score(symbol, {
            'indicators': indicators,
            'funding_rate': funding_rate
        })
        
        logger.info(f"评分结果：{symbol} - 等级={score_result.get('grade')}, 分数={score_result.get('score'):.1f}")
        
        # 5. 交易决策
        self._make_decision(symbol, score_result)
    
    def _get_funding_rate(self, symbol: str) -> float:
        """获取资金费率"""
        try:
            # 从 API 获取资金费率
            # 简化实现，实际需要调用 API
            return 0.0001
        except Exception as e:
            logger.error(f"获取资金费率失败：{e}")
            return 0.0
    
    def _make_decision(self, symbol: str, score_result: Dict[str, Any]):
        """交易决策"""
        grade = score_result.get('grade')
        direction = score_result.get('direction')
        position_ratio = score_result.get('position_ratio', 0)
        
        if grade in ['S', 'A']:
            # 高质量信号，执行交易
            self._execute_trade(symbol, direction, position_ratio, score_result)
        elif grade in ['B']:
            # 中等质量信号，观察
            logger.info(f"中等质量信号，观察：{symbol}")
        else:
            # 低质量信号，跳过
            logger.info(f"低质量信号，跳过：{symbol}")
    
    def _execute_trade(self, symbol: str, direction: str, position_ratio: float, 
                       score_result: Dict[str, Any]):
        """执行交易"""
        logger.info(f"执行交易：{symbol} {direction} 仓位比例={position_ratio:.2%}")
        
        # 1. 检查当前持仓
        current_position = self.binance_api.get_position(symbol)
        if current_position:
            logger.info(f"已有持仓：{current_position['positionAmt']}")
            return
        
        # 2. 计算仓位大小
        balance = self.binance_api.get_umfut_balance('USDT')
        position_margin = Decimal(str(balance)) * Decimal(str(position_ratio))
        
        # 3. 获取当前价格
        current_price = self.binance_api.get_ticker_price(symbol)
        
        # 4. 计算数量
        leverage = self.config['trading']['leverage']
        quantity = (position_margin * leverage) / current_price
        
        # 5. 下单
        side = 'BUY' if direction == 'LONG' else 'SELL'
        order = self.binance_api.place_order(
            symbol=symbol,
            side=side,
            order_type='MARKET',
            quantity=quantity,
            position_side='LONG' if direction == 'LONG' else 'SHORT'
        )
        
        # 6. 保存订单
        order['strategy'] = self.strategy_name
        self.db.save_order(order)
        
        # 7. 发送通知
        self.notification.send_trade_notification({
            'symbol': symbol,
            'side': side,
            'quantity': str(quantity),
            'price': str(current_price),
            'status': order.get('status'),
            'time': datetime.now().isoformat()
        })
        
        logger.info(f"交易执行完成：订单ID={order['orderId']}")
    
    def _restore_state(self):
        """恢复策略状态"""
        state = self.db.get_strategy_state(self.strategy_name, 'main')
        if state:
            logger.info(f"恢复策略状态：{state}")
        else:
            logger.info("无历史状态，从头开始")
    
    def _save_state(self):
        """保存策略状态"""
        state = {
            'last_update': datetime.now().isoformat(),
            'positions': []
        }
        self.db.save_strategy_state(self.strategy_name, 'main', state)
        logger.info("策略状态已保存")
```

### 3.3 第三阶段：配置和部署迁移（2天）

#### 3.3.1 步骤1：创建 Dockerfile

```dockerfile
# strategies/btc_eth/Dockerfile
FROM python:3.10-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/btc_eth/ /app/strategy/

WORKDIR /app/strategy

# 设置环境变量
ENV PYTHONPATH=/app
ENV STRATEGY_NAME=btc_eth

# 启动命令
CMD ["python", "main.py"]
```

#### 3.3.2 步骤2：创建 requirements.txt

```
# strategies/btc_eth/requirements.txt
python-binance>=1.0.19
psycopg2-binary>=2.9.9
pandas>=2.1.4
numpy>=1.26.2
requests>=2.31.0
pyyaml>=6.0.1
python-dotenv>=1.0.0
```

#### 3.3.3 步骤3：更新 docker-compose.yml

```yaml
# docker-compose.yml
version: '3.8'

services:
  btc-eth-strategy:
    build:
      context: .
      dockerfile: strategies/btc_eth/Dockerfile
    container_name: btc-eth-strategy
    restart: unless-stopped
    environment:
      - STRATEGY_NAME=btc_eth
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_SECRET_KEY=${BINANCE_SECRET_KEY}
      - DATABASE_URL=postgresql://bianace_user:Bianace%402024@postgres-db:5432/trading_platform
      - KLINE_SERVICE_URL=http://43.156.242.184:8765/api/v1
      - NOTIFICATION_SERVICE_URL=http://43.156.242.184:8766/api/v1
    volumes:
      - ./logs:/app/logs
      - ./shared:/app/shared
    depends_on:
      - postgres
    networks:
      - trading-network
```

---

## 4. 验收标准

### 4.1 功能验收标准

| 验收项 | 验收标准 | 验证方法 |
|--------|----------|----------|
| K线数据获取 | 能正确获取K线数据 | 单元测试 |
| 技术指标计算 | 指标计算结果正确 | 对比验证 |
| 评分引擎 | 评分结果与原系统一致 | 回测对比 |
| 交易执行 | 订单能正确下单和撤销 | 模拟交易 |
| 通知发送 | 飞书通知正常发送 | 实际测试 |
| 数据库存储 | 数据正确存储和查询 | 数据验证 |
| 策略状态恢复 | 重启后状态正确恢复 | 重启测试 |

### 4.2 性能验收标准

| 指标 | 目标值 | 验证方法 |
|------|--------|----------|
| 策略启动时间 | < 30秒 | 计时测试 |
| K线数据获取延迟 | < 100ms | 性能测试 |
| 评分计算时间 | < 50ms | 性能测试 |
| API调用成功率 | >= 99.5% | 监控统计 |
| 系统可用性 | >= 99.9% | 运行监控 |

### 4.3 回测验证标准

| 指标 | 目标值 | 验证方法 |
|------|--------|----------|
| 回测收益率 | 与原系统一致 | 回测对比 |
| 夏普比率 | >= 1.5 | 回测计算 |
| 最大回撤 | <= 20% | 回测计算 |
| 胜率 | >= 50% | 回测统计 |

---

## 5. 风险与应对

### 5.1 潜在风险

| 风险类型 | 风险描述 | 影响程度 | 发生概率 |
|----------|----------|----------|----------|
| 数据不一致 | 迁移后数据格式不兼容 | 高 | 中 |
| 功能缺失 | 部分功能遗漏 | 高 | 低 |
| 性能下降 | 共享模块性能不如原实现 | 中 | 中 |
| API兼容性 | API 接口变化导致不兼容 | 高 | 低 |
| 部署失败 | Docker 部署配置问题 | 中 | 中 |

### 5.2 应对措施

| 风险 | 应对措施 | 责任人 |
|------|----------|--------|
| 数据不一致 | 编写数据迁移脚本，进行数据校验 | 开发团队 |
| 功能缺失 | 建立功能清单，逐项核对 | 测试团队 |
| 性能下降 | 进行性能测试，优化瓶颈 | 开发团队 |
| API兼容性 | 保持接口兼容，添加适配层 | 开发团队 |
| 部署失败 | 准备详细部署文档，本地预演 | 运维团队 |

### 5.3 回滚方案

**回滚触发条件**:
- 策略运行异常，无法正常交易
- 数据丢失或损坏
- 性能严重下降，影响交易

**回滚步骤**:

1. **停止新系统**
   ```bash
   docker-compose stop btc-eth-strategy
   ```

2. **启动原系统**
   ```bash
   cd /Users/yl/vscode/bianace_btcethbnb_trade
   docker-compose up -d
   ```

3. **数据恢复**
   ```bash
   # 从备份恢复数据库
   pg_restore -d trading_db backup.dump
   ```

4. **验证原系统**
   - 检查策略运行状态
   - 验证交易功能正常
   - 确认通知发送正常

---

## 6. 时间计划

| 阶段 | 任务 | 预计时间 | 负责人 |
|------|------|---------|--------|
| 第一阶段 | 共享模块迁移 | 2天 | 开发团队 |
| 第二阶段 | 策略核心迁移 | 3天 | 开发团队 |
| 第三阶段 | 配置和部署迁移 | 2天 | 运维团队 |
| 测试阶段 | 功能测试和验证 | 2天 | 测试团队 |
| 上线阶段 | 生产环境部署 | 1天 | 运维团队 |

**总计**: 10个工作日

---

## 7. 附录

### 7.1 文件对照表

| 原文件 | 目标文件 | 说明 |
|--------|----------|------|
| `utils/kline_service.py` | `shared/kline_service.py` | K线服务 |
| `utils/lark_notifier.py` | `shared/notification.py` | 通知服务 |
| `utils/binance_trade_api.py` | `shared/binance_api.py` | 交易API |
| `models/database.py` | `shared/database.py` | 数据库服务 |
| `core/scoring/v612.py` | `strategies/btc_eth/strategy.py` | 评分引擎 |
| `config/config.yaml` | `strategies/btc_eth/config.yaml` | 配置文件 |
| `main.py` | `strategies/btc_eth/main.py` | 主程序 |

### 7.2 环境变量对照表

| 原环境变量 | 目标环境变量 | 说明 |
|------------|--------------|------|
| `BINANCE_API_KEY` | `BINANCE_API_KEY` | API Key |
| `BINANCE_SECRET_KEY` | `BINANCE_SECRET_KEY` | Secret Key |
| `DATABASE_URL` | `DATABASE_URL` | 数据库连接 |
| `KLINE_SERVICE_URL` | `KLINE_SERVICE_URL` | K线服务地址 |
| `NOTIFICATION_SERVICE_URL` | `NOTIFICATION_SERVICE_URL` | 通知服务地址 |
| `NOTIFICATION_PROJECT` | `NOTIFICATION_PROJECT` | 项目标识 |

---

**文档结束**
