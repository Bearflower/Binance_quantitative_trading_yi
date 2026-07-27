"""
已注册标的配置模型
"""

from datetime import datetime, timedelta
from typing import List, Optional
from pydantic import BaseModel, Field


class RegisteredSymbolConfig(BaseModel):
    """已注册标的配置"""
    
    id: Optional[int] = None
    symbol: str = Field(..., description="交易对符号，如 NEWCOINUSDT")
    intervals: List[str] = Field(..., description="采集周期列表，如 ['1m', '5m', '15m', '1h']")
    registered_at: datetime = Field(default_factory=datetime.now, description="注册时间")
    expires_at: datetime = Field(..., description="过期时间")
    duration_days: int = Field(..., ge=1, le=30, description="采集持续天数（1-30 天）")
    priority: str = Field(default="normal", description="优先级：high, normal, low")
    status: str = Field(default="active", description="状态：active, expired, cancelled")
    created_by: str = Field(default="system", description="注册方标识")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")
    
    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "NEWCOINUSDT",
                "intervals": ["1m", "5m", "15m", "1h"],
                "duration_days": 10,
                "priority": "high",
                "created_by": "new_coin_system"
            }
        }
    
    def is_expired(self) -> bool:
        """检查是否已过期"""
        return datetime.now() > self.expires_at
    
    def days_remaining(self) -> int:
        """计算剩余天数"""
        if self.is_expired():
            return 0
        delta = self.expires_at - datetime.now()
        return delta.days
    
    @classmethod
    def create(cls, symbol: str, intervals: List[str], duration_days: int = 10, 
               priority: str = "normal", created_by: str = "system") -> "RegisteredSymbolConfig":
        """创建新的注册配置"""
        now = datetime.now()
        expires_at = now + timedelta(days=duration_days)
        
        return cls(
            symbol=symbol,
            intervals=intervals,
            registered_at=now,
            expires_at=expires_at,
            duration_days=duration_days,
            priority=priority,
            status="active",
            created_by=created_by,
            updated_at=now
        )
    
    def renew(self, additional_days: int) -> None:
        """续期"""
        self.expires_at += timedelta(days=additional_days)
        self.duration_days += additional_days
        self.updated_at = datetime.now()
    
    def cancel(self) -> None:
        """取消注册"""
        self.status = "cancelled"
        self.updated_at = datetime.now()
    
    def expire(self) -> None:
        """标记为过期"""
        self.status = "expired"
        self.updated_at = datetime.now()


class RegisterRequest(BaseModel):
    """注册请求"""
    
    symbol: str = Field(..., description="交易对符号")
    intervals: List[str] = Field(..., description="采集周期列表")
    duration_days: int = Field(default=10, ge=1, le=30, description="采集持续天数")
    priority: str = Field(default="normal", description="优先级")
    
    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "NEWCOINUSDT",
                "intervals": ["1m", "5m", "15m", "1h"],
                "duration_days": 10,
                "priority": "high"
            }
        }


class RenewRequest(BaseModel):
    """续期请求"""
    
    symbol: str = Field(..., description="交易对符号")
    additional_days: int = Field(..., ge=1, le=30, description="续期天数")


class UnregisterRequest(BaseModel):
    """取消注册请求"""
    
    symbol: str = Field(..., description="交易对符号")


class RegisteredSymbolList(BaseModel):
    """已注册标的列表响应"""
    
    code: int = 0
    message: str = "success"
    data: List[RegisteredSymbolConfig] = []
    total: int = 0


class RegisterResponse(BaseModel):
    """注册响应"""
    
    code: int = 0
    message: str = "success"
    data: RegisteredSymbolConfig
