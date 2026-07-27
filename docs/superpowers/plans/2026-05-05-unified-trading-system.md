# 统一交易系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标:** 构建一个模块化单体的统一交易系统，整合BTC/ETH策略、新币做空策略和网格交易策略，通过共享核心模块实现代码复用，通过Docker容器实现策略独立部署。

**架构:** 模块化单体架构，共享核心模块（Python包）+ 策略独立部署（Docker容器）。核心模块包括币安API封装、K线服务客户端、通知服务客户端、数据库管理等。策略模块独立运行在Docker容器中，互不影响。

**技术栈:** Python 3.10+, PostgreSQL 14+, Docker, FastAPI, Pydantic, asyncpg, APScheduler

---

## 文件结构

```
Binance_quantitative_trading/
├── shared/                          # 共享核心模块
│   ├── __init__.py
│   ├── binance_api.py              # 币安API封装
│   ├── kline_service.py            # K线服务客户端
│   ├── notification.py             # 通知服务客户端
│   ├── database.py                 # 数据库管理
│   ├── indicators.py               # 技术指标计算
│   └── utils.py                    # 工具函数
│
├── strategies/                      # 策略模块
│   ├── btc_eth/                    # BTC/ETH策略
│   │   ├── __init__.py
│   │   ├── main.py                 # 主入口
│   │   ├── strategy.py             # 策略逻辑
│   │   ├── config.yaml             # 配置文件
│   │   └── Dockerfile              # Docker配置
│   │
│   ├── new_coin/                   # 新币做空策略
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── strategy.py
│   │   ├── config.yaml
│   │   └── Dockerfile
│   │
│   └── grid/                       # 网格交易策略
│       ├── __init__.py
│       ├── main.py
│       ├── strategy.py
│       ├── config.yaml
│       └── Dockerfile
│
├── data/                           # 数据目录（PostgreSQL数据库）
│   └── migrations/                # 数据库迁移脚本
│       ├── 001_initial_schema.sql
│       └── 002_add_indexes.sql
│
├── logs/                           # 日志目录
│   ├── btc_eth/
│   ├── new_coin/
│   └── grid/
│
├── docs/                           # 文档目录
│   ├── architecture/
│   ├── migration/
│   └── deployment/
│
├── tests/                          # 测试目录
│   ├── test_shared/
│   └── test_strategies/
│
├── .env.example                    # 环境变量模板
├── requirements.txt                # Python依赖
├── docker-compose.yml              # Docker编排
└── README.md                       # 项目说明
```

---

## 阶段一：共享核心模块开发（5天）

### Task 1: 创建项目基础结构

**文件:**
- Create: `shared/__init__.py`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `README.md`

- [ ] **Step 1: 创建共享模块初始化文件**

```python
"""
共享核心模块
提供统一的API封装、数据服务、通知服务等
"""

__version__ = "1.0.0"
__author__ = "Trading System Team"

from .binance_api import BinanceClient
from .kline_service import KLineService
from .notification import NotificationClient
from .database import DatabaseManager
from .indicators import TechnicalIndicators

__all__ = [
    "BinanceClient",
    "KLineService",
    "NotificationClient",
    "DatabaseManager",
    "TechnicalIndicators",
]
```

- [ ] **Step 2: 创建requirements.txt**

```txt
# 核心依赖
python-binance==1.0.19
aiohttp==3.9.1
asyncpg==0.29.0
pydantic==2.5.0
pydantic-settings==2.1.0
PyYAML==6.0.1
python-dotenv==1.0.0

# 技术指标
pandas==2.2.1
numpy==1.26.4
TA-Lib==0.4.28

# 任务调度
APScheduler==3.10.4

# 日志
structlog==24.1.0

# 测试
pytest==8.0.0
pytest-asyncio==0.23.3
pytest-cov==4.1.0
```

- [ ] **Step 3: 创建.env.example**

```bash
# 数据库配置
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=trading_platform
DATABASE_USER=trading_user
DATABASE_PASSWORD=your_password_here

# Binance API配置
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_TESTNET=false

# K线服务配置
KLINE_SERVICE_URL=http://43.156.242.184:8765/api/v1
KLINE_SERVICE_TIMEOUT=10

# 通知服务配置
NOTIFICATION_SERVICE_URL=http://43.156.242.184:8766/api/v1
FEISHU_WEBHOOK=your_webhook_url_here

# 日志配置
LOG_LEVEL=INFO
LOG_FORMAT=json

# 策略配置
STRATEGY_NAME=btc_eth
STRATEGY_ENV=production
```

- [ ] **Step 4: 创建README.md**

```markdown
# Binance_quantitative_trading

统一交易系统 - 整合多个交易策略的模块化平台

## 项目结构

- `shared/` - 共享核心模块
- `strategies/` - 策略模块（BTC/ETH、新币做空、网格交易）
- `docs/` - 项目文档
- `tests/` - 测试代码

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`
2. 配置环境变量：`cp .env.example .env`
3. 运行策略：`python strategies/btc_eth/main.py`

## 文档

- [系统架构设计](docs/architecture/系统架构设计.md)
- [部署指南](docs/deployment/部署指南.md)
- [迁移方案](docs/migration/README.md)
```

- [ ] **Step 5: 提交基础结构**

```bash
git add shared/__init__.py requirements.txt .env.example README.md
git commit -m "feat: 初始化项目基础结构"
```

---

### Task 2: 实现币安API封装

**文件:**
- Create: `shared/binance_api.py`
- Create: `tests/test_shared/test_binance_api.py`

- [ ] **Step 1: 编写币安API测试**

```python
"""
测试币安API封装
"""
import pytest
from decimal import Decimal
from shared.binance_api import BinanceClient


@pytest.mark.asyncio
async def test_get_account_balance():
    """测试获取账户余额"""
    client = BinanceClient(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True
    )
    
    balance = await client.get_account_balance()
    
    assert balance is not None
    assert "USDT" in balance


@pytest.mark.asyncio
async def test_place_order_validation():
    """测试下单参数验证"""
    client = BinanceClient(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True
    )
    
    with pytest.raises(ValueError):
        await client.place_order(
            symbol="BTCUSDT",
            side="INVALID",
            quantity=Decimal("0.001")
        )
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_shared/test_binance_api.py -v
```

预期输出：FAIL (BinanceClient未实现)

- [ ] **Step 3: 实现币安API封装**

```python
"""
币安API封装
提供统一的API调用接口，包含频率控制、错误重试等
"""
import asyncio
import time
from typing import Dict, List, Optional, Any
from decimal import Decimal
import aiohttp
import hmac
import hashlib
from urllib.parse import urlencode
import structlog

from .utils import retry_on_failure


logger = structlog.get_logger()


class BinanceAPIError(Exception):
    """币安API异常"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class RateLimiter:
    """频率控制器"""
    
    def __init__(self, max_requests: int = 1200, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self.requests = []
    
    async def acquire(self):
        """获取请求许可"""
        now = time.time()
        
        # 清理过期请求
        self.requests = [r for r in self.requests if now - r < self.window]
        
        # 检查是否超过限制
        if len(self.requests) >= self.max_requests:
            wait_time = self.window - (now - self.requests[0])
            logger.warning(f"频率限制，等待 {wait_time:.2f} 秒")
            await asyncio.sleep(wait_time)
        
        # 记录请求时间
        self.requests.append(now)


class BinanceClient:
    """币安API客户端"""
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        base_url: str = None
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        # 设置API地址
        if base_url:
            self.base_url = base_url
        elif testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"
        
        # 频率控制器
        self.rate_limiter = RateLimiter()
        
        # HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        logger.info(
            "币安客户端初始化",
            testnet=testnet,
            base_url=self.base_url
        )
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
    
    async def _init_session(self):
        """初始化HTTP会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "Content-Type": "application/json"
                }
            )
    
    async def close(self):
        """关闭客户端"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """生成请求签名"""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    @retry_on_failure(max_retries=3, delay=1.0)
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True
    ) -> Dict:
        """发送API请求"""
        await self._init_session()
        
        # 频率控制
        await self.rate_limiter.acquire()
        
        # 准备参数
        if params is None:
            params = {}
        
        # 添加签名
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
        
        # 构建URL
        url = f"{self.base_url}{endpoint}"
        
        # 发送请求
        async with self.session.request(method, url, params=params) as response:
            data = await response.json()
            
            if response.status != 200:
                code = data.get('code', response.status)
                message = data.get('msg', response.reason)
                raise BinanceAPIError(code, message)
            
            return data
    
    async def get_account_balance(self) -> Dict[str, Decimal]:
        """获取账户余额"""
        data = await self._request("GET", "/fapi/v2/balance")
        
        balance = {}
        for asset in data:
            symbol = asset['asset']
            available = Decimal(asset['availableBalance'])
            if available > 0:
                balance[symbol] = available
        
        return balance
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        order_type: str = "MARKET",
        **kwargs
    ) -> Dict:
        """下单"""
        # 参数验证
        if side not in ["BUY", "SELL"]:
            raise ValueError(f"无效的订单方向: {side}")
        
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": str(quantity)
        }
        
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("限价单必须提供价格")
            params["price"] = str(price)
            params["timeInForce"] = kwargs.get("timeInForce", "GTC")
        
        # 添加额外参数
        for key, value in kwargs.items():
            if key not in params:
                params[key] = str(value)
        
        return await self._request("POST", "/fapi/v1/order", params)
    
    async def get_position(self, symbol: Optional[str] = None) -> List[Dict]:
        """查询持仓"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        
        return await self._request("GET", "/fapi/v2/positionRisk", params)
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None
    ) -> Dict:
        """撤销订单"""
        params = {"symbol": symbol}
        
        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("必须提供 orderId 或 clientOrderId")
        
        return await self._request("DELETE", "/fapi/v1/order", params)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_shared/test_binance_api.py -v
```

预期输出：PASS

- [ ] **Step 5: 提交币安API封装**

```bash
git add shared/binance_api.py tests/test_shared/test_binance_api.py
git commit -m "feat: 实现币安API封装，包含频率控制和错误重试"
```

---

### Task 3: 实现K线服务客户端

**文件:**
- Create: `shared/kline_service.py`
- Create: `tests/test_shared/test_kline_service.py`

- [ ] **Step 1: 编写K线服务测试**

```python
"""
测试K线服务客户端
"""
import pytest
from shared.kline_service import KLineService


@pytest.mark.asyncio
async def test_get_klines():
    """测试获取K线数据"""
    service = KLineService(
        service_url="http://43.156.242.184:8765/api/v1",
        timeout=10
    )
    
    klines = await service.get_klines(
        symbol="BTCUSDT",
        interval="1h",
        limit=100
    )
    
    assert klines is not None
    assert len(klines) > 0
    assert "timestamp" in klines[0]
    assert "open" in klines[0]
    assert "close" in klines[0]


@pytest.mark.asyncio
async def test_get_multi_timeframe_data():
    """测试获取多时间框架数据"""
    service = KLineService(
        service_url="http://43.156.242.184:8765/api/v1",
        timeout=10
    )
    
    data = await service.get_multi_timeframe_data(
        symbol="BTCUSDT",
        intervals=["1h", "4h", "1d"]
    )
    
    assert "1h" in data
    assert "4h" in data
    assert "1d" in data
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_shared/test_kline_service.py -v
```

预期输出：FAIL (KLineService未实现)

- [ ] **Step 3: 实现K线服务客户端**

```python
"""
K线服务客户端
从通用K线服务获取数据，支持失败重连
"""
import asyncio
from typing import Dict, List, Optional
import aiohttp
import structlog

from .utils import retry_on_failure


logger = structlog.get_logger()


class KLineServiceError(Exception):
    """K线服务异常"""
    pass


class KLineService:
    """K线服务客户端"""
    
    def __init__(
        self,
        service_url: str,
        timeout: int = 10,
        max_retries: int = 3
    ):
        self.service_url = service_url
        self.timeout = timeout
        self.max_retries = max_retries
        
        # HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        logger.info(
            "K线服务客户端初始化",
            service_url=service_url,
            timeout=timeout
        )
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
    
    async def _init_session(self):
        """初始化HTTP会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self):
        """关闭客户端"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    @retry_on_failure(max_retries=3, delay=1.0)
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            symbol: 交易对
            interval: K线周期
            limit: 数量限制
        
        Returns:
            K线数据列表
        """
        await self._init_session()
        
        url = f"{self.service_url}/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        logger.debug(
            "获取K线数据",
            symbol=symbol,
            interval=interval,
            limit=limit
        )
        
        try:
            async with self.session.get(url, params=params) as response:
                data = await response.json()
                
                if response.status != 200:
                    raise KLineServiceError(
                        f"K线服务请求失败: {response.status}"
                    )
                
                if data.get('code') != 0:
                    raise KLineServiceError(
                        data.get('message', '未知错误')
                    )
                
                klines = data['data']['klines']
                
                logger.info(
                    "K线数据获取成功",
                    symbol=symbol,
                    interval=interval,
                    count=len(klines)
                )
                
                return klines
        
        except aiohttp.ClientError as e:
            logger.error(
                "K线服务连接失败",
                error=str(e)
            )
            raise KLineServiceError(f"连接失败: {e}")
    
    async def get_multi_timeframe_data(
        self,
        symbol: str,
        intervals: List[str] = ["1h", "4h", "1d"]
    ) -> Dict[str, List[Dict]]:
        """
        获取多时间框架数据
        
        Args:
            symbol: 交易对
            intervals: 时间框架列表
        
        Returns:
            {interval: klines}
        """
        tasks = [
            self.get_klines(symbol, interval, self._get_limit(interval))
            for interval in intervals
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        data = {}
        for interval, result in zip(intervals, results):
            if isinstance(result, Exception):
                logger.error(
                    "获取多时间框架数据失败",
                    symbol=symbol,
                    interval=interval,
                    error=str(result)
                )
            else:
                data[interval] = result
        
        return data
    
    def _get_limit(self, interval: str) -> int:
        """根据周期获取K线数量"""
        limits = {
            '1d': 180,
            '4h': 1080,
            '1h': 4320,
            '15m': 17280,
            '5m': 51840,
            '1m': 259200
        }
        return limits.get(interval, 100)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_shared/test_kline_service.py -v
```

预期输出：PASS

- [ ] **Step 5: 提交K线服务客户端**

```bash
git add shared/kline_service.py tests/test_shared/test_kline_service.py
git commit -m "feat: 实现K线服务客户端，支持失败重连"
```

---

### Task 4: 实现通知服务客户端

**文件:**
- Create: `shared/notification.py`
- Create: `tests/test_shared/test_notification.py`

- [ ] **Step 1: 编写通知服务测试**

```python
"""
测试通知服务客户端
"""
import pytest
from shared.notification import NotificationClient


@pytest.mark.asyncio
async def test_send_notification():
    """测试发送通知"""
    client = NotificationClient(
        service_url="http://43.156.242.184:8766/api/v1"
    )
    
    result = await client.send(
        message="测试通知",
        level="info",
        project="test"
    )
    
    assert result is True


@pytest.mark.asyncio
async def test_send_trade_notification():
    """测试发送交易通知"""
    client = NotificationClient(
        service_url="http://43.156.242.184:8766/api/v1"
    )
    
    result = await client.send_trade_notification(
        strategy="btc_eth",
        symbol="BTCUSDT",
        action="BUY",
        quantity=0.001,
        price=50000.0
    )
    
    assert result is True
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_shared/test_notification.py -v
```

预期输出：FAIL (NotificationClient未实现)

- [ ] **Step 3: 实现通知服务客户端**

```python
"""
通知服务客户端
发送飞书通知、告警等
"""
from typing import Optional
import aiohttp
import structlog


logger = structlog.get_logger()


class NotificationError(Exception):
    """通知服务异常"""
    pass


class NotificationClient:
    """通知服务客户端"""
    
    def __init__(
        self,
        service_url: str,
        timeout: int = 10
    ):
        self.service_url = service_url
        self.timeout = timeout
        
        # HTTP会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        logger.info(
            "通知服务客户端初始化",
            service_url=service_url
        )
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
    
    async def _init_session(self):
        """初始化HTTP会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self):
        """关闭客户端"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def send(
        self,
        message: str,
        level: str = "info",
        project: str = "default"
    ) -> bool:
        """
        发送通知
        
        Args:
            message: 消息内容
            level: 消息级别 (info, warning, error)
            project: 项目名称
        
        Returns:
            是否发送成功
        """
        await self._init_session()
        
        url = f"{self.service_url}/notification/send"
        payload = {
            "message": message,
            "level": level,
            "project": project
        }
        
        logger.debug(
            "发送通知",
            message=message,
            level=level,
            project=project
        )
        
        try:
            async with self.session.post(url, json=payload) as response:
                data = await response.json()
                
                if response.status != 200:
                    raise NotificationError(
                        f"通知服务请求失败: {response.status}"
                    )
                
                if data.get('code') != 0:
                    raise NotificationError(
                        data.get('message', '未知错误')
                    )
                
                logger.info(
                    "通知发送成功",
                    message=message
                )
                
                return True
        
        except aiohttp.ClientError as e:
            logger.error(
                "通知服务连接失败",
                error=str(e)
            )
            return False
    
    async def send_trade_notification(
        self,
        strategy: str,
        symbol: str,
        action: str,
        quantity: float,
        price: float,
        **kwargs
    ) -> bool:
        """
        发送交易通知
        
        Args:
            strategy: 策略名称
            symbol: 交易对
            action: 交易动作
            quantity: 数量
            price: 价格
        
        Returns:
            是否发送成功
        """
        message = f"""
【交易通知】
策略: {strategy}
交易对: {symbol}
动作: {action}
数量: {quantity}
价格: {price}
"""
        
        if kwargs:
            message += "\n额外信息:\n"
            for key, value in kwargs.items():
                message += f"- {key}: {value}\n"
        
        return await self.send(
            message=message,
            level="info",
            project=strategy
        )
    
    async def send_alert(
        self,
        title: str,
        message: str,
        level: str = "warning"
    ) -> bool:
        """
        发送告警
        
        Args:
            title: 告警标题
            message: 告警消息
            level: 告警级别
        
        Returns:
            是否发送成功
        """
        alert_message = f"""
【{title}】
{message}
"""
        
        return await self.send(
            message=alert_message,
            level=level,
            project="alert"
        )
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_shared/test_notification.py -v
```

预期输出：PASS

- [ ] **Step 5: 提交通知服务客户端**

```bash
git add shared/notification.py tests/test_shared/test_notification.py
git commit -m "feat: 实现通知服务客户端"
```

---

### Task 5: 实现数据库管理

**文件:**
- Create: `shared/database.py`
- Create: `tests/test_shared/test_database.py`

- [ ] **Step 1: 编写数据库管理测试**

```python
"""
测试数据库管理
"""
import pytest
from shared.database import DatabaseManager


@pytest.mark.asyncio
async def test_database_connection():
    """测试数据库连接"""
    db = DatabaseManager(
        host="localhost",
        port=5432,
        database="test_db",
        user="test_user",
        password="test_password"
    )
    
    await db.connect()
    
    result = await db.execute("SELECT 1")
    
    assert result is not None
    
    await db.disconnect()


@pytest.mark.asyncio
async def test_insert_and_query():
    """测试插入和查询"""
    db = DatabaseManager(
        host="localhost",
        port=5432,
        database="test_db",
        user="test_user",
        password="test_password"
    )
    
    await db.connect()
    
    # 插入测试数据
    await db.execute(
        "INSERT INTO test_table (name) VALUES ($1)",
        "test_name"
    )
    
    # 查询测试数据
    result = await db.fetch_one(
        "SELECT name FROM test_table WHERE name = $1",
        "test_name"
    )
    
    assert result is not None
    assert result['name'] == "test_name"
    
    await db.disconnect()
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_shared/test_database.py -v
```

预期输出：FAIL (DatabaseManager未实现)

- [ ] **Step 3: 实现数据库管理**

```python
"""
数据库管理
PostgreSQL连接池管理
"""
from typing import Optional, List, Dict, Any
import asyncpg
import structlog


logger = structlog.get_logger()


class DatabaseError(Exception):
    """数据库异常"""
    pass


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        min_pool_size: int = 5,
        max_pool_size: int = 20
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        
        # 连接池
        self.pool: Optional[asyncpg.Pool] = None
        
        logger.info(
            "数据库管理器初始化",
            host=host,
            port=port,
            database=database
        )
    
    async def connect(self):
        """建立数据库连接池"""
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                min_size=self.min_pool_size,
                max_size=self.max_pool_size
            )
            
            logger.info(
                "数据库连接池已建立",
                min_size=self.min_pool_size,
                max_size=self.max_pool_size
            )
    
    async def disconnect(self):
        """关闭数据库连接池"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            
            logger.info("数据库连接池已关闭")
    
    async def execute(
        self,
        query: str,
        *args,
        **kwargs
    ) -> str:
        """
        执行SQL语句（INSERT, UPDATE, DELETE）
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            执行结果
        """
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *args, **kwargs)
            
            logger.debug(
                "SQL执行成功",
                query=query[:100],
                result=result
            )
            
            return result
    
    async def fetch_one(
        self,
        query: str,
        *args,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        查询单条记录
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            查询结果（字典）
        """
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args, **kwargs)
            
            if row:
                return dict(row)
            
            return None
    
    async def fetch_all(
        self,
        query: str,
        *args,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        查询多条记录
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            查询结果列表
        """
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args, **kwargs)
            
            return [dict(row) for row in rows]
    
    async def execute_transaction(
        self,
        queries: List[tuple]
    ) -> bool:
        """
        执行事务
        
        Args:
            queries: 查询列表 [(query, args), ...]
        
        Returns:
            是否成功
        """
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for query, args in queries:
                    await conn.execute(query, *args)
        
        logger.info(
            "事务执行成功",
            query_count=len(queries)
        )
        
        return True
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_shared/test_database.py -v
```

预期输出：PASS

- [ ] **Step 5: 提交数据库管理**

```bash
git add shared/database.py tests/test_shared/test_database.py
git commit -m "feat: 实现数据库管理，支持连接池"
```

---

### Task 6: 实现技术指标计算

**文件:**
- Create: `shared/indicators.py`
- Create: `tests/test_shared/test_indicators.py`

- [ ] **Step 1: 编写技术指标测试**

```python
"""
测试技术指标计算
"""
import pytest
import pandas as pd
from shared.indicators import TechnicalIndicators


def test_calculate_ma():
    """测试计算移动平均线"""
    data = pd.DataFrame({
        'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
    })
    
    ma = TechnicalIndicators.calculate_ma(data, period=5)
    
    assert len(ma) == len(data)
    assert ma.iloc[-1] == (105 + 106 + 107 + 108 + 109) / 5


def test_calculate_rsi():
    """测试计算RSI"""
    data = pd.DataFrame({
        'close': [100, 101, 102, 103, 102, 101, 100, 101, 102, 103,
                  104, 105, 106, 107, 108]
    })
    
    rsi = TechnicalIndicators.calculate_rsi(data, period=14)
    
    assert len(rsi) == len(data)
    assert 0 <= rsi.iloc[-1] <= 100
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_shared/test_indicators.py -v
```

预期输出：FAIL (TechnicalIndicators未实现)

- [ ] **Step 3: 实现技术指标计算**

```python
"""
技术指标计算
提供常用的技术指标计算方法
"""
from typing import List
import pandas as pd
import numpy as np
import talib
import structlog


logger = structlog.get_logger()


class TechnicalIndicators:
    """技术指标计算器"""
    
    @staticmethod
    def calculate_ma(data: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算移动平均线
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            MA序列
        """
        return data['close'].rolling(window=period).mean()
    
    @staticmethod
    def calculate_ema(data: pd.DataFrame, period: int = 20) -> pd.Series:
        """
        计算指数移动平均线
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            EMA序列
        """
        return data['close'].ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_rsi(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算RSI
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
        
        Returns:
            RSI序列
        """
        return talib.RSI(data['close'], timeperiod=period)
    
    @staticmethod
    def calculate_macd(
        data: pd.DataFrame,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> tuple:
        """
        计算MACD
        
        Args:
            data: 包含 'close' 列的DataFrame
            fast_period: 快线周期
            slow_period: 慢线周期
            signal_period: 信号线周期
        
        Returns:
            (MACD, Signal, Histogram)
        """
        macd, signal, hist = talib.MACD(
            data['close'],
            fastperiod=fast_period,
            slowperiod=slow_period,
            signalperiod=signal_period
        )
        
        return macd, signal, hist
    
    @staticmethod
    def calculate_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算ATR
        
        Args:
            data: 包含 'high', 'low', 'close' 列的DataFrame
            period: 周期
        
        Returns:
            ATR序列
        """
        return talib.ATR(
            data['high'],
            data['low'],
            data['close'],
            timeperiod=period
        )
    
    @staticmethod
    def calculate_adx(data: pd.DataFrame, period: int = 14) -> pd.Series:
        """
        计算ADX
        
        Args:
            data: 包含 'high', 'low', 'close' 列的DataFrame
            period: 周期
        
        Returns:
            ADX序列
        """
        return talib.ADX(
            data['high'],
            data['low'],
            data['close'],
            timeperiod=period
        )
    
    @staticmethod
    def calculate_bollinger_bands(
        data: pd.DataFrame,
        period: int = 20,
        std_dev: int = 2
    ) -> tuple:
        """
        计算布林带
        
        Args:
            data: 包含 'close' 列的DataFrame
            period: 周期
            std_dev: 标准差倍数
        
        Returns:
            (Upper, Middle, Lower)
        """
        upper, middle, lower = talib.BBANDS(
            data['close'],
            timeperiod=period,
            nbdevup=std_dev,
            nbdevdn=std_dev
        )
        
        return upper, middle, lower
    
    @staticmethod
    def calculate_all(data: pd.DataFrame) -> dict:
        """
        计算所有常用指标
        
        Args:
            data: K线数据
        
        Returns:
            指标字典
        """
        indicators = {}
        
        # MA
        for period in [7, 21, 55]:
            indicators[f'MA{period}'] = TechnicalIndicators.calculate_ma(data, period)
        
        # EMA
        for period in [12, 26]:
            indicators[f'EMA{period}'] = TechnicalIndicators.calculate_ema(data, period)
        
        # RSI
        indicators['RSI'] = TechnicalIndicators.calculate_rsi(data)
        
        # MACD
        macd, signal, hist = TechnicalIndicators.calculate_macd(data)
        indicators['MACD'] = macd
        indicators['MACD_Signal'] = signal
        indicators['MACD_Hist'] = hist
        
        # ATR
        indicators['ATR'] = TechnicalIndicators.calculate_atr(data)
        
        # ADX
        indicators['ADX'] = TechnicalIndicators.calculate_adx(data)
        
        # Bollinger Bands
        upper, middle, lower = TechnicalIndicators.calculate_bollinger_bands(data)
        indicators['BB_Upper'] = upper
        indicators['BB_Middle'] = middle
        indicators['BB_Lower'] = lower
        
        logger.info(
            "技术指标计算完成",
            indicators_count=len(indicators)
        )
        
        return indicators
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_shared/test_indicators.py -v
```

预期输出：PASS

- [ ] **Step 5: 提交技术指标计算**

```bash
git add shared/indicators.py tests/test_shared/test_indicators.py
git commit -m "feat: 实现技术指标计算，支持MA/EMA/RSI/MACD/ATR/ADX等"
```

---

### Task 7: 实现工具函数

**文件:**
- Create: `shared/utils.py`

- [ ] **Step 1: 实现工具函数**

```python
"""
工具函数
提供重试、日志等通用功能
"""
import asyncio
import functools
from typing import Callable, Any
import structlog


logger = structlog.get_logger()


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 初始延迟（秒）
        backoff: 退避系数
        exceptions: 要捕获的异常类型
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        logger.error(
                            "重试次数已达上限",
                            function=func.__name__,
                            attempts=attempt,
                            error=str(e)
                        )
                        raise
                    
                    logger.warning(
                        "操作失败，准备重试",
                        function=func.__name__,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=current_delay,
                        error=str(e)
                    )
                    
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        
        return wrapper
    
    return decorator


def setup_logging(level: str = "INFO", format: str = "json"):
    """
    配置日志
    
    Args:
        level: 日志级别
        format: 日志格式 (json, text)
    """
    import logging
    import sys
    
    # 配置structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if format == "json" else structlog.dev.ConsoleRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # 配置标准库logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper())
    )
```

- [ ] **Step 2: 提交工具函数**

```bash
git add shared/utils.py
git commit -m "feat: 实现工具函数，包含重试装饰器和日志配置"
```

---

## 阶段二：BTC/ETH策略迁移（5天）

### Task 8: 创建BTC/ETH策略基础结构

**文件:**
- Create: `strategies/btc_eth/__init__.py`
- Create: `strategies/btc_eth/config.yaml`
- Create: `strategies/btc_eth/Dockerfile`

- [ ] **Step 1: 创建策略初始化文件**

```python
"""
BTC/ETH/BNB交易策略
基于评分引擎的趋势跟踪策略
"""

__version__ = "1.0.0"
__strategy_name__ = "btc_eth_trend"

from .strategy import BTCEthStrategy
from .main import main

__all__ = ["BTCEthStrategy", "main"]
```

- [ ] **Step 2: 创建策略配置文件**

```yaml
strategy:
  name: btc_eth_trend
  version: "1.0.0"
  
  symbols:
    - BTCUSDT
    - ETHUSDT
    - BNBUSDT
  
  timeframes:
    - 1h
    - 4h
    - 1d
  
  schedule:
    cron: "0 * * * *"  # 每小时整点执行
  
  risk:
    max_position_size: 0.1  # 最大仓位比例
    max_daily_trades: 4
    stop_loss_atr_multiplier: 2.0
    take_profit_atr_multiplier: 2.5
    
  scoring:
    min_score: 75
    grade_thresholds:
      S: 90
      A: 80
      B: 70
      C: 60

binance:
  leverage:
    S: 5
    A: 4
    B: 3
    C: 2
  
  position_ratio:
    S: 0.3
    A: 0.25
    B: 0.2
    C: 0.15

notification:
  enabled: true
  levels:
    - info
    - warning
    - error
```

- [ ] **Step 3: 创建Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制共享模块
COPY shared/ /app/shared/

# 复制策略代码
COPY strategies/btc_eth/ /app/strategies/btc_eth/

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 设置环境变量
ENV STRATEGY_NAME=btc_eth
ENV PYTHONUNBUFFERED=1

# 运行策略
CMD ["python", "strategies/btc_eth/main.py"]
```

- [ ] **Step 4: 提交BTC/ETH策略基础结构**

```bash
git add strategies/btc_eth/
git commit -m "feat: 创建BTC/ETH策略基础结构"
```

---

### Task 9: 实现BTC/ETH策略主逻辑

**文件:**
- Create: `strategies/btc_eth/strategy.py`
- Create: `strategies/btc_eth/main.py`

- [ ] **Step 1: 实现策略类**

```python
"""
BTC/ETH策略逻辑
"""
from typing import Dict, List, Optional
from decimal import Decimal
import pandas as pd
import structlog

from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.indicators import TechnicalIndicators


logger = structlog.get_logger()


class BTCEthStrategy:
    """BTC/ETH/BNB交易策略"""
    
    def __init__(
        self,
        config: Dict,
        binance_client: BinanceClient,
        kline_service: KLineService,
        notification_client: NotificationClient
    ):
        self.config = config
        self.binance = binance_client
        self.kline_service = kline_service
        self.notification = notification_client
        
        self.symbols = config['strategy']['symbols']
        self.timeframes = config['strategy']['timeframes']
        
        logger.info(
            "BTC/ETH策略初始化",
            symbols=self.symbols,
            timeframes=self.timeframes
        )
    
    async def analyze(self, symbol: str) -> Optional[Dict]:
        """
        分析市场数据，生成交易信号
        
        Args:
            symbol: 交易对
        
        Returns:
            交易信号或None
        """
        logger.info(f"开始分析 {symbol}")
        
        # 1. 获取多时间框架数据
        klines = await self.kline_service.get_multi_timeframe_data(
            symbol=symbol,
            intervals=self.timeframes
        )
        
        if not klines:
            logger.warning(f"{symbol} 获取K线数据失败")
            return None
        
        # 2. 计算技术指标
        indicators = {}
        for timeframe, data in klines.items():
            df = pd.DataFrame(data)
            indicators[timeframe] = TechnicalIndicators.calculate_all(df)
        
        # 3. 评分计算
        score = self._calculate_score(indicators)
        
        if score < self.config['strategy']['scoring']['min_score']:
            logger.info(f"{symbol} 评分 {score} < 最低评分，跳过")
            return None
        
        # 4. 确定信号等级
        grade = self._determine_grade(score)
        
        # 5. 计算方向
        direction = self._determine_direction(indicators)
        
        # 6. 生成信号
        signal = {
            'symbol': symbol,
            'direction': direction,
            'grade': grade,
            'score': score,
            'entry_price': Decimal(str(klines['1h'][-1]['close'])),
            'timestamp': pd.Timestamp.now()
        }
        
        logger.info(
            f"{symbol} 生成交易信号",
            direction=direction,
            grade=grade,
            score=score
        )
        
        return signal
    
    def _calculate_score(self, indicators: Dict) -> float:
        """计算评分"""
        score = 0.0
        
        # 趋势评分（40分）
        trend_score = self._calculate_trend_score(indicators)
        score += trend_score * 0.4
        
        # 动量评分（30分）
        momentum_score = self._calculate_momentum_score(indicators)
        score += momentum_score * 0.3
        
        # 波动率评分（20分）
        volatility_score = self._calculate_volatility_score(indicators)
        score += volatility_score * 0.2
        
        # 成交量评分（10分）
        volume_score = self._calculate_volume_score(indicators)
        score += volume_score * 0.1
        
        return score
    
    def _calculate_trend_score(self, indicators: Dict) -> float:
        """计算趋势评分"""
        score = 0.0
        
        # 使用1h和4h的MA判断趋势
        if '1h' in indicators and '4h' in indicators:
            ma21_1h = indicators['1h']['MA21'].iloc[-1]
            ma55_1h = indicators['1h']['MA55'].iloc[-1]
            
            if ma21_1h > ma55_1h:
                score += 50  # 上升趋势
            else:
                score += 30  # 下降趋势
        
        return score
    
    def _calculate_momentum_score(self, indicators: Dict) -> float:
        """计算动量评分"""
        score = 0.0
        
        if '1h' in indicators:
            rsi = indicators['1h']['RSI'].iloc[-1]
            
            if 30 < rsi < 70:
                score += 50
            elif rsi < 30:
                score += 70  # 超卖
            elif rsi > 70:
                score += 30  # 超买
        
        return score
    
    def _calculate_volatility_score(self, indicators: Dict) -> float:
        """计算波动率评分"""
        score = 50.0  # 默认中等波动
        
        if '1h' in indicators:
            adx = indicators['1h']['ADX'].iloc[-1]
            
            if adx > 25:
                score += 20  # 强趋势
            elif adx < 20:
                score -= 20  # 弱趋势
        
        return max(0, min(100, score))
    
    def _calculate_volume_score(self, indicators: Dict) -> float:
        """计算成交量评分"""
        # 简化实现，返回中等评分
        return 50.0
    
    def _determine_grade(self, score: float) -> str:
        """确定信号等级"""
        thresholds = self.config['strategy']['scoring']['grade_thresholds']
        
        if score >= thresholds['S']:
            return 'S'
        elif score >= thresholds['A']:
            return 'A'
        elif score >= thresholds['B']:
            return 'B'
        else:
            return 'C'
    
    def _determine_direction(self, indicators: Dict) -> str:
        """确定交易方向"""
        # 简化实现，基于MA判断
        if '1h' in indicators:
            ma21 = indicators['1h']['MA21'].iloc[-1]
            ma55 = indicators['1h']['MA55'].iloc[-1]
            
            if ma21 > ma55:
                return 'LONG'
            else:
                return 'SHORT'
        
        return 'LONG'  # 默认做多
```

- [ ] **Step 2: 实现主入口**

```python
"""
BTC/ETH策略主入口
"""
import asyncio
import os
from datetime import datetime
import yaml
import structlog

from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.utils import setup_logging
from .strategy import BTCEthStrategy


logger = structlog.get_logger()


async def main():
    """主函数"""
    # 配置日志
    setup_logging(level="INFO", format="json")
    
    logger.info("BTC/ETH策略启动")
    
    # 加载配置
    config_path = os.path.join(
        os.path.dirname(__file__),
        "config.yaml"
    )
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    # 初始化客户端
    binance_client = BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    )
    
    kline_service = KLineService(
        service_url=os.getenv("KLINE_SERVICE_URL"),
        timeout=10
    )
    
    notification_client = NotificationClient(
        service_url=os.getenv("NOTIFICATION_SERVICE_URL")
    )
    
    # 创建策略实例
    strategy = BTCEthStrategy(
        config=config,
        binance_client=binance_client,
        kline_service=kline_service,
        notification_client=notification_client
    )
    
    # 执行策略
    async with binance_client, kline_service, notification_client:
        for symbol in strategy.symbols:
            try:
                signal = await strategy.analyze(symbol)
                
                if signal:
                    # 发送通知
                    await notification_client.send_trade_notification(
                        strategy="btc_eth",
                        symbol=signal['symbol'],
                        action=signal['direction'],
                        quantity=0.001,  # 示例数量
                        price=float(signal['entry_price']),
                        grade=signal['grade'],
                        score=signal['score']
                    )
            
            except Exception as e:
                logger.error(
                    f"策略执行失败: {symbol}",
                    error=str(e),
                    exc_info=True
                )
    
    logger.info("BTC/ETH策略执行完成")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: 提交BTC/ETH策略主逻辑**

```bash
git add strategies/btc_eth/strategy.py strategies/btc_eth/main.py
git commit -m "feat: 实现BTC/ETH策略主逻辑"
```

---

## 阶段三：Docker部署配置（2天）

### Task 10: 创建Docker Compose配置

**文件:**
- Create: `docker-compose.yml`

- [ ] **Step 1: 创建docker-compose.yml**

```yaml
version: '3.8'

services:
  # BTC/ETH策略容器
  btc-eth-strategy:
    build:
      context: .
      dockerfile: strategies/btc_eth/Dockerfile
    container_name: btc-eth-strategy
    restart: unless-stopped
    env_file:
      - .env
    environment:
      - STRATEGY_NAME=btc_eth
    volumes:
      - ./logs/btc_eth:/app/logs
      - ./data/btc_eth:/app/data
    networks:
      - trading-network
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

networks:
  trading-network:
    external: true
```

- [ ] **Step 2: 提交Docker Compose配置**

```bash
git add docker-compose.yml
git commit -m "feat: 创建Docker Compose配置"
```

---

## 阶段四：测试和文档（3天）

### Task 11: 编写集成测试

**文件:**
- Create: `tests/integration/test_btc_eth_strategy.py`

- [ ] **Step 1: 编写集成测试**

```python
"""
BTC/ETH策略集成测试
"""
import pytest
from strategies.btc_eth.strategy import BTCEthStrategy
from shared.binance_api import BinanceClient
from shared.kline_service import KLineService
from shared.notification import NotificationClient


@pytest.mark.asyncio
async def test_strategy_analyze():
    """测试策略分析功能"""
    config = {
        'strategy': {
            'symbols': ['BTCUSDT'],
            'timeframes': ['1h', '4h'],
            'scoring': {
                'min_score': 75,
                'grade_thresholds': {
                    'S': 90,
                    'A': 80,
                    'B': 70,
                    'C': 60
                }
            }
        }
    }
    
    binance_client = BinanceClient(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True
    )
    
    kline_service = KLineService(
        service_url="http://43.156.242.184:8765/api/v1"
    )
    
    notification_client = NotificationClient(
        service_url="http://43.156.242.184:8766/api/v1"
    )
    
    strategy = BTCEthStrategy(
        config=config,
        binance_client=binance_client,
        kline_service=kline_service,
        notification_client=notification_client
    )
    
    signal = await strategy.analyze('BTCUSDT')
    
    # 验证信号格式
    if signal:
        assert 'symbol' in signal
        assert 'direction' in signal
        assert 'grade' in signal
        assert 'score' in signal
```

- [ ] **Step 2: 运行集成测试**

```bash
pytest tests/integration/test_btc_eth_strategy.py -v
```

- [ ] **Step 3: 提交集成测试**

```bash
git add tests/integration/test_btc_eth_strategy.py
git commit -m "test: 添加BTC/ETH策略集成测试"
```

---

## 验收标准

### 功能验收

- [ ] 共享核心模块功能完整
  - [ ] 币安API封装可用
  - [ ] K线服务客户端可用
  - [ ] 通知服务客户端可用
  - [ ] 数据库管理可用
  - [ ] 技术指标计算正确

- [ ] BTC/ETH策略功能完整
  - [ ] 策略分析逻辑正确
  - [ ] 评分计算准确
  - [ ] 信号生成正确
  - [ ] 通知发送成功

- [ ] Docker部署成功
  - [ ] 容器构建成功
  - [ ] 容器运行正常
  - [ ] 日志输出正常

### 性能验收

- [ ] K线数据获取 < 2秒
- [ ] 策略分析 < 5秒
- [ ] 内存占用 < 500MB

### 文档验收

- [ ] 架构设计文档完整
- [ ] 部署文档完整
- [ ] 迁移方案文档完整
- [ ] API文档完整

---

## 注意事项

1. **渐进式迁移**：先完成共享核心模块，再迁移BTC/ETH策略
2. **保留原有项目**：不要删除原有项目，作为备份
3. **充分测试**：每个模块都要编写单元测试和集成测试
4. **频繁提交**：每完成一个小功能就提交一次
5. **文档同步**：代码变更时同步更新文档

---

**计划完成时间：** 15个工作日

**计划保存位置：** `docs/superpowers/plans/2026-05-05-unified-trading-system.md`
