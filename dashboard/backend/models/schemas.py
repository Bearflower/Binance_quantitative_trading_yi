"""
Dashboard 数据模型
定义 API 请求和响应的数据结构
"""
from typing import List, Dict, Optional, Any
from decimal import Decimal

from pydantic import BaseModel, Field


# ========================================
# 基础模型
# ========================================

class BaseResponse(BaseModel):
    """基础响应模型"""

    code: int = Field(0, description="状态码，0表示成功")
    message: str = Field("success", description="响应消息")


class HealthData(BaseModel):
    """健康检查数据"""

    status: str = Field(..., description="服务状态")
    timestamp: str = Field(..., description="时间戳")
    version: str = Field(..., description="版本号")


class HealthResponse(BaseResponse):
    """健康检查响应"""

    data: HealthData = Field(..., description="健康检查数据")


class ErrorResponse(BaseModel):
    """错误响应"""

    code: str = Field(..., description="错误码")
    message: str = Field(..., description="错误信息")
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="错误详情"
    )


# ========================================
# 策略相关模型
# ========================================

class StrategySummary(BaseModel):
    """策略摘要（用于总览和列表）"""

    id: str = Field(..., description="策略ID")
    name: str = Field(..., description="策略名称")
    emoji: str = Field("", description="策略图标")
    order_count: int = Field(0, description="订单数")
    fill_count: int = Field(0, description="成交数")
    closed_count: int = Field(0, description="平仓数")
    win_count: int = Field(0, description="盈利笔数")
    loss_count: int = Field(0, description="亏损笔数")
    total_pnl: str = Field("0", description="总盈亏（净）")
    gross_pnl: Optional[str] = Field(None, description="毛利润（不含佣金）")
    commission: Optional[str] = Field(None, description="佣金支出（负值）")
    win_rate: float = Field(0.0, description="胜率")
    error: Optional[str] = Field(None, description="错误信息")


class SymbolDetail(BaseModel):
    """币种明细"""

    symbol: str = Field(..., description="交易对")
    order_count: int = Field(0, description="订单数")
    fill_count: int = Field(0, description="成交数")
    wins: int = Field(0, description="盈利笔数")
    losses: int = Field(0, description="亏损笔数")
    total_pnl: str = Field("0", description="总盈亏（净）")
    gross_pnl: Optional[str] = Field(None, description="毛利润（不含佣金）")
    commission: Optional[str] = Field(None, description="佣金支出（负值）")
    win_rate: float = Field(0.0, description="胜率")
    data_quality: str = Field("ok", description="数据质量")
    quality_note: str = Field("", description="质量说明")


class StrategyDetailData(BaseModel):
    """策略详情数据"""

    id: str = Field(..., description="策略ID")
    name: str = Field(..., description="策略名称")
    emoji: str = Field("", description="策略图标")
    order_count: int = Field(0, description="订单数")
    fill_count: int = Field(0, description="成交数")
    closed_count: int = Field(0, description="平仓数")
    win_count: int = Field(0, description="盈利笔数")
    loss_count: int = Field(0, description="亏损笔数")
    total_pnl: str = Field("0", description="总盈亏（净）")
    gross_pnl: Optional[str] = Field(None, description="毛利润（不含佣金）")
    commission: Optional[str] = Field(None, description="佣金支出（负值）")
    win_rate: float = Field(0.0, description="胜率")
    avg_daily_orders: float = Field(0.0, description="日均订单数")
    symbols: List[SymbolDetail] = Field(
        default_factory=list,
        description="币种明细"
    )
    daily_counts: Dict[str, int] = Field(
        default_factory=dict,
        description="逐日分布"
    )
    data_source: str = Field("binance_api", description="数据来源")
    validation_warnings: List[str] = Field(
        default_factory=list,
        description="校验警告"
    )
    error: Optional[str] = Field(None, description="错误信息")
    updated_at: str = Field(..., description="更新时间")


class StrategyDetail(BaseResponse):
    """策略详情响应"""

    data: StrategyDetailData = Field(..., description="策略详情数据")


# ========================================
# 总览相关模型
# ========================================

class OverviewData(BaseModel):
    """总览数据"""

    total_pnl: str = Field(..., description="总盈亏（净）")
    total_gross_pnl: Optional[str] = Field(None, description="总毛利润（不含佣金）")
    total_commission: Optional[str] = Field(None, description="总佣金支出（负值）")
    total_orders: int = Field(..., description="总订单数")
    total_closed: int = Field(..., description="总平仓数")
    total_wins: int = Field(..., description="总盈利笔数")
    win_rate: float = Field(..., description="总胜率")
    strategies: List[StrategySummary] = Field(
        ...,
        description="策略列表"
    )
    report_type: str = Field(..., description="报告类型")
    updated_at: str = Field(..., description="更新时间")


class OverviewResponse(BaseResponse):
    """总览响应"""

    data: OverviewData = Field(..., description="总览数据")


class StrategiesData(BaseModel):
    """策略列表数据"""

    strategies: List[StrategySummary] = Field(
        ...,
        description="策略列表"
    )
    report_type: str = Field(..., description="报告类型")
    updated_at: str = Field(..., description="更新时间")


class StrategiesResponse(BaseResponse):
    """策略列表响应"""

    data: StrategiesData = Field(..., description="策略列表数据")


class SymbolsData(BaseModel):
    """币种明细数据"""

    strategy_id: str = Field(..., description="策略ID")
    symbols: List[SymbolDetail] = Field(
        ...,
        description="币种明细列表"
    )
    report_type: str = Field(..., description="报告类型")
    updated_at: str = Field(..., description="更新时间")


class SymbolsResponse(BaseResponse):
    """币种明细响应"""

    data: SymbolsData = Field(..., description="币种明细数据")


# ========================================
# 趋势相关模型
# ========================================

class TrendDataPoint(BaseModel):
    """趋势数据点"""

    date: str = Field(..., description="日期")
    total_pnl: str = Field("0", description="总盈亏")
    win_rate: float = Field(0.0, description="胜率")
    order_count: int = Field(0, description="订单数")


class TrendData(BaseModel):
    """趋势数据"""

    trends: List[TrendDataPoint] = Field(
        ...,
        description="趋势数据列表"
    )
    report_type: str = Field(..., description="报告类型")
    updated_at: str = Field(..., description="更新时间")


class TrendResponse(BaseResponse):
    """趋势响应"""

    data: TrendData = Field(..., description="趋势数据")


# ========================================
# 元数据相关模型
# ========================================

class StrategyMeta(BaseModel):
    """策略元数据"""

    id: str = Field(..., description="策略ID")
    name: str = Field(..., description="策略名称")
    description: str = Field("", description="策略描述")


class MetadataData(BaseModel):
    """元数据"""

    strategies: List[StrategyMeta] = Field(
        ...,
        description="策略列表"
    )
    version: str = Field(..., description="版本号")
    updated_at: str = Field(..., description="更新时间")


class MetadataResponse(BaseResponse):
    """元数据响应"""

    data: MetadataData = Field(..., description="元数据")
