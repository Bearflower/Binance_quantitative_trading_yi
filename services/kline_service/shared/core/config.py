"""
配置管理模块

统一管理应用配置，支持环境变量和配置文件
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
import os
from functools import lru_cache


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用基础配置
    APP_NAME: str = "common_service"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # 数据库配置
    DATABASE_URL: str = Field(
        default="postgresql://binance:secure_password_here@localhost:5432/binance_data",
        description="PostgreSQL 数据库连接 URL"
    )
    DB_POOL_SIZE: int = Field(default=20, ge=5, le=50)
    
    # Redis 配置
    REDIS_URL: str = Field(default="redis://localhost:6379", description="Redis 连接 URL")
    
    # 币安 API 配置
    BINANCE_API_URL: str = "https://fapi.binance.com"
    BINANCE_API_TIMEOUT: int = 30
    BINANCE_RATE_LIMIT: int = 1200  # 每分钟请求数限制
    
    # K 线数据服务配置
    SYMBOLS: str = Field(default="BTCUSDT,ETHUSDT,BNBUSDT")
    COLLECT_INTERVALS: str = Field(default="15m,1h,4h,1d")
    
    # 首次采集最小窗口（分钟），确保足够数据用于 ATR 计算
    MIN_INITIAL_COLLECT_MINUTES: int = Field(default=1000, ge=60, le=10080, description="首次采集最小窗口（分钟）")

    # 通知服务配置
    BTC_ETH_WEBHOOK: Optional[str] = None
    NEW_COIN_WEBHOOK: Optional[str] = None
    GRID_WEBHOOK: Optional[str] = None
    INSPECTION_WEBHOOK: Optional[str] = None
    STOCK_WEBHOOK: Optional[str] = None
    WORKER_COUNT: int = Field(default=3, ge=1, le=10)
    RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=10, le=1000)
    
    # 日志配置
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # API 配置
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = Field(default="*", description="允许的 CORS 源，逗号分隔")
    
    @property
    def symbols_list(self) -> List[str]:
        """获取币种列表"""
        return [s.strip() for s in self.SYMBOLS.split(",") if s.strip()]
    
    @property
    def intervals_list(self) -> List[str]:
        """获取周期列表"""
        return [i.strip() for i in self.COLLECT_INTERVALS.split(",") if i.strip()]
    
    @property
    def cors_origins_list(self) -> List[str]:
        """获取 CORS 源列表"""
        if self.CORS_ORIGINS == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
    
    @property
    def all_webhooks(self) -> dict:
        """获取所有 Webhook 配置"""
        return {
            "btc_eth": self.BTC_ETH_WEBHOOK,
            "new_coin": self.NEW_COIN_WEBHOOK,
            "grid": self.GRID_WEBHOOK,
            "inspection": self.INSPECTION_WEBHOOK,
            "stock": self.STOCK_WEBHOOK,
        }
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"  # 忽略额外的环境变量


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


# 全局配置实例
settings = get_settings()
