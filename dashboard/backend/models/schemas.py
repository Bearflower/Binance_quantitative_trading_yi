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
    open_position_count: int = Field(0, description="当前持仓数量")
    open_margin: str = Field("0", description="当前持仓保证金")
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
    open_position_count: int = Field(0, description="当前持仓数量")
    open_margin: str = Field("0", description="当前持仓保证金")
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
    total_unrealized_pnl: Optional[str] = Field(None, description="浮动盈亏（未实现，实时）")
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
# 账户净资产模型
# ========================================

class EquityData(BaseModel):
    """合约账户净资产数据"""

    total_equity: str = Field(..., description="合约账户净资产（含未实现盈亏）")
    available_balance: str = Field("0", description="可用余额")
    open_positions: int = Field(0, description="当前持仓数（非零持仓）")
    updated_at: str = Field(..., description="数据更新时间")


class EquityResponse(BaseResponse):
    """净资产响应"""

    data: EquityData = Field(..., description="净资产数据")


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


# ========================================
# 收益率（基于净资产快照）
# ========================================

class ReturnsData(BaseModel):
    """账户收益率数据"""

    period: str = Field(..., description="周期：daily/weekly/monthly")
    equity: str = Field("0", description="当前净资产")
    # yield 为 Python 保留字，用 alias 保持 API 字段名一致（service 返回 dict 键为 "yield"）
    yield_: Optional[float] = Field(None, alias="yield", description="收益率（%）")
    yield_text: str = Field("--", description="收益率展示文本")
    yield_unavailable: bool = Field(False, description="收益率是否不可用")
    period_start_equity: Optional[str] = Field(None, description="期初净资产")
    period_pnl: Optional[str] = Field(None, description="期初至今盈亏")
    snapshot_date: str = Field(..., description="快照日期")

    model_config = {"populate_by_name": True}


class ReturnsResponse(BaseResponse):
    """收益率响应"""

    data: ReturnsData = Field(..., description="收益率数据")


# ========================================
# AI 监控（调优执行 + 月度分配 + 最近建议）
# ========================================

class AiTuningRun(BaseModel):
    """AI 调优执行记录"""

    strategy_id: Optional[str] = Field(None, description="策略ID")
    strategy_name: Optional[str] = Field(None, description="策略名称")
    status: Optional[str] = Field(None, description="状态：success/skip/error")
    run_key: Optional[str] = Field(None, description="批次标识（周日日期）")
    executed_at: Optional[str] = Field(None, description="执行时间")


class AllocationEntry(BaseModel):
    """月度资金分配条目"""

    strategy_id: Optional[str] = Field(None, description="策略ID")
    strategy_name: Optional[str] = Field(None, description="策略名称")
    allocated_amount: Optional[float] = Field(None, description="分配金额（USDT）")
    allocated_ratio: Optional[float] = Field(None, description="分配比例")
    occupied_amount: Optional[float] = Field(None, description="占用金额（家庭级持仓保证金合计）")
    occupied_ratio: Optional[float] = Field(None, description="占用比（持仓保证金/分配金额）")
    rank: Optional[int] = Field(None, description="排名")


class CapitalAllocationItem(BaseModel):
    """月度资金分配"""

    month: Optional[str] = Field(None, description="分配月份")
    total_capital: Optional[str] = Field(None, description="总资金")
    strategy_count: int = Field(0, description="策略数量")
    entries: List[AllocationEntry] = Field(default_factory=list, description="分配条目")
    status: Optional[str] = Field(None, description="状态")


class SuggestionItem(BaseModel):
    """最近 AI 建议"""

    strategy_id: Optional[str] = Field(None, description="策略ID")
    strategy_name: Optional[str] = Field(None, description="策略名称")
    created_at: Optional[str] = Field(None, description="创建时间")
    status: Optional[str] = Field(None, description="周度调优状态：success=已调整 / skip=无需调整 / error=异常")
    adjustments: List[str] = Field(default_factory=list, description="调整建议")
    is_applied: bool = Field(False, description="是否已应用")
    is_rejected: bool = Field(False, description="是否已拒绝")


class AiMonitorData(BaseModel):
    """AI 监控数据"""

    tuning_runs: List[AiTuningRun] = Field(default_factory=list, description="调优执行记录")
    capital_allocation: Optional[CapitalAllocationItem] = Field(
        None, description="月度资金分配"
    )
    recent_suggestions: List[SuggestionItem] = Field(
        default_factory=list, description="最近优化建议"
    )
    refresh_info: Optional[Dict[str, Any]] = Field(
        None, description="持仓数据刷新信息（refresh_interval/refresh_in）"
    )


class AiMonitorResponse(BaseResponse):
    """AI 监控响应"""

    data: AiMonitorData = Field(..., description="AI 监控数据")


# ========================================
# 风控模块
# ========================================

class StopTrendItem(BaseModel):
    """止损趋势点"""

    date: str = Field(..., description="日期（MM-DD）")
    count: int = Field(0, description="止损单笔数")


class RiskData(BaseModel):
    """风控指标数据"""

    total_position_margin: str = Field("0", description="当前总持仓保证金")
    margin_limit: Optional[str] = Field(None, description="持仓上限（月度分配总额）")
    limit_occupancy: Optional[float] = Field(None, description="占用率（%）")
    account_ratio_caps: Dict[str, float] = Field(
        default_factory=dict, description="各策略真实 cap 对账"
    )
    equity_ratio_occupancy: Optional[float] = Field(None, description="净资产占用率（%）")
    available_margin: Optional[str] = Field(None, description="可用保证金")
    approaching_threshold: bool = Field(False, description="是否逼近阈值")
    threshold_exceeded: bool = Field(False, description="是否超限")
    recent_stop_count: int = Field(0, description="近 N 天止损单笔数")
    recent_stop_trend: List[StopTrendItem] = Field(
        default_factory=list, description="止损每日分布"
    )
    consecutive_loss_days: int = Field(0, description="连续亏损天数")
    max_drawdown_period: Optional[str] = Field(None, description="最大回撤发生日期")
    drawdown_pct: Optional[float] = Field(None, description="最大单日回撤（%）")
    daily_drawdown_pct: Optional[float] = Field(None, description="单日回撤阈值（%）")
    updated_at: str = Field(..., description="更新时间")
    refresh_info: Optional[Dict[str, Any]] = Field(
        None, description="持仓数据刷新信息（refresh_interval/refresh_in）"
    )


class RiskResponse(BaseResponse):
    """风控响应"""

    data: RiskData = Field(..., description="风控指标数据")
