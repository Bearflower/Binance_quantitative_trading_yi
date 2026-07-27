"""K 线数据模型"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class KlineData(BaseModel):
    """K 线数据模型"""

    symbol: str = Field(..., description="交易对")
    interval: str = Field(..., description="时间间隔")
    open_time: int = Field(..., description="开盘时间（毫秒）")
    open_price: float = Field(..., description="开盘价")
    high_price: float = Field(..., description="最高价")
    low_price: float = Field(..., description="最低价")
    close_price: float = Field(..., description="收盘价")
    volume: float = Field(..., description="成交量")
    close_time: int = Field(..., description="收盘时间（毫秒）")
    quote_volume: float = Field(..., description="成交额")
    trade_count: int = Field(..., description="成交笔数")
    taker_buy_volume: float = Field(..., description="主动买入成交量")
    taker_buy_quote_volume: float = Field(..., description="主动买入成交额")

    class Config:
        from_attributes = True

    @classmethod
    def from_binance_data(cls, symbol: str, interval: str, data: list) -> "KlineData":
        """
        从币安原始数据创建 KlineData 实例

        Args:
            symbol: 交易对
            interval: 时间间隔
            data: 币安 K 线数据（列表格式）

        Returns:
            KlineData 实例
        """
        return cls(
            symbol=symbol,
            interval=interval,
            open_time=data[0],
            open_price=float(data[1]),
            high_price=float(data[2]),
            low_price=float(data[3]),
            close_price=float(data[4]),
            volume=float(data[5]),
            close_time=data[6],
            quote_volume=float(data[7]),
            trade_count=data[8],
            taker_buy_volume=float(data[9]),
            taker_buy_quote_volume=float(data[10]),
        )

    def to_dict(self) -> dict:
        """转换为字典格式，用于数据库插入"""
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": datetime.fromtimestamp(self.open_time / 1000),
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "close_price": self.close_price,
            "volume": self.volume,
            "close_time": datetime.fromtimestamp(self.close_time / 1000),
            "quote_volume": self.quote_volume,
            "trade_count": self.trade_count,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
        }
