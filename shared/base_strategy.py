"""
策略基类
定义交易策略的基本接口和通用功能
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import structlog

from .binance_api import BinanceClient
from .kline_service import KLineService
from .notification import NotificationClient
from .database import DatabaseManager


logger = structlog.get_logger()


class BaseStrategy(ABC):
    """
    策略基类
    
    所有交易策略必须继承此类并实现抽象方法
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化策略
        
        Args:
            config: 策略配置字典
        """
        if not isinstance(config, dict):
            raise ValueError(f"配置必须是字典类型，实际为 {type(config).__name__}")
        
        if not config:
            raise ValueError("配置不能为空")
        
        self.config = config
        self.binance_client: Optional[BinanceClient] = None
        self.kline_service: Optional[KLineService] = None
        self.notification_client: Optional[NotificationClient] = None
        self.db: Optional[DatabaseManager] = None
        self._running = False
        
        logger.info(
            "策略基类初始化",
            config_keys=list(config.keys())
        )
    
    @abstractmethod
    async def initialize(self) -> None:
        """
        初始化策略资源
        
        子类必须实现此方法，用于初始化：
        - 币安客户端
        - K线服务
        - 通知客户端
        - 数据库连接
        - 其他策略所需资源
        
        Raises:
            Exception: 初始化失败
        """
        pass
    
    @abstractmethod
    async def analyze(self, symbol: str) -> Dict[str, Any]:
        """
        分析市场数据
        
        Args:
            symbol: 交易对
        
        Returns:
            分析结果字典，包含信号、指标等信息
        
        Raises:
            ValueError: 参数验证失败
            Exception: 分析失败
        """
        pass
    
    @abstractmethod
    async def execute_signal(self, signal: Dict[str, Any]) -> bool:
        """
        执行交易信号
        
        Args:
            signal: 交易信号字典，包含：
                - action: 交易动作 (BUY, SELL, HOLD)
                - symbol: 交易对
                - quantity: 数量
                - price: 价格（可选）
                - reason: 信号原因
        
        Returns:
            是否执行成功
        
        Raises:
            ValueError: 参数验证失败
            Exception: 执行失败
        """
        pass
    
    @abstractmethod
    async def run(self) -> None:
        """
        运行策略
        
        子类必须实现此方法，定义策略的主循环逻辑：
        1. 获取市场数据
        2. 分析数据
        3. 生成信号
        4. 执行交易
        5. 发送通知
        6. 记录日志
        
        Raises:
            Exception: 运行失败
        """
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """
        停止策略
        
        子类必须实现此方法，用于：
        - 停止主循环
        - 清理资源
        - 关闭连接
        - 保存状态
        
        Raises:
            Exception: 停止失败
        """
        pass
    
    def is_running(self) -> bool:
        """
        检查策略是否正在运行
        
        Returns:
            是否正在运行
        """
        return self._running
    
    async def set_binance_client(self, client: BinanceClient) -> None:
        """
        设置币安客户端
        
        Args:
            client: 币安客户端实例
        
        Raises:
            ValueError: 参数验证失败
        """
        if not isinstance(client, BinanceClient):
            raise ValueError(f"客户端必须是 BinanceClient 类型，实际为 {type(client).__name__}")
        
        self.binance_client = client
        logger.info("币安客户端已设置")
    
    async def set_kline_service(self, service: KLineService) -> None:
        """
        设置K线服务
        
        Args:
            service: K线服务实例
        
        Raises:
            ValueError: 参数验证失败
        """
        if not isinstance(service, KLineService):
            raise ValueError(f"服务必须是 KLineService 类型，实际为 {type(service).__name__}")
        
        self.kline_service = service
        logger.info("K线服务已设置")
    
    async def set_notification_client(self, client: NotificationClient) -> None:
        """
        设置通知客户端
        
        Args:
            client: 通知客户端实例
        
        Raises:
            ValueError: 参数验证失败
        """
        if not isinstance(client, NotificationClient):
            raise ValueError(f"客户端必须是 NotificationClient 类型，实际为 {type(client).__name__}")
        
        self.notification_client = client
        logger.info("通知客户端已设置")
    
    async def set_database(self, db: DatabaseManager) -> None:
        """
        设置数据库管理器
        
        Args:
            db: 数据库管理器实例
        
        Raises:
            ValueError: 参数验证失败
        """
        if not isinstance(db, DatabaseManager):
            raise ValueError(f"数据库必须是 DatabaseManager 类型，实际为 {type(db).__name__}")
        
        self.db = db
        logger.info("数据库管理器已设置")
    
    async def cleanup(self) -> None:
        """
        清理资源
        
        关闭所有连接和客户端
        """
        logger.info("开始清理策略资源")
        
        if self.binance_client:
            await self.binance_client.close()
            logger.info("币安客户端已关闭")
        
        if self.kline_service:
            await self.kline_service.close()
            logger.info("K线服务已关闭")
        
        if self.notification_client:
            await self.notification_client.close()
            logger.info("通知客户端已关闭")
        
        if self.db:
            await self.db.disconnect()
            logger.info("数据库连接已关闭")
        
        self._running = False
        logger.info("策略资源清理完成")
