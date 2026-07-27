"""
通知服务客户端
发送飞书通知、告警等
"""
from typing import Optional, Dict
import os
import asyncio
import aiohttp
import structlog


logger = structlog.get_logger()

# 策略名称映射：内部标识 -> 可读显示名称
STRATEGY_DISPLAY_NAMES = {
    "btc_eth": "主流币种趋势回调确认策略(MTPCS)",
    "new_coin": "新币策略",
    "grid": "网格策略",
    "hrs": "混合反转策略(HRS)",
}


class NotificationError(Exception):
    """通知服务异常"""
    pass


class NotificationClient:
    """通知服务客户端"""
    
    def __init__(
        self,
        service_url: str,
        timeout: int = 10,
        use_direct_webhook: bool = True
    ):
        self.service_url = service_url
        self.timeout = timeout
        self.use_direct_webhook = use_direct_webhook
        
        self.session: Optional[aiohttp.ClientSession] = None
        
        self._webhook_mapping = self._load_webhook_mapping()
        
        logger.info(
            "通知服务客户端初始化",
            service_url=service_url,
            use_direct_webhook=use_direct_webhook,
            webhook_projects=list(self._webhook_mapping.keys())
        )
    
    def _load_webhook_mapping(self) -> Dict[str, str]:
        """
        从环境变量加载webhook映射
        
        Returns:
            project到webhook URL的映射字典
        """
        mapping = {}
        
        if webhook := os.getenv("FEISHU_WEBHOOK_GRID"):
            mapping["grid"] = webhook
        
        if webhook := os.getenv("FEISHU_WEBHOOK_BTC_ETH"):
            mapping["btc_eth"] = webhook
        
        if webhook := os.getenv("FEISHU_WEBHOOK_NEW_COIN"):
            mapping["new_coin"] = webhook
        
        if webhook := os.getenv("FEISHU_WEBHOOK_HRS", "https://open.feishu.cn/open-apis/bot/v2/hook/e628ca79-2f0c-4e59-97b3-6c2054ccddb7"):
            mapping["hrs"] = webhook
        
        if webhook := os.getenv("FEISHU_WEBHOOK"):
            mapping["default"] = webhook
        
        return mapping
    
    def register_webhook(self, project: str, webhook_url: str) -> None:
        """
        注册或覆盖指定项目的 webhook URL

        Args:
            project: 项目名称
            webhook_url: 飞书 webhook URL
        """
        if not project or not project.strip():
            raise ValueError("项目名称不能为空")
        if not webhook_url or not webhook_url.strip():
            raise ValueError("webhook URL 不能为空")
        self._webhook_mapping[project.strip()] = webhook_url.strip()
        logger.info("已注册 webhook", project=project.strip())

    def has_webhook(self, project: str) -> bool:
        """
        检查指定项目是否已注册 webhook

        Args:
            project: 项目名称

        Returns:
            是否已注册
        """
        return project in self._webhook_mapping

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
        
        Raises:
            ValueError: 参数验证失败
        """
        if not message or not message.strip():
            raise ValueError("消息内容不能为空")
        
        if level not in ["info", "warning", "error", "debug"]:
            raise ValueError(f"无效的消息级别: {level}, 有效级别: info, warning, error, debug")
        
        if not project or not project.strip():
            raise ValueError("项目名称不能为空")
        
        project = project.strip()
        
        if self.use_direct_webhook and project in self._webhook_mapping:
            return await self._send_to_feishu_webhook(
                webhook_url=self._webhook_mapping[project],
                message=message.strip(),
                level=level
            )
        
        return await self._send_via_service(
            message=message.strip(),
            level=level,
            project=project
        )
    
    async def _send_to_feishu_webhook(
        self,
        webhook_url: str,
        message: str,
        level: str = "info"
    ) -> bool:
        """
        直接发送消息到飞书webhook（带重试机制）
        
        Args:
            webhook_url: 飞书webhook URL
            message: 消息内容
            level: 消息级别
        
        Returns:
            是否发送成功
        """
        await self._init_session()
        
        level_emoji = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "debug": "🔍"
        }
        
        emoji = level_emoji.get(level, "ℹ️")
        formatted_message = f"{emoji} {message}"
        
        payload = {
            "msg_type": "text",
            "content": {
                "text": formatted_message
            }
        }
        
        max_retries = int(os.getenv("FEISHU_RETRY_MAX", "3"))
        retry_delay = int(os.getenv("FEISHU_RETRY_DELAY", "5"))
        rate_limit_wait_base = int(os.getenv("FEISHU_RATE_LIMIT_WAIT", "30"))  # 频率限制基础等待秒数
        
        for attempt in range(max_retries):
            try:
                async with self.session.post(webhook_url, json=payload) as response:
                    data = await response.json()
                    
                    if response.status != 200:
                        logger.error(
                            "飞书webhook请求失败",
                            status=response.status,
                            response=data,
                            attempt=attempt + 1
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        return False
                    
                    if data.get('code') != 0:
                        error_code = data.get('code')
                        error_msg = data.get('msg', '')
                        
                        # 所有飞书返回错误码统一使用指数退避重试 + warning级别
                        # 19006: 飞书服务器内部错误（非代码问题）
                        # 11232: 飞书频率限制
                        wait_time = rate_limit_wait_base * (2 ** attempt)
                        logger.warning(
                            "飞书webhook返回错误，等待重试",
                            code=error_code,
                            message=error_msg,
                            attempt=attempt + 1,
                            wait_seconds=wait_time
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_time)
                            continue
                        return False
                    
                    logger.info(
                        "飞书webhook通知发送成功",
                        attempt=attempt + 1
                    )
                    
                    return True
            
            except aiohttp.ClientError as e:
                logger.error(
                    "飞书webhook连接失败",
                    error=str(e),
                    attempt=attempt + 1
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return False
        
        return False
    
    async def _send_via_service(
        self,
        message: str,
        level: str,
        project: str
    ) -> bool:
        """
        通过通知服务发送消息
        
        Args:
            message: 消息内容
            level: 消息级别
            project: 项目名称
        
        Returns:
            是否发送成功
        """
        await self._init_session()
        
        url = f"{self.service_url}/send"
        payload = {
            "message": message,
            "level": level,
            "project": project
        }
        
        logger.debug(
            "发送通知服务请求",
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
                
                if not isinstance(data, dict):
                    raise NotificationError(f"响应数据格式错误: 期望字典，实际为 {type(data).__name__}")
                
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
        
        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        if not strategy or not strategy.strip():
            raise ValueError("策略名称不能为空")
        
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not action or not action.strip():
            raise ValueError("交易动作不能为空")
        
        if quantity <= 0:
            raise ValueError(f"数量必须大于0: {quantity}")
        
        if price <= 0:
            raise ValueError(f"价格必须大于0: {price}")
        
        display_name = STRATEGY_DISPLAY_NAMES.get(strategy.strip(), strategy.strip())
        message = f"""
【交易通知】
策略: {display_name}
类型: 趋势跟踪 · 回调确认入场
交易对: {symbol.strip()}
方向: {action.strip()}
数量: {quantity}
价格: {price} USDT
"""
        
        if kwargs:
            message += "\n额外信息:\n"
            for key, value in kwargs.items():
                message += f"- {key}: {value}\n"
        
        return await self.send(
            message=message,
            level="info",
            project=strategy.strip()
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
    
    async def send_error_notification(
        self,
        strategy: str,
        error_message: str,
        symbol: str = ""
    ) -> bool:
        """
        发送错误通知
        
        Args:
            strategy: 策略名称
            error_message: 错误信息
            symbol: 交易对（可选）
        
        Returns:
            是否发送成功
        """
        if not strategy or not strategy.strip():
            raise ValueError("策略名称不能为空")
        
        if not error_message or not error_message.strip():
            raise ValueError("错误信息不能为空")
        
        symbol_info = f"\n交易对: {symbol.strip()}" if symbol and symbol.strip() else ""
        
        display_name = STRATEGY_DISPLAY_NAMES.get(strategy.strip(), strategy.strip())
        
        message = f"策略执行错误\n策略: {display_name}{symbol_info}\n错误: {error_message.strip()}"
        
        return await self.send(
            message=message,
            level="error",
            project=strategy.strip()
        )
