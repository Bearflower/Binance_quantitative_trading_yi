"""
BTC/ETH策略逻辑
基于评分引擎的趋势跟踪策略
"""
import asyncio
import os
from typing import Dict, Optional, Tuple, Any
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import structlog

from shared.binance_api import BinanceClient, BinanceAPIError
from shared.circuit_breaker import (
    CircuitBreaker,
    compute_1h_return,
    floor_index_hour,
    load_circuit_breaker_config,
)
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.indicators import TechnicalIndicators
from shared.dynamic_atr_filter import DynamicATRFilter
from shared.condition_orders import record_condition_order, get_open_orders
from shared.trade_logger import TradeLogger
from shared.capital_manager import CapitalManager
from shared.position_ownership import (
    load_ownership_config,
    resolve_position_owner,
    is_symbol_owned_by_other,
)
from shared.position_baseline import (
    DEFAULT_CONTRACT_SIZE,
    calc_graded_positions_margin,
    calc_position_margin,
    check_entry_within_limit,
)
from strategies.btc_eth.market_state import (
    get_market_state,
    get_market_state_behavior,
    MarketState
)


logger = structlog.get_logger()


# 保证金计算防除零兜底（杠杆缺失/无效时的保守默认，非业务阈值）
_DEFAULT_LEVERAGE = 2
_MIN_LEVERAGE = 1

# 止盈类平仓原因白名单：这些平仓不计入止损统计（与下方各止盈调用点 close_reason 保持一致）
_TAKE_PROFIT_CLOSE_REASONS = {"TP1", "TP2", "TRAILING_STOP"}


def _safe_last(indicators: Dict, timeframe: str, field: str) -> Optional[float]:
    """
    安全读取某时间框架某指标的最新有效值（v6.26 新增）

    统一兜底：字段缺失、时间框架缺失、Series 为空、值为 NaN 时返回 None，
    禁止调用方裸用 iloc[-1] 读取指标导致主循环中断。

    Args:
        indicators: 多时间框架指标字典，形如 {timeframe: {field: pd.Series}}
        timeframe: 时间框架（如 '1h' / '4h' / '1d'）
        field: 指标字段名（如 'EMA21' / 'RSI' / 'ATR' / 'BB_Middle' / 'Volume_MA'）

    Returns:
        最新有效值（float）；缺失 / 空 / NaN 返回 None
    """
    timeframe_indicators = indicators.get(timeframe)
    if not isinstance(timeframe_indicators, dict):
        return None
    series = timeframe_indicators.get(field)
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


class PositionState:
    """持仓状态管理类
    
    用于跟踪持仓的详细状态，包括分批止盈、动态利润保护、时间止损等
    """
    
    def __init__(self):
        """初始化持仓状态"""
        self.entry_price: Optional[Decimal] = None  # 入场价格
        self.entry_time: Optional[datetime] = None  # 入场时间
        self.direction: Optional[str] = None  # 方向：LONG/SHORT
        self.initial_quantity: Decimal = Decimal('0')  # 初始数量
        self.current_quantity: Decimal = Decimal('0')  # 当前数量
        self.atr: Decimal = Decimal('0')  # 入场时的ATR
        
        # 订单ID跟踪
        self.entry_order_id: Optional[int] = None  # 入场订单ID
        self.stop_loss_order_id: Optional[int] = None  # 止损订单ID
        self.tp1_order_id: Optional[int] = None  # TP1订单ID
        self.tp2_order_id: Optional[int] = None  # TP2订单ID
        self.cancel_pending: bool = False  # 条件单取消待处理标记（平仓时异步取消未确认，兜底扫描清理后清除）
        
        # 止盈止损状态
        self.tp1_hit: bool = False  # TP1是否触发
        self.tp2_hit: bool = False  # TP2是否触发
        self.trailing_activated: bool = False          # 动态利润保护是否激活
        self.trailing_stop_price: Optional[Decimal] = None  # 动态保护止损价
        self.trailing_stop_order_id: Optional[int] = None  # 动态移动止损条件单ID（同步到交易所）
        self.pending_profit_pct: Optional[float] = None     # 上次计算的浮盈%
        self.current_tier_index: int = -1                  # 当前回撤阶梯索引
        
        # 最高/最低价（用于动态利润保护）
        self.highest_price: Optional[Decimal] = None  # 做多时的最高价
        self.lowest_price: Optional[Decimal] = None  # 做空时的最低价
        self.cancel_retry_count: Dict[str, int] = {}  # 条件单取消重试计数（v6.23）
        self.last_retry_cycle: int = 0  # 上次重试时的主循环计数（v6.23.1）
        self.first_retry_time: Optional[datetime] = None  # 首次重试时间（v6.23.1，用于强制清理超时）
        self.grade: str = ""  # 信号等级，用于动态读取对应的风险参数
        # v6.27 时间平仓复核制：复核是否已完成（防重复平仓；不复用 tp1_hit，避免误激活动态止盈 also_on_tp1）
        self.time_stop_review_done: bool = False
        # v6.28 加仓统一托管：重建待收敛标记（取消旧单/重建条件单任一失败置 True，由 _retry_rebuild_pending 收敛）
        # 移动止损尾仓精度调整后为 0 时直接 logger.warning 跳过即可，无需额外状态字段
        self.rebuild_pending: bool = False


class FrequencyController:
    """频率控制器
    
    管理交易频率限制，包括每日交易次数、品种冷却期、连续亏损暂停等。
    支持将状态持久化到数据库，重启后自动恢复。
    """
    
    def __init__(self, config: Dict, db_manager=None, strategy_name: str = "MTPCS策略"):
        """
        初始化频率控制器
        
        Args:
            config: 频率控制配置
            db_manager: 数据库管理器（可选，用于持久化）
            strategy_name: 策略名称
        """
        self.config = config
        self.db_manager = db_manager
        self.strategy_name = strategy_name
        self.daily_trades: Dict[str, int] = {}
        self.symbol_daily_trades: Dict[str, Dict[str, int]] = {}
        self.symbol_last_trade_time: Dict[str, datetime] = {}
        self.consecutive_losses: int = 0
        self.pause_until: Optional[datetime] = None  # 连续亏损暂停截止时间
        self.weekly_pause_until: Optional[datetime] = None  # 单周亏损暂停截止时间（v6.16.10）
        self.daily_pnl: Dict[str, Decimal] = {}
        self.weekly_pnl: Dict[str, Decimal] = {}  # 按周聚合盈亏 {"2026-W25": Decimal}
    
    async def ensure_table_exists(self):
        """创建频率控制状态表（如果不存在）"""
        if not self.db_manager:
            return
        
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS frequency_control_state (
            id SERIAL PRIMARY KEY,
            strategy_name VARCHAR(50) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            last_trade_time TIMESTAMP WITH TIME ZONE,
            daily_trade_count INTEGER DEFAULT 0,
            daily_pnl DECIMAL(18, 8) DEFAULT 0,
            trade_date DATE,
            consecutive_losses INTEGER DEFAULT 0,
            pause_until TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(strategy_name, symbol, trade_date)
        )
        """
        
        create_index_sql = """
        CREATE INDEX IF NOT EXISTS idx_fc_state_strategy 
        ON frequency_control_state(strategy_name, trade_date)
        """
        
        try:
            await self.db_manager.execute_ddl(create_table_sql)
            await self.db_manager.execute_ddl(create_index_sql)
            
            # v6.16.10：为新字段添加列（如果不存在）
            alter_columns_sql = [
                """ALTER TABLE frequency_control_state 
                   ADD COLUMN IF NOT EXISTS weekly_pnl DECIMAL(18, 8) DEFAULT 0""",
                """ALTER TABLE frequency_control_state 
                   ADD COLUMN IF NOT EXISTS weekly_pause_until TIMESTAMP WITH TIME ZONE"""
            ]
            for alter_sql in alter_columns_sql:
                try:
                    await self.db_manager.execute_ddl(alter_sql)
                except Exception as e:
                    logger.debug(f"ALTER TABLE 跳过（列可能已存在）: {e}")
            
            logger.info("频率控制状态表已就绪")
        except Exception as e:
            logger.warning(f"创建频率控制状态表失败: {e}")
    
    async def load_state(self):
        """从数据库加载状态"""
        if not self.db_manager:
            return
        
        try:
            today = datetime.now().date().isoformat()
            
            rows = await self.db_manager.fetch_all(
                """SELECT symbol, last_trade_time, daily_trade_count, daily_pnl, 
                          trade_date, consecutive_losses, pause_until,
                          weekly_pnl, weekly_pause_until
                   FROM frequency_control_state 
                   WHERE strategy_name = $1""",
                self.strategy_name
            )
            
            if not rows:
                return
            
            for row in rows:
                symbol = row['symbol']
                trade_date = row['trade_date']
                
                if row['last_trade_time']:
                    self.symbol_last_trade_time[symbol] = row['last_trade_time']
                
                if trade_date and trade_date.isoformat() == today:
                    self.daily_trades[today] = self.daily_trades.get(today, 0) + (row['daily_trade_count'] or 0)
                    
                    if symbol not in self.symbol_daily_trades:
                        self.symbol_daily_trades[symbol] = {}
                    self.symbol_daily_trades[symbol][today] = row['daily_trade_count'] or 0
                    
                    if row['daily_pnl'] is not None:
                        self.daily_pnl[today] = self.daily_pnl.get(today, Decimal('0')) + Decimal(str(row['daily_pnl']))
                
                if row['consecutive_losses'] is not None:
                    self.consecutive_losses = max(self.consecutive_losses, row['consecutive_losses'] or 0)
                
                if row['pause_until']:
                    if self.pause_until is None or row['pause_until'] > self.pause_until:
                        self.pause_until = row['pause_until']
                
                # v6.16.10：恢复单周亏损状态
                if row.get('weekly_pnl') is not None:
                    # 取第一个非空值（所有行应相同）
                    try:
                        week_key = self._get_week_key(row['trade_date'])
                        self.weekly_pnl[week_key] = Decimal(str(row['weekly_pnl']))
                    except Exception:
                        pass
                
                if row.get('weekly_pause_until'):
                    if self.weekly_pause_until is None or row['weekly_pause_until'] > self.weekly_pause_until:
                        self.weekly_pause_until = row['weekly_pause_until']
            
            logger.info(
                "频率控制状态已从数据库恢复",
                symbols_in_cooldown=len(self.symbol_last_trade_time),
                consecutive_losses=self.consecutive_losses,
                pause_until=str(self.pause_until) if self.pause_until else None
            )
            
        except Exception as e:
            logger.warning(f"加载频率控制状态失败: {e}")
    
    async def _save_state(self, symbol: str):
        """保存单个品种的状态到数据库"""
        if not self.db_manager:
            return
        
        try:
            today = datetime.now().date()
            last_trade_time = self.symbol_last_trade_time.get(symbol)
            daily_count = 0
            daily_pnl = Decimal('0')
            
            today_str = today.isoformat()
            if symbol in self.symbol_daily_trades and today_str in self.symbol_daily_trades[symbol]:
                daily_count = self.symbol_daily_trades[symbol][today_str]
            
            if today_str in self.daily_pnl:
                daily_pnl = self.daily_pnl[today_str]
            
            await self.db_manager.execute(
                """INSERT INTO frequency_control_state 
                   (strategy_name, symbol, last_trade_time, daily_trade_count, daily_pnl, 
                    trade_date, consecutive_losses, pause_until, weekly_pnl, weekly_pause_until, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                   ON CONFLICT (strategy_name, symbol, trade_date) 
                   DO UPDATE SET 
                       last_trade_time = EXCLUDED.last_trade_time,
                       daily_trade_count = EXCLUDED.daily_trade_count,
                       daily_pnl = EXCLUDED.daily_pnl,
                       consecutive_losses = EXCLUDED.consecutive_losses,
                       pause_until = EXCLUDED.pause_until,
                       weekly_pnl = EXCLUDED.weekly_pnl,
                       weekly_pause_until = EXCLUDED.weekly_pause_until,
                       updated_at = NOW()
                """,
                self.strategy_name,
                symbol,
                last_trade_time,
                daily_count,
                float(daily_pnl),
                today,
                self.consecutive_losses,
                self.pause_until,
                float(self._calculate_weekly_pnl(self._get_week_key(datetime.now())) or Decimal('0')),
                self.weekly_pause_until
            )
            
        except Exception as e:
            logger.warning(f"保存频率控制状态失败: {e}")
    
    def can_trade(self, symbol: str, current_time: datetime) -> Tuple[bool, str]:
        """
        检查是否可以交易
        
        Args:
            symbol: 交易对
            current_time: 当前时间
        
        Returns:
            (是否可以交易, 原因说明)
        """
        # 检查是否在暂停期（连续亏损暂停）
        if self.pause_until and current_time < self.pause_until:
            remaining = self.pause_until - current_time
            return False, f"策略暂停中（连续亏损），剩余{remaining.days}天{remaining.seconds // 3600}小时"
        
        # 检查是否在单周亏损暂停期（v6.16.10，独立于连续亏损暂停）
        if self.weekly_pause_until and current_time < self.weekly_pause_until:
            remaining = self.weekly_pause_until - current_time
            return False, f"单周亏损暂停中，剩余{remaining.days}天{remaining.seconds // 3600}小时"
        
        # 检查每日最大亏损（绝对值 + 百分比双重限制，v6.16.10）
        today = current_time.date().isoformat()
        if today in self.daily_pnl:
            max_loss_abs = Decimal(str(self.config['max_daily_loss_usdt']))
            max_loss_ratio = Decimal(str(self.config.get('max_daily_loss_ratio', 0.05)))
            initial_capital = Decimal(str(self.config.get('initial_capital_usdt', 500)))
            
            loss_limit = max(max_loss_abs, initial_capital * max_loss_ratio)
            if self.daily_pnl[today] <= -loss_limit:
                return False, f"已达每日最大亏损限额{float(loss_limit):.1f}U"
        
        # 检查单周亏损（v6.16.10 新增）
        weekly_can_trade, weekly_reason = self._check_weekly_loss(current_time)
        if not weekly_can_trade:
            return False, weekly_reason
        
        # v6.21：全局每日总交易数限制移至 market_state 配置，与市场状态联动
        # 在 analyze() 中根据市场状态检查 market_state.behaviors.{state}.max_daily_trades
        # 此处不再检查全局限制
        
        # 检查单品种每日交易数
        if symbol not in self.symbol_daily_trades:
            self.symbol_daily_trades[symbol] = {}
        
        if today not in self.symbol_daily_trades[symbol]:
            self.symbol_daily_trades[symbol][today] = 0
        
        if self.symbol_daily_trades[symbol][today] >= self.config['max_daily_symbol_trades']:
            return False, f"{symbol}已达每日最大交易数{self.config['max_daily_symbol_trades']}笔"
        
        # 冷却期检查已移至 analyze() 中，根据市场状态使用不同的冷却期
        # 趋势市 cooling: 72h，震荡市 cooling: 3h
        
        return True, "可以交易"
    
    async def record_trade(self, symbol: str, current_time: datetime, pnl: Optional[Decimal] = None):
        """
        记录交易（同步更新内存状态并持久化到数据库）
        
        Args:
            symbol: 交易对
            current_time: 当前时间
            pnl: 盈亏金额（None表示开仓，有值表示平仓）
        """
        today = current_time.date().isoformat()
        
        self.daily_trades[today] = self.daily_trades.get(today, 0) + 1
        
        if symbol not in self.symbol_daily_trades:
            self.symbol_daily_trades[symbol] = {}
        self.symbol_daily_trades[symbol][today] = self.symbol_daily_trades[symbol].get(today, 0) + 1
        
        self.symbol_last_trade_time[symbol] = current_time
        
        if pnl is not None:
            if today not in self.daily_pnl:
                self.daily_pnl[today] = Decimal('0')
            self.daily_pnl[today] += pnl
            
            if pnl < 0:
                self.consecutive_losses += 1
                if self.consecutive_losses >= self.config['consecutive_loss_pause']:
                    pause_hours = self.config['pause_duration_hours']
                    self.pause_until = current_time + timedelta(hours=pause_hours)
                    logger.warning(
                        f"连续亏损{self.consecutive_losses}笔，策略暂停{pause_hours}小时",
                        pause_until=self.pause_until
                    )
            else:
                self.consecutive_losses = 0
        
        await self._save_state(symbol)
    
    def get_daily_stats(self, date: datetime) -> Dict:
        """
        获取指定日期的交易统计
        
        Args:
            date: 日期
        
        Returns:
            统计数据字典
        """
        date_str = date.date().isoformat()
        
        # 基础统计
        total_trades = self.daily_trades.get(date_str, 0)
        total_pnl = float(self.daily_pnl.get(date_str, Decimal('0')))
        
        # 计算盈亏次数（简化版本，实际应该从交易记录中统计）
        win_count = 0
        loss_count = 0
        max_profit = Decimal('0')
        max_loss = Decimal('0')
        
        # 如果有盈亏记录，估算盈亏次数
        if total_pnl > 0:
            win_count = max(1, total_trades // 2)  # 简化估算
            max_profit = Decimal(str(abs(total_pnl)))
        elif total_pnl < 0:
            loss_count = max(1, total_trades // 2)  # 简化估算
            max_loss = Decimal(str(abs(total_pnl)))
        
        # 计算胜率
        win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0
        
        return {
            'total_trades': total_trades,
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'max_profit': float(max_profit),
            'max_loss': float(max_loss),
            'consecutive_losses': self.consecutive_losses
        }
    
    def _get_week_key(self, dt: datetime) -> str:
        """获取当前日期所属的周标识（ISO 8601 周号）"""
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    
    def _check_weekly_loss(self, current_time: datetime) -> Tuple[bool, str]:
        """
        检查单周亏损是否超过阈值（v6.16.10）
        
        单周亏损 >15% 初始资金 → 暂停 3 天
        
        Args:
            current_time: 当前时间
        
        Returns:
            (是否可交易, 原因说明)
        """
        # 检查配置开关
        if not self.config.get('weekly_loss_pause_enabled', True):
            return True, "单周亏损暂停未启用"
        
        weekly_loss_ratio = self.config.get('weekly_loss_max_ratio', 0.15)
        pause_days = self.config.get('weekly_loss_pause_days', 3)
        initial_capital = Decimal(str(self.config.get('initial_capital_usdt', 500)))
        
        # 计算当前周盈亏
        week_key = self._get_week_key(current_time)
        weekly_pnl = self._calculate_weekly_pnl(week_key)
        
        if weekly_pnl is not None and weekly_pnl <= -initial_capital * Decimal(str(weekly_loss_ratio)):
            # 设置单周亏损暂停（如果还没有设置）
            if self.weekly_pause_until is None or self.weekly_pause_until < current_time:
                self.weekly_pause_until = current_time + timedelta(days=pause_days)
                logger.warning(
                    f"单周亏损{float(weekly_pnl):.1f}U 超过{weekly_loss_ratio*100:.0f}%阈值，"
                    f"策略暂停{pause_days}天",
                    week=week_key,
                    weekly_pnl=float(weekly_pnl),
                    weekly_pause_until=str(self.weekly_pause_until)
                )
            
            remaining = self.weekly_pause_until - current_time
            return False, (
                f"单周亏损已达{weekly_loss_ratio*100:.0f}%阈值，暂停{pause_days}天，"
                f"剩余{remaining.days}天{remaining.seconds // 3600}小时"
            )
        
        return True, "单周亏损正常"
    
    def _calculate_weekly_pnl(self, week_key: str) -> Optional[Decimal]:
        """
        计算指定周的累计盈亏
        
        从 daily_pnl 中筛选属于该周的所有日盈亏，求和。
        
        Args:
            week_key: 周标识，如 "2026-W25"
        
        Returns:
            该周累计盈亏，如果无数据返回 None
        """
        total = Decimal('0')
        has_data = False
        
        for date_str, pnl in self.daily_pnl.items():
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                if self._get_week_key(dt) == week_key:
                    total += pnl
                    has_data = True
            except ValueError:
                continue
        
        return total if has_data else None


class BTCEthStrategy:
    """BTC/ETH/BNB交易策略
    
    基于多时间框架分析的评分系统，综合评估趋势强度、形态质量、动量背离，
    生成交易信号并执行交易。
    
    评分维度：
    - 趋势强度（40%）：基于MA和MACD判断趋势强度
    - 形态质量（35%）：识别阳包阴、阴包阳、突破回踩、背离等形态
    - 动量背离（25%）：基于RSI和MACD判断动量背离
    
    风险管理：
    - 分批止盈：TP1(2.5×ATR)平25%，TP2(4.0×ATR)平25%
    - 吊灯止损：启动阈值1.8×ATR，回撤阈值1.2×ATR
    - 时间止损：持仓>72小时未达TP1平仓50%
    - 频率控制：每日最大4笔，单品种最大2笔，冷却期12小时
    - 动态仓位：保留20%安全垫，最小保证金5U，最大单仓100U
    """
    
    # 信号等级排序映射（v6.17：用于市场状态等级过滤）
    GRADE_ORDER = {'S': 0, 'A': 1, 'B': 2, 'C': 3}

    # 信号等级降级阶梯（v6.26：机制②轻度过热降级，从高到低）
    # C 为最低档，再降即返回 None（禁开）
    _GRADE_LADDER = ['S', 'A', 'B', 'C']
    
    def __init__(
        self,
        config: Dict,
        binance_client: BinanceClient,
        kline_service: KLineService,
        notification_client: NotificationClient,
        db_manager=None
    ):
        """
        初始化策略
        
        Args:
            config: 策略配置字典
            binance_client: 币安API客户端
            kline_service: K线服务客户端
            notification_client: 通知服务客户端
            db_manager: 数据库管理器（可选，用于持久化频率控制状态）
        """
        self.config = config
        self.binance = binance_client
        self.kline_service = kline_service
        self.notification = notification_client
        self.db_manager = db_manager
        
        # 策略配置
        self.symbols = config['strategy']['symbols']
        self.timeframes = config['strategy']['timeframes']
        self.risk_config = config['strategy']['risk']
        self.scoring_config = config['strategy']['scoring']
        self.symbol_config = config['strategy'].get('symbol_config', {})  # v6.16.10 币种差异化配置
        self.binance_config = config['binance']
        
        # 初始化频率控制器
        self.frequency_controller = FrequencyController(
            self.risk_config['frequency_control'],
            db_manager=self.db_manager,
            strategy_name="MTPCS策略"
        )
        
        # 初始化动态ATR过滤器（v6.16.10）
        atr_config = self.risk_config.get('dynamic_atr', {}).copy()
        # 从 symbol_config 构建 per-symbol ATR 参数覆盖（修复幽灵参数 bug）
        # 映射关系：
        #   atr_abs_min (decimal, 0.009=0.9%) → absolute_min_atr_percent (percentage, 0.9)
        #   atr_percentile (0-100, 50) → percentile (0-1, 0.5)
        #   atr_factor_strong → strong_coefficient
        if self.symbol_config:
            symbol_overrides = {}
            for sym, cfg in self.symbol_config.items():
                override = {}
                if 'atr_abs_min' in cfg:
                    override['absolute_min_atr_percent'] = cfg['atr_abs_min'] * 100
                if 'atr_percentile' in cfg:
                    override['percentile'] = cfg['atr_percentile'] / 100.0
                if 'atr_factor_strong' in cfg:
                    override['strong_coefficient'] = cfg['atr_factor_strong']
                if override:
                    symbol_overrides[sym] = override
            if symbol_overrides:
                atr_config['symbol_overrides'] = symbol_overrides
        if atr_config.get('enabled', True):
            self.atr_filter = DynamicATRFilter(atr_config)
        else:
            self.atr_filter = None
        
        # 持仓状态管理
        self.positions: Dict[str, PositionState] = {}
        
        # 策略名称（v6.28：用于 record_condition_order 等数据库落库的 strategy_name 参数）
        self.strategy_name = "btc_eth"
        
        # 交易对精度信息缓存
        self.symbol_precision: Dict[str, Dict] = {}
        
        # 主循环计数（v6.23.1：用于条件单重试间隔控制）
        self._cycle_count: int = 0

        # 震荡反转极端期暂停截止时间（per-symbol，机制③，CP-7，v6.26 新增）
        # 结构：{symbol: datetime}，跨 analyze 调度周期保持，进程内有效
        self.symbol_extreme_pause_until: Dict[str, datetime] = {}
        
        # 条件单取消操作锁（v6.23.1：防止异步路径和主循环路径并发修改）
        self._cancel_lock = asyncio.Lock()

        # 资金分配管理器（方案 D：运行时读 DB 为主来源，config 为兜底；保证金口径）
        # strategy_id=btc_eth：MTPCS 原版独立参与月度资金分配（激进版 btc_eth_aggressive 独立分配）
        config_dir = os.path.dirname(os.path.abspath(__file__))
        self.capital_mgr = CapitalManager(
            os.path.join(config_dir, "config.yaml"),
            db=self.db_manager,
            strategy_id="btc_eth",
        )
        
        # 最小持仓量阈值（v6.23.1：从配置读取，禁止硬编码）
        self.min_position_amt = float(
            config.get('strategy', {}).get('position_sync', {}).get('min_position_amt', 0.00001)
        )

        # 组合级熔断器（池=mtpcs 固定池；enabled=false 时置 None，不拦截任何开仓）
        cb_cfg = load_circuit_breaker_config()
        if cb_cfg.get("enabled"):
            self.circuit_breaker = CircuitBreaker(cb_cfg, self.db_manager, pool="mtpcs")
        else:
            self.circuit_breaker = None

        # 持仓归属隔离（方案C）：本策略归属名 + 共享账户对家策略列表
        _owc = load_ownership_config(config)
        self.my_record_name = _owc['my_record_name']
        self._competing_record_names = _owc['competing_record_names']

        logger.info(
            "BTC/ETH策略初始化",
            symbols=self.symbols,
            timeframes=self.timeframes,
            version=self.config.get('strategy', {}).get('version', '2.2.0')
        )
    
    async def analyze(self, symbol: str) -> Optional[Dict]:
        """
        分析市场数据，生成交易信号（v6.16.10）
        
        Args:
            symbol: 交易对
        
        Returns:
            分析结果字典
        """
        logger.info(f"开始分析 {symbol}")
        
        # 初始化基础分析结果
        analysis_result = {
            'symbol': symbol,
            'score': 0,
            'grade': 'D',
            'reason': ''
        }
        
        try:
            # 1. 频率控制检查
            current_time = datetime.now()
            can_trade, reason = self.frequency_controller.can_trade(symbol, current_time)
            
            if not can_trade:
                logger.info(f"{symbol} 频率控制限制: {reason}")
                analysis_result['reason'] = f"频率限制: {reason}"
                return analysis_result
            
            # 1.1 经济日历检查（v6.16.10 新增，频率控制之后、K线之前）
            eco_can_trade, eco_reason = self._check_economic_calendar(current_time)
            if not eco_can_trade:
                logger.info(f"{symbol} 经济日历禁止交易: {eco_reason}")
                analysis_result['reason'] = f"经济日历: {eco_reason}"
                return analysis_result
            
            # 1.2 亏损时禁止加仓检查（v6.16.10，v6.28 复用 _is_position_profitable）
            if symbol in self.positions:
                position = self.positions[symbol]
                if position.current_quantity > 0:
                    if not await self._is_position_profitable(symbol, position):
                        logger.info(
                            f"{symbol} 已有持仓浮亏，禁止加仓",
                            direction=position.direction,
                            entry_price=float(position.entry_price) if position.entry_price else None,
                            grade=position.grade
                        )
                        analysis_result['reason'] = "已有持仓浮亏，禁止加仓"
                        return analysis_result
            
            # 2. 获取多时间框架数据
            klines = await self.kline_service.get_multi_timeframe_data(
                symbol=symbol,
                intervals=self.timeframes
            )
            
            if not klines or len(klines) == 0:
                logger.warning(f"{symbol} 获取K线数据失败")
                analysis_result['reason'] = "K线数据获取失败"
                return analysis_result
            
            # 检查数据完整性
            for timeframe in self.timeframes:
                if timeframe not in klines or not klines[timeframe]:
                    logger.warning(f"{symbol} {timeframe} K线数据不完整")
                    analysis_result['reason'] = f"{timeframe} K线数据不完整"
                    return analysis_result
            
            # 3. 计算技术指标
            indicators = self._build_indicators(klines)
            
            # 3.5 市场状态识别（v6.18 激进收紧版）
            market_state_config = self.risk_config.get('market_state', {})
            market_state = None
            if market_state_config.get('enabled', True) and '4h' in klines:
                # 提取4h收盘价用于计算价格变化
                close_4h = pd.Series([float(k['close']) for k in klines['4h']])
                # 补传日线指标：日线EMA21斜率判断强趋势市的必要条件，
                # 漏传会导致 daily_slope 恒为0，强趋势市永远无法识别（v6.21.1 修复）
                market_state, state_desc = get_market_state(
                    indicators['4h'],
                    close_prices=close_4h,
                    indicators_1d=indicators.get('1d'),
                    config=market_state_config
                )
                market_behavior = get_market_state_behavior(market_state, market_state_config)
                
                logger.info(f"{symbol} 市场状态: {state_desc}, 行为: {market_behavior}")
                
                # 震荡市：完全禁止开仓
                if not market_behavior['can_trade']:
                    logger.info(f"{symbol} 震荡市，完全禁止开仓")
                    analysis_result['reason'] = f"市场状态: {state_desc}，禁止开仓"
                    return analysis_result
            else:
                # 市场状态未启用或无4h数据，使用配置的 fallback 行为
                fallback = market_state_config.get('fallback', {})
                market_behavior = {
                    'can_trade': fallback.get('can_trade', True),
                    'min_grade': fallback.get('min_grade', 'C'),
                    'vol_boost': fallback.get('vol_boost', 0.0),
                    'position_ratio_mult': fallback.get('position_ratio_mult', 1.0),
                    'stop_loss_atr': fallback.get('stop_loss_atr', 0)
                }
            
            # 3.6 v6.21：市场状态特定频率控制检查
            market_state_name = market_state.value if market_state else 'RANGING'
            symbol_cfg = self.symbol_config.get(symbol, {})
            market_max_trades = market_behavior.get('max_daily_trades', 999)
            today_str = current_time.date().isoformat()
            # 全局每日交易数检查（使用全局计数器）
            if today_str not in self.frequency_controller.daily_trades:
                self.frequency_controller.daily_trades[today_str] = 0
            if self.frequency_controller.daily_trades[today_str] >= market_max_trades:
                logger.info(
                    f"{symbol} 市场状态{market_state_name}已达每日最大交易数{market_max_trades}笔"
                )
                analysis_result['reason'] = f"频率限制: {market_state_name}已达每日最大交易数{market_max_trades}笔"
                return analysis_result
            # v6.21：per-symbol 震荡市每日交易数限制（使用 per-symbol 计数器，不阻塞其他币种）
            if market_state_name == 'RANGING' and 'ranging_max_daily_trades' in symbol_cfg:
                symbol_max_trades = symbol_cfg['ranging_max_daily_trades']
                symbol_daily = self.frequency_controller.symbol_daily_trades.setdefault(symbol, {})
                if today_str not in symbol_daily:
                    symbol_daily[today_str] = 0
                if symbol_daily[today_str] >= symbol_max_trades:
                    logger.info(
                        f"{symbol} 震荡市已达每日最大交易数{symbol_max_trades}笔"
                    )
                    analysis_result['reason'] = f"频率限制: {symbol}震荡市已达每日最大交易数{symbol_max_trades}笔"
                    return analysis_result
            
            # 3.7 v6.22.1：市场状态特定冷却期检查（从 can_trade() 移至此处）
            # 趋势市冷却期: 72h，震荡市冷却期: 3h
            if symbol in self.frequency_controller.symbol_last_trade_time:
                cooldown_hours = market_behavior.get('ranging_symbol_cooldown_hours', 72)
                # v6.21：per-symbol 震荡市冷却期覆盖
                if market_state_name == 'RANGING' and 'ranging_cooldown_hours' in symbol_cfg:
                    cooldown_hours = symbol_cfg['ranging_cooldown_hours']
                last_trade_time = self.frequency_controller.symbol_last_trade_time[symbol]
                
                # 处理时区问题
                check_time = current_time
                if last_trade_time.tzinfo is not None and check_time.tzinfo is None:
                    last_trade_time = last_trade_time.replace(tzinfo=None)
                elif last_trade_time.tzinfo is None and check_time.tzinfo is not None:
                    check_time = check_time.replace(tzinfo=None)
                
                time_since_last = check_time - last_trade_time
                if time_since_last < timedelta(hours=cooldown_hours):
                    remaining = timedelta(hours=cooldown_hours) - time_since_last
                    remaining_hours = remaining.seconds // 3600 + remaining.days * 24
                    logger.info(
                        f"{symbol} 冷却期中({market_state_name}市,冷却{cooldown_hours}h)，剩余{remaining_hours}小时"
                    )
                    analysis_result['reason'] = f"冷却期: {symbol}冷却期中({market_state_name}市)，剩余{remaining_hours}小时"
                    return analysis_result
            
            # 3.8 v6.21：per-symbol 震荡市 ADX 下限检查
            # 在 RANGING 模式下，对特定币种额外要求最低 ADX 值
            if market_state_name == 'RANGING' and 'ranging_adx_min' in symbol_cfg:
                adx_4h = indicators.get('4h', {}).get('ADX', pd.Series([0])).iloc[-1]
                if pd.notna(adx_4h) and adx_4h < symbol_cfg['ranging_adx_min']:
                    logger.info(
                        f"{symbol} 震荡市ADX={float(adx_4h):.1f} < {symbol_cfg['ranging_adx_min']}，跳过"
                    )
                    analysis_result['reason'] = f"震荡市ADX不足: {float(adx_4h):.1f} < {symbol_cfg['ranging_adx_min']}"
                    return analysis_result
            
            # 4. 入场检查（v6.22：根据市场状态分流）
            strategy_mode = market_behavior.get('strategy_mode', 'trend')
            if strategy_mode == 'ranging':
                # 震荡市：使用反转入场条件
                entry_pass, entry_result = self._check_ranging_entry(symbol, indicators, klines)
                if not entry_pass:
                    logger.info(f"{symbol} 震荡入场未通过: {entry_result}")
                    analysis_result['reason'] = f"震荡入场: {entry_result}"
                    return analysis_result
                direction = entry_result  # 震荡市入场结果直接包含方向
            else:
                # 趋势市：使用趋势过滤器
                trend_pass, trend_result = self._check_trend_filter(symbol, indicators, klines)
                if not trend_pass:
                    logger.info(f"{symbol} 趋势过滤未通过: {trend_result}")
                    analysis_result['reason'] = f"趋势过滤: {trend_result}"
                    return analysis_result
                if trend_result:
                    direction = trend_result
                else:
                    direction = self._determine_direction(indicators)
            
            # 5. 禁止入场条件检查（v6.16.10 新增）
            allowed, prohibition_reason = await self._check_prohibited_conditions(symbol, klines)
            if not allowed:
                logger.info(f"{symbol} 禁止入场: {prohibition_reason}")
                analysis_result['reason'] = f"禁止入场: {prohibition_reason}"
                return analysis_result
            
            # 6. 动态ATR过滤器（v6.16.10 新增）
            current_price = Decimal(str(klines['1h'][-1]['close']))
            atr = Decimal(str(indicators['1h']['ATR'].iloc[-1]))
            
            if self.atr_filter and self.atr_filter.enabled:
                atr_pct = float(atr / current_price) * 100
                self.atr_filter.update_history(symbol, float(atr), float(current_price))
                
                adx_1d = indicators.get('1d', {}).get('ADX', pd.Series([0])).iloc[-1]
                if pd.isna(adx_1d) or adx_1d == 0:
                    # 1d ADX不可用时（数据不足或过期）降级使用4h ADX
                    adx_4h = indicators.get('4h', {}).get('ADX', pd.Series([0])).iloc[-1]
                    if not pd.isna(adx_4h) and adx_4h > 0:
                        adx_1d = adx_4h
                        logger.debug(f"{symbol} 1d ADX不可用，降级使用4h ADX={float(adx_4h):.1f}")
                should_filter, filter_reason = self.atr_filter.should_filter(
                    symbol, atr_pct, float(adx_1d) if pd.notna(adx_1d) else 0
                )
                if should_filter:
                    logger.info(f"{symbol} ATR过滤: {filter_reason}")
                    analysis_result['reason'] = f"ATR过滤: {filter_reason}"
                    return analysis_result
            
            # 7. 计算综合评分（v6.22：震荡市使用不同权重）
            score = self._calculate_score(indicators, klines, market_state_name)
            analysis_result['score'] = score
            
            if score < self.scoring_config['min_score']:
                logger.info(
                    f"{symbol} 评分 {score} < 最低评分 {self.scoring_config['min_score']}，跳过"
                )
                analysis_result['reason'] = f"评分 {score} < 最低评分 {self.scoring_config['min_score']}"
                analysis_result['grade'] = self._determine_grade(score, symbol)
                return analysis_result
            
            # 8. 确定信号等级（v6.16.10：币种差异化S级阈值）
            grade = self._determine_grade(score, symbol)

            # 8.0 v6.26：机制②轻度过热降级（仅震荡市路径，min_grade 过滤之前）
            if strategy_mode == 'ranging':
                grade = self._apply_overheat_downgrade(
                    grade, direction, indicators, klines,
                    self.risk_config.get('ranging_strategy', {})
                )
                if grade is None:
                    # 降档后低于最低允许等级（C 再降即禁开）
                    analysis_result['reason'] = "过热降级后低于最低允许等级，禁开"
                    return analysis_result

            analysis_result['grade'] = grade
            
            # 8.1 市场状态等级过滤（v6.17）
            min_grade = market_behavior.get('min_grade', 'C')
            if self.GRADE_ORDER.get(grade, 3) > self.GRADE_ORDER.get(min_grade, 3):
                logger.info(
                    f"{symbol} 市场状态不允许{grade}级信号（最低{min_grade}）",
                    market_state=market_state.value if market_state else 'RANGING'
                )
                analysis_result['reason'] = f"市场状态不允许{grade}级信号（最低{min_grade}）"
                return analysis_result
            
            # 9. 动态成交量过滤器（v6.16.10 新增，v6.17 集成市场状态 vol_boost）
            vol_boost = market_behavior.get('vol_boost', 0.0)
            vol_pass, vol_reason = self._check_volume_filter(symbol, grade, klines, vol_boost)
            if not vol_pass:
                logger.info(f"{symbol} 成交量过滤: {vol_reason}")
                analysis_result['reason'] = f"成交量过滤: {vol_reason}"
                return analysis_result
            
            # 10. 计算动态仓位大小（v6.16.10：波动率目标 + 同时持仓限制）
            position_size_usdt, fail_reason = await self._calculate_position_size(grade, current_price, symbol)
            
            if position_size_usdt is None:
                logger.warning(f"{symbol} 仓位计算失败: {fail_reason}")
                analysis_result['reason'] = f"仓位计算失败: {fail_reason}"
                return analysis_result
            
            # 10.0 用户决定：去掉市场状态仓位乘数（position_ratio_mult）对仓位大小的放大，
            # 仓位大小仅由「分配额 × 等级比例」决定（见 _calculate_position_size）。

            # 10.1 获取交易对精度信息
            precision_info = await self._get_symbol_precision(symbol)
            step_size = precision_info.get('stepSize', '0.001')
            tick_size = precision_info.get('tickSize', Decimal('0.01'))
            
            # 10.2 将USDT金额转换为币的数量
            quantity = position_size_usdt / current_price
            
            # 10.3 调整数量精度
            quantity = self._adjust_quantity_precision(quantity, step_size)
            
            # 10.4 检查最小下单量
            min_notional = Decimal(precision_info.get('minNotional', '5'))
            actual_notional = quantity * current_price
            
            if actual_notional < min_notional:
                logger.warning(
                    f"{symbol} 下单金额不足最小要求",
                    actual_notional=float(actual_notional),
                    min_notional=float(min_notional),
                    quantity=float(quantity)
                )
                analysis_result['reason'] = f"下单金额 {float(actual_notional):.2f}U < 最小要求 {float(min_notional)}U"
                return analysis_result
            
            logger.info(
                f"{symbol} 仓位计算完成",
                position_size_usdt=float(position_size_usdt),
                quantity=float(quantity),
                current_price=float(current_price),
                actual_notional=float(actual_notional)
            )
            
            # 11. 计算初始止损价格（v6.22：震荡市使用专用止损参数）
            if strategy_mode == 'ranging':
                ranging_risk = self.risk_config.get('ranging_strategy', {}).get('risk', {})
                sl_atr_mult = ranging_risk.get('stop_loss_atr', 2.0)
            else:
                grade_risk = self._get_grade_risk(grade)
                sl_atr_mult = grade_risk['stop_loss_atr_multiplier']
            
            if direction == 'LONG':
                initial_stop_loss = current_price - atr * Decimal(str(sl_atr_mult))
            else:
                initial_stop_loss = current_price + atr * Decimal(str(sl_atr_mult))
            
            # 12. 计算限价单价格
            limit_price = await self._get_optimized_price(symbol, direction)
            
            if limit_price:
                limit_price = self._adjust_price_precision(limit_price, tick_size)
            else:
                limit_price = current_price
            
            # 13. 生成信号
            signal = {
                'symbol': symbol,
                'direction': direction,
                'grade': grade,
                'score': score,
                'entry_price': limit_price,
                'initial_stop_loss': initial_stop_loss,
                'atr': atr,
                'leverage': self.binance_config['leverage'][grade],
                'position_size_usdt': position_size_usdt,
                'quantity': quantity,
                'position_ratio': self.binance_config['position_ratio'][grade],
                'timestamp': current_time,
                'market_state': market_state_name,  # v6.21：记录市场状态用于频率控制
                'price_change_1h': compute_1h_return(klines['1h']),  # 组合级熔断单币轨：目标币自身 1h 涨幅
                'tp1_price': self._calculate_tp_price(current_price, atr, direction, 1, grade),
                'tp2_price': self._calculate_tp_price(current_price, atr, direction, 2, grade),
            }
            
            logger.info(
                f"{symbol} 生成交易信号",
                direction=direction,
                grade=grade,
                score=score,
                entry_price=float(signal['entry_price']),
                position_size_usdt=float(position_size_usdt),
                quantity=float(quantity),
                tp1_price=float(signal['tp1_price']),
                tp2_price=float(signal['tp2_price'])
            )
            
            return signal
            
        except Exception as e:
            logger.error(
                f"{symbol} 分析失败",
                error=str(e),
                exc_info=True
            )
            analysis_result['reason'] = f"执行异常: {str(e)}"
            return analysis_result
    
    def _calculate_score(self, indicators: Dict, klines: Dict, market_state: str = 'STRONG_TREND') -> float:
        """
        计算综合评分（v6.22：震荡市使用不同权重）
        
        评分体系：
        - 趋势强度（趋势市25%/震荡市15%）
        - 形态质量（趋势市50%/震荡市60%）
        - 动量背离（趋势市25%/震荡市25%）
        
        Args:
            indicators: 各时间框架的技术指标
            klines: 各时间框架的K线数据
            market_state: 市场状态（STRONG_TREND/RANGING）
        
        Returns:
            综合评分（0-100）
        """
        score = 0.0
        
        # 震荡市使用不同权重（形态权重提高，趋势权重降低）
        if market_state == 'RANGING':
            ranging_config = self.risk_config.get('ranging_strategy', {})
            weights = ranging_config.get('scoring_weights', {
                'trend_strength': 0.15,
                'pattern_quality': 0.60,
                'momentum_divergence': 0.25,
            })
        else:
            weights = self.scoring_config['weights']
        
        # 趋势强度评分（40%）
        trend_score = self._calculate_trend_strength_score(indicators)
        score += trend_score * weights['trend_strength']
        
        # 形态质量评分（35%）
        pattern_score = self._calculate_pattern_quality_score(indicators, klines)
        score += pattern_score * weights['pattern_quality']
        
        # 动量背离评分（25%）
        momentum_score = self._calculate_momentum_divergence_score(indicators, klines)
        score += momentum_score * weights['momentum_divergence']
        
        # A级额外加分：4h RSI 在 35~65 之间 +2 分（v6.16.10）
        if '4h' in indicators:
            rsi_4h = indicators['4h']['RSI'].iloc[-1]
            bonus_config = self.scoring_config.get('a_level_bonus', {})
            if pd.notna(rsi_4h):
                rsi_low = bonus_config.get('rsi_low', 35)
                rsi_high = bonus_config.get('rsi_high', 65)
                if rsi_low <= rsi_4h <= rsi_high:
                    score += bonus_config.get('bonus', 2)
        
        logger.debug(
            "评分计算完成",
            trend_strength_score=trend_score,
            pattern_quality_score=pattern_score,
            momentum_divergence_score=momentum_score,
            total_score=score
        )
        
        return round(score, 2)
    
    def _calculate_trend_strength_score(self, indicators: Dict) -> float:
        """
        计算趋势强度评分
        
        评分标准：
        - 1h和4h时间框架的MA21 > MA55（上升趋势）：基础分60
        - 1h和4h时间框架的MA21 < MA55（下降趋势）：基础分50
        - MACD在零轴上方：+10分
        - 多时间框架趋势一致：+15分
        - ADX > 25（强趋势）：+15分
        
        Args:
            indicators: 技术指标字典
        
        Returns:
            趋势强度评分（0-100）
        """
        score = float(self.scoring_config['trend_strength']['base_score'])  # 基础分
        
        # 检查1h和4h的MA趋势
        if '1h' in indicators and '4h' in indicators:
            ma21_1h = indicators['1h']['MA21'].iloc[-1]
            ma55_1h = indicators['1h']['MA55'].iloc[-1]
            ma21_4h = indicators['4h']['MA21'].iloc[-1]
            ma55_4h = indicators['4h']['MA55'].iloc[-1]
            
            # 1h趋势
            trend_1h = ma21_1h > ma55_1h
            # 4h趋势
            trend_4h = ma21_4h > ma55_4h
            
            # 趋势一致加分
            if trend_1h == trend_4h:
                score += self.scoring_config['trend_strength']['consistency_bonus']
            
            # 上升趋势加分
            if trend_1h and trend_4h:
                score += self.scoring_config['trend_strength']['dual_uptrend_bonus']
            elif not trend_1h and not trend_4h:
                # 下降趋势也有一定分数
                pass
        
        # 检查MACD
        if '1h' in indicators:
            macd = indicators['1h']['MACD'].iloc[-1]
            if macd > 0:
                score += self.scoring_config['trend_strength']['macd_positive_bonus']
            else:
                score += self.scoring_config['trend_strength']['macd_negative_penalty']
        
        # 检查ADX趋势强度
        if '1h' in indicators:
            adx = indicators['1h']['ADX'].iloc[-1]
            if pd.notna(adx):
                if adx > self.scoring_config['trend_strength']['adx_strong_threshold']:
                    score += self.scoring_config['trend_strength']['adx_strong_bonus']
                elif adx >= self.scoring_config['trend_strength']['adx_medium_threshold']:
                    score += self.scoring_config['trend_strength']['adx_medium_bonus']
        
        return max(0, min(100, score))
    
    def _calculate_pattern_quality_score(self, indicators: Dict, klines: Dict) -> float:
        """
        计算形态质量评分
        
        识别的形态：
        - 阳包阴：看涨形态，+20分
        - 阴包阳：看跌形态，+20分
        - 突破回踩：趋势延续形态，+25分
        - 背离：反转信号，+30分
        
        Args:
            indicators: 技术指标字典
            klines: K线数据字典
        
        Returns:
            形态质量评分（0-100）
        """
        score = float(self.scoring_config['pattern_quality']['base_score'])  # 基础分
        
        if '1h' not in klines:
            return score
        
        # 获取最近几根K线
        recent_klines = klines['1h'][-5:]
        if len(recent_klines) < 3:
            return score
        
        # 转换为DataFrame方便计算
        df = pd.DataFrame(recent_klines)
        df['open'] = pd.to_numeric(df['open'])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        df['close'] = pd.to_numeric(df['close'])
        
        # 检测阳包阴形态（看涨）
        if self._detect_bullish_engulfing(df):
            score += self.scoring_config['pattern_quality']['bullish_engulfing_bonus']
            logger.debug("检测到阳包阴形态")
        
        # 检测阴包阳形态（看跌）
        if self._detect_bearish_engulfing(df):
            score += self.scoring_config['pattern_quality']['bearish_engulfing_bonus']
            logger.debug("检测到阴包阳形态")
        
        # 检测突破回踩形态
        if self._detect_breakout_pullback(df, indicators):
            score += self.scoring_config['pattern_quality']['breakout_pullback_bonus']
            logger.debug("检测到突破回踩形态")
        
        # 检测背离形态
        if self._detect_divergence(indicators, klines):
            score += self.scoring_config['pattern_quality']['divergence_bonus']
            logger.debug("检测到背离形态")
        
        return max(0, min(100, score))
    
    def _detect_bullish_engulfing(self, df: pd.DataFrame) -> bool:
        """
        检测阳包阴形态
        
        条件：
        1. 前一根K线是阴线（收盘<开盘）
        2. 当前K线是阳线（收盘>开盘）
        3. 当前K线的实体完全包含前一根K线的实体
        
        Args:
            df: K线数据DataFrame
        
        Returns:
            是否检测到阳包阴形态
        """
        if len(df) < 2:
            return False
        
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        
        # 前一根是阴线
        prev_is_bearish = prev['close'] < prev['open']
        # 当前是阳线
        curr_is_bullish = curr['close'] > curr['open']
        # 当前实体包含前一根实体
        curr_engulfs = (
            curr['open'] <= prev['close'] and
            curr['close'] >= prev['open']
        )
        
        return prev_is_bearish and curr_is_bullish and curr_engulfs
    
    def _detect_bearish_engulfing(self, df: pd.DataFrame) -> bool:
        """
        检测阴包阳形态
        
        条件：
        1. 前一根K线是阳线（收盘>开盘）
        2. 当前K线是阴线（收盘<开盘）
        3. 当前K线的实体完全包含前一根K线的实体
        
        Args:
            df: K线数据DataFrame
        
        Returns:
            是否检测到阴包阳形态
        """
        if len(df) < 2:
            return False
        
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        
        # 前一根是阳线
        prev_is_bullish = prev['close'] > prev['open']
        # 当前是阴线
        curr_is_bearish = curr['close'] < curr['open']
        # 当前实体包含前一根实体
        curr_engulfs = (
            curr['open'] >= prev['close'] and
            curr['close'] <= prev['open']
        )
        
        return prev_is_bullish and curr_is_bearish and curr_engulfs
    
    def _detect_breakout_pullback(self, df: pd.DataFrame, indicators: Dict) -> bool:
        """
        检测突破回踩形态
        
        条件：
        1. 价格突破MA21
        2. 回踩至MA21附近（距离MA21在1%以内）
        3. MA21呈上升趋势
        
        Args:
            df: K线数据DataFrame
            indicators: 技术指标字典
        
        Returns:
            是否检测到突破回踩形态
        """
        if '1h' not in indicators:
            return False
        
        if len(df) < 5:
            return False
        
        ma21 = indicators['1h']['MA21'].iloc[-1]
        current_close = df.iloc[-1]['close']
        
        # 检查是否在MA21附近（从配置读取阈值）
        proximity_pct = self.scoring_config['breakout_pullback']['proximity_pct']
        distance_ratio = abs(current_close - ma21) / ma21
        near_ma21 = distance_ratio < proximity_pct
        
        # 检查MA21是否上升
        ma21_prev = indicators['1h']['MA21'].iloc[-5]
        ma21_rising = ma21 > ma21_prev
        
        # 检查之前是否突破MA21
        prev_closes = df.iloc[-5:-1]['close']
        broke_above = any(close > ma21_prev for close in prev_closes)
        
        return near_ma21 and ma21_rising and broke_above
    
    def _detect_divergence(self, indicators: Dict, klines: Dict) -> bool:
        """
        检测背离形态
        
        条件：
        1. 价格创新高但RSI未创新高（顶背离）
        2. 价格创新低但RSI未创新低（底背离）
        
        Args:
            indicators: 技术指标字典
            klines: K线数据字典
        
        Returns:
            是否检测到背离形态
        """
        if '1h' not in indicators or '1h' not in klines:
            return False
        
        # 获取最近20根K线
        closes = [float(k['close']) for k in klines['1h'][-20:]]
        rsi_values = indicators['1h']['RSI'].iloc[-20:].values
        
        if len(closes) < 20 or len(rsi_values) < 20:
            return False
        
        # 检测顶背离
        price_high = max(closes[-10:])
        price_prev_high = max(closes[-20:-10])
        rsi_high = max(rsi_values[-10:])
        rsi_prev_high = max(rsi_values[-20:-10])
        
        if price_high > price_prev_high and rsi_high < rsi_prev_high:
            return True
        
        # 检测底背离
        price_low = min(closes[-10:])
        price_prev_low = min(closes[-20:-10])
        rsi_low = min(rsi_values[-10:])
        rsi_prev_low = min(rsi_values[-20:-10])
        
        if price_low < price_prev_low and rsi_low > rsi_prev_low:
            return True
        
        return False
    
    def _calculate_momentum_divergence_score(self, indicators: Dict, klines: Dict) -> float:
        """
        计算动量背离评分
        
        评分标准：
        - RSI在30-70之间（正常区间）：基础分60
        - RSI < 30（超卖）：+20分（买入机会）
        - RSI > 70（超买）：-20分（风险较高）
        - MACD柱状图为正：+10分
        - MACD柱状图为负：-10分
        - 存在背离：+15分
        
        Args:
            indicators: 技术指标字典
            klines: K线数据字典
        
        Returns:
            动量背离评分（0-100）
        """
        score = float(self.scoring_config['momentum_divergence']['base_score'])  # 基础分
        
        if '1h' in indicators:
            rsi = indicators['1h']['RSI'].iloc[-1]
            
            if pd.notna(rsi):
                if rsi < self.scoring_config['momentum_divergence']['rsi_oversold']:
                    # 超卖，买入机会
                    score += self.scoring_config['momentum_divergence']['rsi_oversold_bonus']
                elif rsi > self.scoring_config['momentum_divergence']['rsi_overbought']:
                    # 超买，风险较高
                    score += self.scoring_config['momentum_divergence']['rsi_overbought_penalty']
                else:
                    # 正常区间
                    score += self.scoring_config['momentum_divergence']['rsi_normal_bonus']
            
            # MACD柱状图
            macd_hist = indicators['1h']['MACD_Hist'].iloc[-1]
            if pd.notna(macd_hist):
                if macd_hist > 0:
                    score += self.scoring_config['momentum_divergence']['macd_hist_positive_bonus']
                else:
                    score += self.scoring_config['momentum_divergence']['macd_hist_negative_penalty']
            
            # 检测背离加分
            if self._detect_divergence(indicators, klines):
                score += self.scoring_config['momentum_divergence']['divergence_bonus']
        
        return max(0, min(100, score))
    
    def _check_trend_filter(
        self, 
        symbol: str, 
        indicators: Dict, 
        klines: Dict
    ) -> Tuple[bool, str]:
        """
        v6.16.10 趋势过滤器（硬性条件）
        
        多头方向：
        - 日线收盘价 > 日线 EMA55
        - 日线 EMA21 斜率 > 0.05%（最近5根日线线性回归）
        - 4h 价格回调至 EMA21 附近（≤ 1.5×ATR）
        - 1h 收盘价 > 1h EMA21
        
        禁止入场：
        - 日线 EMA21 斜率绝对值 < 0.03%
        - ATR/价格 > 4.5% 或 < 1.0%
        
        Returns:
            (是否通过, 方向或失败原因)
        """
        config = self.risk_config.get('trend_filter', {})
        if not config.get('enabled', True):
            return True, ""
        
        # 1. 日线趋势判断
        if '1d' not in indicators:
            return False, "日线数据缺失"
        
        df_1d = pd.DataFrame(klines['1d'])
        close_1d = Decimal(str(df_1d['close'].iloc[-1]))
        ema55_1d = indicators['1d']['EMA55'].iloc[-1]
        
        # 日线 EMA21 斜率（从配置读取窗口大小）
        slope_window = config.get('ema_slope_window', 5)
        ema21_series = indicators['1d']['MA21'].iloc[-slope_window:]
        if len(ema21_series) >= slope_window:
            x = np.arange(slope_window)
            slope, _ = np.polyfit(x, ema21_series.values, 1)
            slope_pct = slope / ema21_series.iloc[-1]
        else:
            slope_pct = 0
        
        # 禁止：斜率过平
        if abs(slope_pct) < config['ema_slope_flat']:
            return False, f"日线EMA21斜率过平({slope_pct*100:.2f}%)"
        
        # 确定方向
        if slope_pct > 0:
            direction = 'LONG'
        else:
            direction = 'SHORT'
        
        # 做多硬性条件
        if direction == 'LONG':
            if close_1d <= ema55_1d:
                return False, "日线收盘价未站上EMA55"
            if slope_pct < config['ema_slope_min']:
                return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% < 0.05%)"
        else:
            if close_1d >= ema55_1d:
                return False, "日线收盘价未跌破EMA55"
            if slope_pct > -config['ema_slope_min']:
                return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% > -0.05%)"
        
        # 2. 4h 价格回调检查
        if '4h' in indicators:
            atr_4h = indicators['4h']['ATR'].iloc[-1]
            ema21_4h = indicators['4h']['MA21'].iloc[-1]
            close_4h = Decimal(str(klines['4h'][-1]['close']))
            
            proximity = abs(close_4h - Decimal(str(ema21_4h)))
            max_proximity = Decimal(str(atr_4h)) * Decimal(str(config['ema21_proximity_atr_mult']))
            
            if proximity > max_proximity:
                return False, f"4h价格距EMA21过远({float(proximity):.2f} > {float(max_proximity):.2f})"
        
        # 3. 1h 收盘价检查
        if '1h' in indicators:
            ema21_1h = indicators['1h']['MA21'].iloc[-1]
            close_1h = Decimal(str(klines['1h'][-1]['close']))
            
            if direction == 'LONG' and close_1h <= Decimal(str(ema21_1h)):
                return False, "1h收盘价未站上EMA21"
            elif direction == 'SHORT' and close_1h >= Decimal(str(ema21_1h)):
                return False, "1h收盘价未跌破EMA21"
        
        # 4. ATR/价格检查
        if '1h' in indicators:
            atr_1h = Decimal(str(indicators['1h']['ATR'].iloc[-1]))
            atr_ratio = float(atr_1h / close_1h)
            prohibition = self.risk_config.get('prohibition', {})
            
            if atr_ratio > prohibition.get('atr_price_max', 0.045):
                return False, f"波动率过高(ATR/价格={atr_ratio*100:.1f}% > 4.5%)"
            if atr_ratio < prohibition.get('atr_price_min', 0.010):
                return False, f"波动率过低(ATR/价格={atr_ratio*100:.1f}% < 1.0%)"
        
        return True, direction
    
    @staticmethod
    def _bb_touch_votes(df_4h: pd.DataFrame, indicators: Dict,
                        entry_conditions: Dict) -> Tuple[int, int, list, float, float]:
        """BB半区投票（v6.29 由触轨判定改为半区判定）。

        价格位于布林中轨下方投多票、上方投空票、等于中轨平票。
        与量价确认机制③a 的半区校验逻辑统一，解决窄幅横盘行情下
        触轨判定（价格贴中轨距轨 47-53%）永远无法触发导致 24h+ 零信号的问题。

        返回 (多票增量, 空票增量, 命中描述列表, 距下轨比例, 距上轨比例)。
        禁用或数据缺失（含 BB_Middle 缺失）时返回 0 票，诊断比例取默认 1.0。
        """
        if not entry_conditions.get('bb_touch', True):
            return 0, 0, [], 1.0, 1.0
        if df_4h.empty or 'close' not in df_4h.columns:
            return 0, 0, [], 1.0, 1.0
        close_4h_raw = df_4h['close'].iloc[-1]
        if pd.isna(close_4h_raw):
            return 0, 0, [], 1.0, 1.0
        close_4h = float(close_4h_raw)
        bb_upper = _safe_last(indicators, '4h', 'BB_Upper')
        bb_middle = _safe_last(indicators, '4h', 'BB_Middle')
        bb_lower = _safe_last(indicators, '4h', 'BB_Lower')
        if bb_upper is None or bb_middle is None or bb_lower is None:
            return 0, 0, [], 1.0, 1.0
        bb_range = bb_upper - bb_lower
        if bb_range <= 0:
            return 0, 0, [], 1.0, 1.0
        dist_to_lower = (close_4h - bb_lower) / bb_range
        dist_to_upper = (bb_upper - close_4h) / bb_range
        long_delta = 0
        short_delta = 0
        conds = []
        if close_4h < bb_middle:
            long_delta += 1
            conds.append(f"BB下半区(距下轨{dist_to_lower*100:.1f}%)")
        elif close_4h > bb_middle:
            short_delta += 1
            conds.append(f"BB上半区(距上轨{dist_to_upper*100:.1f}%)")
        return long_delta, short_delta, conds, dist_to_lower, dist_to_upper

    @staticmethod
    def _rsi_extreme_votes(indicators: Dict,
                           entry_conditions: Dict) -> Tuple[int, int, list, float]:
        """RSI极端投票：超卖投多、超买投空（沿用原判定，仅迁移不复算）。

        返回 (多票增量, 空票增量, 命中描述列表, RSI 诊断值)。
        禁用或数据缺失时返回 0 票，诊断值取默认 0.0。
        """
        if not entry_conditions.get('rsi_extreme', True):
            return 0, 0, [], 0.0
        rsi = _safe_last(indicators, '4h', 'RSI')
        if rsi is None:
            return 0, 0, [], 0.0
        oversold = entry_conditions.get('rsi_oversold', 20)
        overbought = entry_conditions.get('rsi_overbought', 80)
        long_delta = 0
        short_delta = 0
        conds = []
        if rsi < oversold:
            long_delta += 1
            conds.append(f"RSI超卖({rsi:.1f}<{oversold})")
        if rsi > overbought:
            short_delta += 1
            conds.append(f"RSI超买({rsi:.1f}>{overbought})")
        return long_delta, short_delta, conds, rsi

    @staticmethod
    def _reversal_pattern_votes(df_4h: pd.DataFrame,
                                entry_conditions: Dict) -> Tuple[int, int, list]:
        """反转K线形态投票：看涨吞没投多、看跌吞没投空（沿用原判定，仅迁移）。"""
        if not entry_conditions.get('reversal_pattern', True):
            return 0, 0, []
        if len(df_4h) < 2:
            return 0, 0, []
        prev_open_raw = df_4h['open'].iloc[-2]
        prev_close_raw = df_4h['close'].iloc[-2]
        curr_open_raw = df_4h['open'].iloc[-1]
        curr_close_raw = df_4h['close'].iloc[-1]
        if any(pd.isna(v) for v in (prev_open_raw, prev_close_raw, curr_open_raw, curr_close_raw)):
            return 0, 0, []
        prev_open = float(prev_open_raw)
        prev_close = float(prev_close_raw)
        curr_open = float(curr_open_raw)
        curr_close = float(curr_close_raw)
        long_delta = 0
        short_delta = 0
        conds = []
        # 看涨吞没：前阴后阳，后实体包住前实体
        if (prev_close < prev_open and curr_close > curr_open and
                curr_open <= prev_close and curr_close >= prev_open):
            long_delta += 1
            conds.append("看涨吞没")
        # 看跌吞没：前阳后阴，后实体包住前实体
        if (prev_close > prev_open and curr_close < curr_open and
                curr_open >= prev_close and curr_close <= prev_open):
            short_delta += 1
            conds.append("看跌吞没")
        return long_delta, short_delta, conds

    def _vote_ranging_direction(self, indicators: Dict, klines: Dict,
                                entry_conditions: Dict) -> Tuple[Optional[str], str, list]:
        """三条件投票得出震荡反转候选方向（沿用原判定，仅迁移不改逻辑）。

        依次汇总 BB 触轨、RSI 极端、反转 K 线形态三项投票，多数方向获胜；
        无票或平票返回 None。
        """
        df_4h = pd.DataFrame(klines['4h'])
        long_votes = 0
        short_votes = 0
        conditions_met = []

        ld, sd, conds, dist_to_lower, dist_to_upper = self._bb_touch_votes(
            df_4h, indicators, entry_conditions)
        long_votes += ld
        short_votes += sd
        conditions_met.extend(conds)

        ld, sd, conds, rsi = self._rsi_extreme_votes(indicators, entry_conditions)
        long_votes += ld
        short_votes += sd
        conditions_met.extend(conds)

        ld, sd, conds = self._reversal_pattern_votes(df_4h, entry_conditions)
        long_votes += ld
        short_votes += sd
        conditions_met.extend(conds)

        if long_votes == 0 and short_votes == 0:
            bb_info = (
                f"BB距下轨{dist_to_lower*100:.1f}%/距上轨{dist_to_upper*100:.1f}%"
                if entry_conditions.get('bb_touch', True) else "BB=禁用"
            )
            rsi_info = (
                f"RSI={rsi:.1f}"
                if entry_conditions.get('rsi_extreme', True) else "RSI=禁用"
            )
            return None, f"无震荡入场条件满足({bb_info}, {rsi_info})", conditions_met

        if long_votes > short_votes:
            direction = 'LONG'
        elif short_votes > long_votes:
            direction = 'SHORT'
        else:
            return None, f"方向不一致(long={long_votes}, short={short_votes})", conditions_met

        return direction, "", conditions_met

    @staticmethod
    def _check_direction_alignment(direction: str, indicators: Dict,
                                   ranging_config: Dict) -> Tuple[bool, str]:
        """机制①：方向一致性对齐。

        校验日线均线排列与候选方向是否一致：多头排列禁止做空、空头排列禁止做多；
        均线缺失/NaN/粘合按 CP-2 保守拒绝（fallback_on_missing=false）。
        """
        config = ranging_config.get('direction_alignment', {})
        if not config.get('enabled', True):
            return True, ""
        fast = _safe_last(indicators, '1d', config.get('fast_ma', 'EMA21'))
        slow = _safe_last(indicators, '1d', config.get('slow_ma', 'EMA55'))
        if fast is None or slow is None or slow == 0:
            if config.get('fallback_on_missing', False):
                logger.warning("均线数据缺失，放行方向护栏")
                return True, ""
            return False, "方向一致性拒绝：日线均线数据缺失"
        stick_threshold = config.get('stick_threshold_pct', 0.3)
        if abs(fast - slow) / slow * 100 <= stick_threshold:
            return False, "方向一致性拒绝：日线均线粘合"
        if fast > slow and direction == 'SHORT':
            return False, "方向一致性拒绝：多头排列禁止做空"
        if fast < slow and direction == 'LONG':
            return False, "方向一致性拒绝：空头排列禁止做多"
        return True, ""

    @staticmethod
    def _safe_last_klines_value(klines: Dict, timeframe: str, field: str) -> Optional[float]:
        """安全读取指定周期 K 线最新字段值，数据缺失/异常返回 None（v6.28 提取公共逻辑）。"""
        tf_data = klines.get(timeframe)
        if not tf_data:
            return None
        df = pd.DataFrame(tf_data)
        if df.empty or field not in df.columns:
            return None
        raw = df[field].iloc[-1]
        if pd.isna(raw):
            return None
        return float(raw)

    @staticmethod
    def _evaluate_overheat(direction: str, indicators: Dict, klines: Dict, cfg: Dict) -> str:
        """机制②过热档位评估（供禁开与降级复用），返回 'ban'/'downgrade'/'ok'。"""
        if not cfg:
            return 'ok'
        close_1d = BTCEthStrategy._safe_last_klines_value(klines, '1d', 'close')
        if close_1d is None:
            return 'ok'

        fast = _safe_last(indicators, '1d', 'EMA21')
        slow = _safe_last(indicators, '1d', 'EMA55')
        rsi_4h = _safe_last(indicators, '4h', 'RSI')
        if fast is None or slow is None or fast == 0 or slow == 0 or rsi_4h is None:
            return 'ok'

        bias_fast = (close_1d - fast) / fast * 100
        bias_slow = (close_1d - slow) / slow * 100
        downgrade_enabled = cfg.get('downgrade_enabled', True)

        if direction == 'LONG':
            rsi_ban = cfg['rsi_extreme_long']
            rsi_warn = cfg['rsi_warn_long']
            over_fast_ban = bias_fast >= cfg['bias_fast_ban_pct']
            over_slow_ban = bias_slow >= cfg['bias_slow_ban_pct']
            over_fast_down = bias_fast >= cfg['bias_fast_downgrade_pct']
            over_slow_down = bias_slow >= cfg['bias_slow_downgrade_pct']
            rsi_ban_cond = rsi_4h > rsi_ban
            rsi_down_cond = rsi_warn < rsi_4h <= rsi_ban
        else:  # direction == 'SHORT'
            rsi_ban = cfg['rsi_extreme_short']
            rsi_warn = cfg['rsi_warn_short']
            over_fast_ban = bias_fast <= -cfg['bias_fast_ban_pct']
            over_slow_ban = bias_slow <= -cfg['bias_slow_ban_pct']
            over_fast_down = bias_fast <= -cfg['bias_fast_downgrade_pct']
            over_slow_down = bias_slow <= -cfg['bias_slow_downgrade_pct']
            rsi_ban_cond = rsi_4h < rsi_ban
            rsi_down_cond = rsi_ban <= rsi_4h < rsi_warn

        if over_fast_ban or over_slow_ban or rsi_ban_cond:
            return 'ban'
        if over_fast_down or over_slow_down or rsi_down_cond:
            return 'ban' if not downgrade_enabled else 'downgrade'
        return 'ok'

    def _check_overheat_ban(self, direction: str, indicators: Dict, klines: Dict,
                            ranging_config: Dict) -> Tuple[bool, str]:
        """机制②：重度过热禁开。乖离率或 RSI 进入极值区时拒绝入场。"""
        cfg = ranging_config.get('overheat_protection', {})
        if not cfg.get('enabled', True):
            return True, ""
        if self._evaluate_overheat(direction, indicators, klines, cfg) == 'ban':
            return False, "过热禁开：乖离率或RSI进入极值区"
        return True, ""

    def _apply_overheat_downgrade(self, grade: str, direction: str, indicators: Dict,
                                  klines: Dict, ranging_config: Dict) -> Optional[str]:
        """机制②：轻度过热降级一档（S→A→B→C，C 再降即返回 None 禁开）。"""
        cfg = ranging_config.get('overheat_protection', {})
        if not cfg.get('enabled', True):
            return grade
        if self._evaluate_overheat(direction, indicators, klines, cfg) != 'downgrade':
            return grade
        idx = self._GRADE_LADDER.index(grade) if grade in self._GRADE_LADDER else None
        if idx is None or idx + 1 >= len(self._GRADE_LADDER):
            return None
        new_grade = self._GRADE_LADDER[idx + 1]
        logger.info("轻度过热，信号降一档", before=grade, after=new_grade, direction=direction)
        return new_grade

    @staticmethod
    def _check_volume_confirm(direction: str, indicators: Dict, klines: Dict,
                              ranging_config: Dict) -> Tuple[bool, str]:
        """机制③a：量价确认（价格半区校验 + 缩量），数据缺失保守拒绝。"""
        cfg = ranging_config.get('volume_confirm', {})
        if not cfg.get('enabled', True):
            return True, ""
        close_4h = BTCEthStrategy._safe_last_klines_value(klines, '4h', 'close')
        if close_4h is None:
            return False, "量价确认拒绝：量价数据缺失"
        volume = BTCEthStrategy._safe_last_klines_value(klines, '4h', 'volume')
        if volume is None:
            return False, "量价确认拒绝：量价数据缺失"
        bb_middle = _safe_last(indicators, '4h', 'BB_Middle')
        vol_ma = _safe_last(indicators, '4h', 'Volume_MA')
        if bb_middle is None or vol_ma is None:
            return False, "量价确认拒绝：量价数据缺失"

        shrink_ratio = cfg.get('shrink_ratio', 1.1)  # 兜底默认值与配置一致（v6.28）
        # v6.28 逻辑修正：价格方向确认由「穿越中轨」改为「半区校验」。
        # 触下轨做多时价格必然在中轨下方，原要求 close > 中轨 形成逻辑死结，
        # 现改为做多要求收盘价处于布林带下半区（接近下轨的买入区），做空反之。
        if direction == 'LONG' and not close_4h < bb_middle:
            return False, "量价确认拒绝：收盘价未处于布林带下半区（做多需在下半区）"
        if direction == 'SHORT' and not close_4h > bb_middle:
            return False, "量价确认拒绝：收盘价未处于布林带上半区（做空需在上半区）"
        if not volume < shrink_ratio * vol_ma:
            return False, "量价确认拒绝：成交量未缩量"
        return True, ""

    def _check_volatility_regime(self, symbol: str, indicators: Dict,
                                 ranging_config: Dict) -> Tuple[bool, str]:
        """机制③b：波动突变 / 极端期检测（per-symbol 暂停，跨调度周期保持）。"""
        cfg = ranging_config.get('volatility_regime', {})
        if not cfg.get('enabled', True):
            return True, ""

        now = datetime.now()
        pause_until = self.symbol_extreme_pause_until.get(symbol)
        if pause_until is not None and now < pause_until:
            return False, f"极端期：{symbol}震荡反转暂停至{pause_until}"

        atr_short = _safe_last(indicators, '4h', 'ATR')
        atr_long = _safe_last(indicators, '4h', 'ATR_long')
        if atr_short is None or atr_long is None or atr_long == 0:
            logger.warning(f"{symbol} 波动数据缺失，跳过极端期检测")
            return True, ""

        spike_ratio = cfg.get('spike_ratio', 2.0)
        if atr_short / atr_long > spike_ratio:
            pause_bars = cfg.get('pause_bars', 12)
            bar_hours = cfg.get('pause_interval_hours', 4)
            pause_until = now + timedelta(hours=pause_bars * bar_hours)
            self.symbol_extreme_pause_until[symbol] = pause_until
            ratio = atr_short / atr_long
            logger.warning(
                f"{symbol} 标记极端期，暂停震荡反转",
                atr_short=atr_short, atr_long=atr_long, ratio=ratio,
                spike_ratio=spike_ratio, pause_until=str(pause_until)
            )
            return False, f"极端期：ATR短长比={ratio:.2f} 超阈值，暂停震荡反转"
        return True, ""

    def _check_ranging_entry(
        self,
        symbol: str,
        indicators: Dict,
        klines: Dict
    ) -> Tuple[bool, str]:
        """震荡市入场检查（v6.26 编排）：投票定方向后依序执行四机制，任一拒绝即短路。"""
        ranging_config = self.risk_config.get('ranging_strategy', {})
        if not ranging_config.get('enabled', True):
            return False, "震荡市策略未启用"
        try:
            if '4h' not in indicators or '4h' not in klines:
                return False, "4h数据缺失"
            entry_conditions = ranging_config.get('entry_conditions', {})
            # (0) 三条件投票得到候选方向
            direction, vote_reason, conditions_met = self._vote_ranging_direction(
                indicators, klines, entry_conditions)
            if direction is None:
                return False, vote_reason
            # (1) 波动突变：极端期暂停
            ok, reason = self._check_volatility_regime(symbol, indicators, ranging_config)
            if not ok:
                return False, reason
            # (2) 方向一致性
            ok, reason = self._check_direction_alignment(direction, indicators, ranging_config)
            if not ok:
                return False, reason
            # (3) 量价确认
            ok, reason = self._check_volume_confirm(direction, indicators, klines, ranging_config)
            if not ok:
                return False, reason
            # (4) 重度过热禁开
            ok, reason = self._check_overheat_ban(direction, indicators, klines, ranging_config)
            if not ok:
                return False, reason
            logger.info(f"{symbol} 震荡入场通过", direction=direction, conditions=conditions_met)
            return True, direction
        except Exception as e:
            logger.error(f"{symbol} 震荡入场检查异常", error=str(e), exc_info=True)
            return False, f"震荡入场检查异常: {str(e)}"
    
    def _determine_grade(self, score: float, symbol: str = None) -> str:
        """
        确定信号等级（v6.16.10：支持币种差异化S级阈值）
        
        Args:
            score: 综合评分
            symbol: 交易对（用于读取币种差异化S级阈值）
        
        Returns:
            等级（S/A/B/C）
        """
        thresholds = self.scoring_config['grade_thresholds']
        
        # 币种差异化 S 级阈值（v6.16.10）
        if symbol and symbol in self.symbol_config:
            s_threshold = self.symbol_config[symbol].get('s_min_score', thresholds['S'])
        else:
            s_threshold = thresholds['S']
        
        if score >= s_threshold:
            return 'S'
        elif score >= thresholds['A']:
            return 'A'
        elif score >= thresholds['B']:
            return 'B'
        else:
            return 'C'
    
    def _get_grade_risk(self, grade: str) -> Dict:
        """根据信号等级获取对应的风险参数
        
        Args:
            grade: 信号等级 (S/A/B/C)
        
        Returns:
            该等级的风险参数字典，若 signal_levels 不存在则返回全局 risk 配置
        """
        signal_levels = self.risk_config.get('signal_levels')
        if not signal_levels:
            return self.risk_config  # 向后兼容
        return signal_levels.get(grade, signal_levels.get('A', self.risk_config))
    
    def _determine_direction(self, indicators: Dict) -> str:
        """
        确定交易方向
        
        基于多时间框架的MA和MACD判断：
        - 上升趋势：MA21 > MA55 且 MACD > 0
        - 下降趋势：MA21 < MA55 且 MACD < 0
        
        Args:
            indicators: 技术指标字典
        
        Returns:
            方向（LONG/SHORT）
        """
        long_votes = 0
        short_votes = 0
        
        # 检查各时间框架（从配置读取，跳过日线等不适合方向判断的时间框架）
        direction_timeframes = [tf for tf in self.timeframes if tf in ('1h', '4h')]
        for timeframe in direction_timeframes:
            if timeframe in indicators:
                ma21 = indicators[timeframe]['MA21'].iloc[-1]
                ma55 = indicators[timeframe]['MA55'].iloc[-1]
                macd = indicators[timeframe]['MACD'].iloc[-1]
                
                if pd.notna(ma21) and pd.notna(ma55):
                    if ma21 > ma55:
                        long_votes += 1
                    else:
                        short_votes += 1
                
                if pd.notna(macd):
                    if macd > 0:
                        long_votes += 1
                    else:
                        short_votes += 1
        
        # 根据投票结果决定方向
        if long_votes > short_votes:
            return 'LONG'
        else:
            return 'SHORT'
    
    async def _check_prohibited_conditions(
        self, 
        symbol: str, 
        klines: Dict
    ) -> Tuple[bool, str]:
        """
        v6.16.10 禁止入场条件
        
        Returns:
            (是否允许入场, 禁止原因)
        """
        config = self.risk_config.get('prohibition', {})
        
        # 1. 最近6h内单根1h K线涨跌幅 > 5%
        if '1h' in klines:
            df_1h = pd.DataFrame(klines['1h'])
            recent_6 = df_1h.tail(6)
            for _, row in recent_6.iterrows():
                pct = abs((row['close'] - row['open']) / row['open'])
                if pct > config.get('kline_spike_6h_pct', 0.05):
                    return False, f"6h内出现单根K线涨跌幅{pct*100:.1f}% > 5%"
        
        # 2. 资金费率绝对值 > 0.05%
        try:
            funding_rate = await self.binance.get_funding_rate(symbol)
            if abs(funding_rate) > config.get('funding_rate_max_abs', 0.0005):
                return False, f"资金费率{funding_rate*100:.2f}% > 0.05%"
        except Exception as e:
            logger.warning(f"{symbol} 获取资金费率失败: {e}")
        
        # 3. 24h涨跌幅
        try:
            ticker = await self.binance.get_ticker(symbol)
            price_change_pct = float(ticker.get('priceChangePercent', 0)) / 100
            
            if price_change_pct > config.get('daily_change_long_max', 0.25):
                return False, f"24h涨幅{price_change_pct*100:.1f}% > 25%"
            if price_change_pct < config.get('daily_change_short_max', -0.20):
                return False, f"24h跌幅{price_change_pct*100:.1f}% < -20%"
        except Exception as e:
            logger.warning(f"{symbol} 获取24h涨跌幅失败: {e}")
        
        # 4. 买卖价差 > 0.3%
        try:
            orderbook = await self.binance.get_orderbook(symbol, limit=5)
            best_bid = Decimal(str(orderbook['bids'][0][0]))
            best_ask = Decimal(str(orderbook['asks'][0][0]))
            spread = float((best_ask - best_bid) / best_bid)
            
            if spread > config.get('spread_max', 0.003):
                return False, f"买卖价差{spread*100:.2f}% > 0.3%"
        except Exception as e:
            logger.warning(f"{symbol} 获取orderbook失败: {e}")
        
        return True, ""
    
    def _check_volume_filter(
        self, 
        symbol: str, 
        grade: str, 
        klines: Dict,
        vol_boost: float = 0.0
    ) -> Tuple[bool, str]:
        """
        v6.16.10 动态成交量过滤器（v6.17 支持市场状态 vol_boost）
        
        对 SOLUSDT 使用严格倍数，其他币种使用上表阈值。
        B/C 级不检查成交量。
        
        Args:
            symbol: 交易对
            grade: 信号等级
            klines: K线数据
            vol_boost: 成交量要求提升比例（v6.17 混合市 +20%）
        """
        config = self.risk_config.get('dynamic_volume', {})
        if not config.get('enabled', True):
            return True, ""
        
        # B/C 级不检查成交量
        if grade in ('B', 'C'):
            return True, ""
        
        # 获取币种配置
        symbol_cfg = self.symbol_config.get(symbol, {})
        vol_ratio = symbol_cfg.get('vol_ratio_base', {})
        required_mult = vol_ratio.get(grade, 0)
        
        if required_mult == 0:
            return True, ""
        
        # 应用市场状态成交量加成（v6.17）
        if vol_boost > 0:
            required_mult = required_mult * (1 + vol_boost)
            logger.debug(f"{symbol} 成交量要求提升{vol_boost*100:.0f}%: {required_mult:.2f}x")
        
        # 计算当前1h成交量 / 过去20h均量
        if '1h' not in klines:
            return True, ""
        
        df_1h = pd.DataFrame(klines['1h'])
        current_vol = float(df_1h['volume'].iloc[-1])
        avg_vol_20h = float(df_1h['volume'].iloc[-21:-1].mean())
        
        if pd.isna(avg_vol_20h) or avg_vol_20h == 0:
            return True, ""
        
        vol_ratio_actual = current_vol / avg_vol_20h
        
        if vol_ratio_actual < required_mult:
            return False, f"成交量不足({vol_ratio_actual:.1f}x < {required_mult}x)"
        
        return True, ""
    
    async def _calculate_position_size(
        self,
        grade: str,
        current_price: Decimal,
        symbol: str = None
    ) -> Tuple[Optional[Decimal], str]:
        """
        计算动态仓位大小（v6.16.10：波动率目标仓位 + 同时持仓限制）
        
        单笔风险 = 10U × (历史中位ATR% / 当前ATR%)，限制 [5U, 15U]
        
        Args:
            grade: 信号等级
            current_price: 当前价格
            symbol: 交易对
        
        Returns:
            (仓位大小 USDT 或 None, 失败原因或空字符串)
        """
        try:
            # 用户决定：开仓基数由「账户可用资金」
            # 改为「策略当月分配额 × 该信号等级百分比」。
            # 分配额通过 capital_mgr.get_effective_margin_limit()
            # 动态读取（DB 为主来源，config 兜底）。
            allocated_limit, _limit_src = await self.capital_mgr.get_effective_margin_limit()
            if allocated_limit is None:
                # 分配额取不到（fail-open / 三级均不可用）时，
                # 回退使用 config 的 trading.total_position_margin_limit（静态兜底）作为基数
                allocated_limit = Decimal(str(
                    self.config['trading']['total_position_margin_limit']
                ))
            else:
                allocated_limit = Decimal(str(allocated_limit))
            
            # 获取配置
            position_sizing_config = self.risk_config['position_sizing']
            safety_margin_ratio = Decimal(str(position_sizing_config['safety_margin_ratio']))
            
            # 同时持仓检查（v6.16.10）
            pm_config = self.risk_config.get('position_management', {})
            max_concurrent = pm_config.get('max_concurrent_positions', 2)
            active_positions = sum(
                1 for p in self.positions.values() if p.current_quantity > 0
            )
            if active_positions >= max_concurrent:
                reason = f"同时持仓数已达上限{max_concurrent}个"
                logger.warning(reason, active_positions=active_positions)
                return None, reason
            
            # 计算可用基数（分配额扣除安全垫，转译为「分配额 × (1 - safety_margin_ratio)」）
            usable_balance = allocated_limit * (Decimal('1') - safety_margin_ratio)

            # 检查最小可开仓保证金（min_position_margin，替代原 min_margin_usdt=100 门槛，
            # 避免分配额较小的策略被误拦；未配置则跳过门槛）
            min_margin = self.capital_mgr.get_min_position_margin()
            if min_margin is not None and usable_balance < Decimal(str(min_margin)):
                reason = f"可用资金不足({float(usable_balance):.2f}U < {float(min_margin)}U)"
                logger.warning(reason, usable_balance=float(usable_balance))
                return None, reason
            
            # 获取币种差异化仓位比例（v6.16.10）
            if symbol and symbol in self.symbol_config:
                if grade == 'S':
                    position_ratio = Decimal(str(
                        self.symbol_config[symbol].get('position_ratio_s', 0.50)
                    ))
                else:
                    position_ratio = Decimal(str(
                        self.binance_config['position_ratio'][grade]
                    ))
            else:
                position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
            
            # 计算仓位大小（用户决定放开 max 钳制，改为「分配额 × 等级比例」，
            # 仅按等级基数计算，不再被 max_single_position_usdt 压缩）
            position_size = usable_balance * position_ratio
            
            # 波动率目标仓位调整（v6.16.10）
            # 单笔风险 = 10U × (历史中位ATR% / 当前ATR%)，限制 [5U, 15U]
            if pm_config.get('volatility_target_risk', 0) > 0:
                target_risk = Decimal(str(pm_config['volatility_target_risk']))
                min_risk = Decimal(str(pm_config.get('volatility_target_min', 5)))
                max_risk = Decimal(str(pm_config.get('volatility_target_max', 15)))
                
                # 从动态ATR过滤器获取历史中位ATR%和当前ATR%
                if self.atr_filter and self.atr_filter.enabled:
                    stats = self.atr_filter.get_statistics(symbol)
                    median_atr_pct = Decimal(str(stats.get('percentile_50', 1.0)))
                    current_atr_pct = Decimal(str(stats.get('current_atr_pct', 1.0)))
                    
                    if current_atr_pct > 0 and median_atr_pct > 0:
                        vol_ratio = median_atr_pct / current_atr_pct
                        risk_amount = target_risk * vol_ratio
                        risk_amount = max(min_risk, min(max_risk, risk_amount))
                        
                        # 调整仓位：position_size × (risk_amount / target_risk)
                        position_size = position_size * (risk_amount / target_risk)
                        logger.info(
                            "波动率目标仓位调整",
                            target_risk=float(target_risk),
                            median_atr_pct=float(median_atr_pct),
                            current_atr_pct=float(current_atr_pct),
                            vol_ratio=float(vol_ratio),
                            risk_amount=float(risk_amount),
                            adjusted_position_size=float(position_size)
                        )
            
            logger.info(
                "仓位计算完成",
                allocated_limit=float(allocated_limit),
                usable_balance=float(usable_balance),
                position_ratio=float(position_ratio),
                position_size=float(position_size)
            )
            
            return position_size, ""
            
        except Exception as e:
            logger.error(
                "计算仓位大小失败",
                error=str(e),
                exc_info=True
            )
            return None, f"计算异常: {str(e)[:50]}"
    
    async def _get_optimized_price(
        self,
        symbol: str,
        direction: str
    ) -> Optional[Decimal]:
        """
        获取优化的限价单价格
        
        做多使用买一价，做空使用卖一价，可节省约60%手续费
        
        Args:
            symbol: 交易对
            direction: 方向（LONG/SHORT）
        
        Returns:
            优化的价格或None
        """
        try:
            # 检查是否启用限价单优化
            if not self.binance_config.get('order_optimization', {}).get('use_limit_order', False):
                return None
            
            # 获取订单簿
            orderbook = await self.binance.get_orderbook(symbol, limit=5)
            
            if direction == 'LONG':
                # 做多使用买一价
                if self.binance_config['order_optimization']['use_buy_one_price']:
                    buy_one_price = Decimal(str(orderbook['bids'][0][0]))
                    logger.debug(f"做多使用买一价: {buy_one_price}")
                    return buy_one_price
            else:
                # 做空使用卖一价
                if self.binance_config['order_optimization']['use_sell_one_price']:
                    sell_one_price = Decimal(str(orderbook['asks'][0][0]))
                    logger.debug(f"做空使用卖一价: {sell_one_price}")
                    return sell_one_price
            
            return None
            
        except Exception as e:
            logger.error(
                "获取优化价格失败",
                error=str(e),
                exc_info=True
            )
            return None
    
    def _calculate_tp_price(
        self,
        entry_price: Decimal,
        atr: Decimal,
        direction: str,
        tp_level: int,
        grade: str = 'A'
    ) -> Decimal:
        """
        计算止盈价格
        
        Args:
            entry_price: 入场价格
            atr: ATR值
            direction: 方向
            tp_level: 止盈级别（1或2）
            grade: 信号等级（S/A/B/C），用于动态读取对应的止盈参数
        
        Returns:
            止盈价格
        """
        grade_risk = self._get_grade_risk(grade)
        partial_config = grade_risk['partial_take_profit']
        
        if tp_level == 1:
            atr_multiplier = Decimal(str(partial_config['tp1_atr_multiplier']))
        else:
            atr_multiplier = Decimal(str(partial_config['tp2_atr_multiplier']))
        
        if direction == 'LONG':
            return entry_price + atr * atr_multiplier
        else:
            return entry_price - atr * atr_multiplier
    
    async def _get_symbol_precision(self, symbol: str) -> Dict:
        """
        获取交易对精度信息（带缓存）
        
        Args:
            symbol: 交易对
        
        Returns:
            精度信息字典，包含 quantityPrecision, pricePrecision, stepSize 等
        """
        if symbol not in self.symbol_precision:
            try:
                precision_info = await self.binance.get_symbol_info(symbol)
                self.symbol_precision[symbol] = precision_info
                logger.info(
                    f"{symbol} 精度信息已缓存",
                    quantity_precision=precision_info.get('quantityPrecision'),
                    price_precision=precision_info.get('pricePrecision'),
                    step_size=precision_info.get('stepSize'),
                    tick_size=precision_info.get('tickSize')
                )
            except Exception as e:
                logger.error(
                    f"{symbol} 获取精度信息失败",
                    error=str(e)
                )
                # 使用默认精度
                self.symbol_precision[symbol] = {
                    'quantityPrecision': 3,
                    'pricePrecision': 2,
                    'stepSize': '0.001',
                    'tickSize': Decimal('0.01')
                }
        
        return self.symbol_precision[symbol]
    
    def _adjust_quantity_precision(
        self,
        quantity: Decimal,
        step_size: str
    ) -> Decimal:
        """
        调整数量精度（向下取整到stepSize的整数倍）
        
        Args:
            quantity: 原始数量
            step_size: 步长（如 '0.001'）
        
        Returns:
            调整后的数量
        """
        if not step_size or step_size == '0':
            return quantity
        
        step = Decimal(step_size)
        # 向下取整到stepSize的整数倍
        adjusted = (quantity // step) * step
        
        return adjusted
    
    def _adjust_price_precision(
        self,
        price: Decimal,
        tick_size: Decimal
    ) -> Decimal:
        """
        调整价格精度（四舍五入到tickSize的整数倍）
        
        注意：价格应该四舍五入到最近的tickSize整数倍
        
        Args:
            price: 原始价格
            tick_size: 价格步长
        
        Returns:
            调整后的价格
        """
        if not tick_size or tick_size == 0:
            return price
        
        # 四舍五入到tickSize的整数倍
        # 使用 Decimal 的 quantize 方法进行精确的四舍五入
        adjusted = (price / tick_size).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * tick_size
        
        return adjusted
    
    async def _get_account_equity(self, symbol: str) -> Optional[float]:
        """获取账户权益（USDT），获取失败或权益无效返回 None（调用方降级不限制）

        Args:
            symbol: 交易对（仅用于日志定位）

        Returns:
            float: 账户权益（USDT）；获取失败或权益无效返回 None
        """
        try:
            account_info = await self.binance.get_account_info()
        except Exception as e:
            logger.warning(
                "获取账户信息失败，跳过总持仓保证金检查",
                symbol=symbol,
                error=str(e),
            )
            return None
        equity = float(account_info.get('totalMarginBalance', 0) or 0)
        if equity <= 0:
            logger.warning(
                "账户权益无效，跳过总持仓保证金检查",
                symbol=symbol,
                account_equity=equity,
            )
            return None
        return equity

    async def _check_total_margin_ratio(self, signal: Dict) -> bool:
        """检查总持仓保证金是否超过账户权益比例阈值（动态读取配置）

        Args:
            signal: 交易信号（含 quantity、entry_price、leverage）

        Returns:
            bool: True 可开仓；False 超限拒绝开仓
        """
        # 1. 动态读取阈值（每次重新读取，保证配置更新立即生效）
        ratio_limit = self.capital_mgr.get_account_ratio_cap()
        if ratio_limit is None or ratio_limit <= 0:
            return True

        # 2. 获取账户权益（失败或无效时降级不限制）
        account_equity = await self._get_account_equity(signal['symbol'])
        if account_equity is None:
            return True

        # 3. 当前持仓保证金 + 新开仓保证金
        current_total_margin = self._calc_current_total_margin()
        # 统一保证金口径：数量 × 入场价 × contractSize / 杠杆，走 position_baseline 唯一实现
        # （contractSize=1：USDT 本位永续合约）
        new_margin = calc_position_margin(
            signal['quantity'],
            signal['entry_price'],
            DEFAULT_CONTRACT_SIZE,
            max(float(signal['leverage']), _MIN_LEVERAGE),
        )

        # 4. 超限判断
        total_margin = current_total_margin + new_margin
        if total_margin / account_equity > ratio_limit:
            logger.warning(
                "总持仓保证金超限，拒绝开仓",
                symbol=signal['symbol'],
                current_margin=round(current_total_margin, 2),
                new_margin=round(new_margin, 2),
                margin_ratio=round(total_margin / account_equity, 4),
                ratio_limit=ratio_limit,
            )
            return False

        return True

    def _calc_current_total_margin(self) -> float:
        """统计 MTPCS 策略当前全部持仓的保证金总和（统一保证金口径）

        单仓保证金 = 名义价值 / 杠杆 = (数量 × 入场价 × contractSize) / 杠杆。
        USDT 本位永续合约 contractSize=1；grade 未知或无效时取配置中最小杠杆保守高估保证金。
        具体累加由 shared.position_baseline.calc_graded_positions_margin 唯一实现。

        Returns:
            float: 当前总持仓保证金（USDT）
        """
        return calc_graded_positions_margin(
            self.positions.values(),
            self.binance_config['leverage'],
            _DEFAULT_LEVERAGE,
        )

    def build_positions_report(self) -> tuple:
        """构建 MTPCS 策略持仓上报数据（供 main.py 统一调用）

        同一套持仓快照在"开仓成功后立即上报"与"周期末上报"两处复用，
        避免重复构造。返回三份字典：
        - positions:    strategy_states 表持仓快照（含方向/入场价/数量/订单ID等）
        - margin_dict:  {symbol: 保证金}，按 当前数量×入场价/等级杠杆 计算
        - qty_dict:     {symbol: 当前持仓数量}

        杠杆/grage 口径与 _calc_current_total_margin 保持一致：
        单仓保证金 = 名义价值 / 杠杆 = (当前数量 × 入场价) / 等级杠杆。
        grade 未知或无效时取配置中最小杠杆保守高估保证金（防除零兜底）。

        Returns:
            (positions, margin_dict, qty_dict)
        """
        leverage_config = self.binance_config.get('leverage', {})
        positions = {}
        margin_dict = {}
        qty_dict = {}

        for sym, pos in self.positions.items():
            positions[sym] = {
                "direction": pos.direction,
                "entry_price": float(pos.entry_price) if pos.entry_price else None,
                "quantity": float(pos.initial_quantity) if pos.initial_quantity else 0,
                "current_quantity": float(pos.current_quantity) if pos.current_quantity else 0,
                "entry_time": str(pos.entry_time) if pos.entry_time else "",
                "entry_order_id": pos.entry_order_id,
                "stop_loss_order_id": pos.stop_loss_order_id,
                "tp1_order_id": pos.tp1_order_id,
                "tp2_order_id": pos.tp2_order_id,
            }

            quantity = float(pos.current_quantity) if pos.current_quantity else 0.0
            entry_price = float(pos.entry_price) if pos.entry_price else 0.0
            qty_dict[sym] = quantity

            # 仅对有效的（数量>0 且 有入场价）持仓计算保证金，其余不下发（平仓后由 DELETE 清除）
            if quantity > 0 and entry_price > 0:
                if pos.grade and pos.grade in leverage_config:
                    leverage = leverage_config[pos.grade]
                else:
                    # grade 未知或无效：取配置中最小杠杆保守高估保证金（与保证金风控同口径）
                    leverage = min(leverage_config.values()) if leverage_config else _DEFAULT_LEVERAGE
                if leverage and leverage > 0:
                    # 与 _calc_current_total_margin 同口径：数量 × 入场价 × contractSize / 杠杆
                    # （USDT 本位永续合约 contractSize=1），统一走 calc_position_margin
                    margin_dict[sym] = calc_position_margin(quantity, entry_price, 1.0, leverage)

        return positions, margin_dict, qty_dict

    async def execute_signal(self, signal: Dict) -> bool:
        """执行交易信号（含加仓统一托管分派，v6.28）

        决策链：
        ① 无持仓/已清仓 → 新开仓 _open_new_position
        ② 反向信号拒绝加仓
        ③ 已进入分批止盈（tp1/tp2_hit）拒绝加仓
        ④ 持仓浮亏拒绝加仓
        否则 → 浮盈同向加仓 _add_position

        Args:
            signal: 交易信号

        Returns:
            是否执行成功
        """
        symbol = signal['symbol']

        # 组合级熔断闸门：任何开仓/加仓决策前统一否决性拦截（覆盖新开仓与浮盈加仓路径）
        # 组合级独立生效（与单币涨幅无关）；单币涨幅缺失时仅单币级 fail-open（guard 内处理）
        if self.circuit_breaker is not None:
            index_hour = floor_index_hour()
            pool_index = await self.circuit_breaker.load_index(index_hour)
            allow, level = self.circuit_breaker.guard(
                signal["direction"], symbol, signal.get("price_change_1h"), pool_index
            )
            if not allow:
                logger.info(
                    "组合级熔断拦截开仓",
                    symbol=symbol,
                    direction=signal["direction"],
                    level=level,
                    pool_index=pool_index,
                )
                return False

        pos = self.positions.get(symbol)

        # ① 无持仓（或已清仓）→ 新开仓路径（原 execute_signal 逻辑主体拆分）
        if pos is None or pos.current_quantity <= 0:
            return await self._open_new_position(signal)

        # ② 决策1：反向信号拒绝加仓
        if pos.direction != signal['direction']:
            logger.info(
                f"{symbol} 反向信号拒绝加仓",
                position_direction=pos.direction,
                signal_direction=signal['direction'],
                grade=signal.get('grade'),
                score=signal.get('score')
            )
            return False

        # ③ 决策2：已进入分批止盈，拒绝加仓
        if pos.tp1_hit or pos.tp2_hit:
            logger.info(
                f"{symbol} 已进入分批止盈，拒绝加仓",
                tp1_hit=pos.tp1_hit,
                tp2_hit=pos.tp2_hit,
                grade=signal.get('grade'),
                score=signal.get('score')
            )
            return False

        # ④ 决策3：持仓浮亏，拒绝加仓
        if not await self._is_position_profitable(symbol, pos):
            logger.info(
                f"{symbol} 持仓浮亏，拒绝加仓",
                direction=pos.direction,
                entry_price=float(pos.entry_price) if pos.entry_price else None,
                grade=signal.get('grade'),
                score=signal.get('score')
            )
            return False

        # ⑤ 浮盈同向 → 加仓统一托管
        return await self._add_position(symbol, pos, signal)

    async def _open_new_position(self, signal: Dict) -> bool:
        """新开仓主流程（v6.28 重构瘦身）

        负责：频率控制记录 → 入场下单（_place_entry_order）→ 下保护单（硬止损/TP1/TP2）
        → 初始化持仓状态。非加仓场景（单笔持仓）行为与 v6.28 原逻辑完全一致。

        Args:
            signal: 交易信号

        Returns:
            是否执行成功
        """
        symbol = signal['symbol']
        try:
            logger.info(
                f"执行交易信号: {symbol}",
                direction=signal['direction'],
                grade=signal['grade'],
                score=signal['score']
            )

            # 开仓互斥（方案C）：同名币已由对家策略持有未平开仓单则跳过，防互留保护单
            if await is_symbol_owned_by_other(
                self.db_manager,
                symbol,
                self.my_record_name,
                self._competing_record_names,
            ):
                logger.info(
                    f"{symbol} 已被其他策略持有未平开仓单，跳过开仓（归属互斥）",
                    competing=self._competing_record_names,
                )
                try:
                    await self.notification.send(
                        message=f"{symbol} 开仓被归属互斥拦截（其他策略已持有该币开仓单），跳过",
                        level="warning",
                        project="btc_eth",
                    )
                except Exception as _e:
                    logger.warning("发送归属互斥告警失败", error=str(_e))
                return False

            # 记录交易（频率控制）
            await self.frequency_controller.record_trade(symbol, signal['timestamp'])

            # 入场下单（设置杠杆 + 仓位检查 + 限价单 + 等待成交）
            entry_order = await self._place_entry_order(symbol, signal)
            if entry_order is None:
                return False

            # 下硬止损/TP1/TP2 保护单（任一失败终止开仓）
            order_ids, ok = await self._place_entry_protection_orders(symbol, signal)
            if not ok:
                return False

            # 初始化持仓状态并保存
            position = self._build_position_state(signal, entry_order.get('orderId'), order_ids)
            self.positions[symbol] = position

            logger.info(
                f"交易信号执行完成: {symbol}",
                entry_order_id=position.entry_order_id,
                stop_loss_order_id=position.stop_loss_order_id,
                tp1_order_id=position.tp1_order_id
            )
            return True

        except Exception as e:
            logger.error(f"执行交易信号失败: {symbol}", error=str(e), exc_info=True)
            await self._send_signal_error_notification(symbol, e)
            return False

    async def _place_entry_protection_orders(
        self, symbol: str, signal: Dict
    ) -> Tuple[Optional[Dict[str, int]], bool]:
        """新开仓保护单：硬止损 + TP1/TP2（v6.28 重构拆分子函数）

        全部读配置不硬编码；任一保护单失败返回 (None, False) 终止开仓。
        """
        direction = signal['direction']
        stop_side = "SELL" if direction == "LONG" else "BUY"
        grade_risk = self._get_grade_risk(signal.get('grade', 'A'))
        partial_cfg = grade_risk['partial_take_profit']
        tp1_ratio = Decimal(str(partial_cfg['tp1_close_ratio']))
        tp2_ratio = Decimal(str(partial_cfg['tp2_close_ratio']))
        stop_offset = Decimal(str(self.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        tp_offset = Decimal(str(self.risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))

        # 1. 硬止损单（全量）
        initial_stop = Decimal(str(signal['initial_stop_loss']))
        stop_limit = self._apply_limit_offset(initial_stop, stop_offset, direction)
        logger.info(
            f"{symbol} 下止损限价单",
            stop_side=stop_side,
            stop_price=float(initial_stop),
            limit_price=float(stop_limit),
            quantity=float(signal['quantity'])
        )
        stop_order_id = await self._place_conditional_order_and_record(
            symbol, stop_side, "STOP", initial_stop, stop_limit,
            signal['quantity'], "STOP_LOSS", self.strategy_name
        )
        if stop_order_id is None:
            logger.error(f"{symbol} 止损单下单失败，终止开仓")
            return None, False

        # 2/3. TP1/TP2 止盈单（按等级比例，统一走 _place_tp_order）
        order_ids = {}
        for level, ratio in ((1, tp1_ratio), (2, tp2_ratio)):
            tp_price = Decimal(str(signal[f'tp{level}_price']))
            tp_limit = self._apply_limit_offset(tp_price, tp_offset, direction)
            tp_qty = signal['quantity'] * ratio
            tp_id = await self._place_tp_order(
                symbol, stop_side, tp_price, tp_limit, tp_qty, f"TP{level}", self.strategy_name
            )
            if tp_id is None:
                logger.error(f"{symbol} TP{level}止盈单下单失败，终止开仓")
                return None, False
            order_ids[level] = tp_id
        return {'stop': stop_order_id, 'tp1': order_ids[1], 'tp2': order_ids[2]}, True

    @staticmethod
    def _build_position_state(
        signal: Dict, entry_order_id: Optional[int], order_ids: Dict[str, int]
    ) -> PositionState:
        """根据信号与保护单ID构建新持仓状态（v6.28 重构拆分子函数）"""
        position = PositionState()
        position.entry_price = signal['entry_price']
        position.entry_time = signal['timestamp']
        position.direction = signal['direction']
        position.initial_quantity = signal['quantity']
        position.current_quantity = signal['quantity']
        position.atr = signal['atr']
        position.grade = signal.get('grade', 'A')
        position.entry_order_id = entry_order_id
        position.stop_loss_order_id = order_ids['stop']
        position.tp1_order_id = order_ids['tp1']
        position.tp2_order_id = order_ids['tp2']
        return position

    async def _send_signal_error_notification(self, symbol: str, error: Exception) -> None:
        """信号执行失败时发送飞书错误通知（v6.28 重构拆分子函数）"""
        try:
            await self.notification.send_error_notification(
                strategy=self.strategy_name,
                error_message=f"{symbol} 信号执行失败: {str(error)}",
                symbol=symbol
            )
        except Exception as notify_error:
            logger.error(f"{symbol} 发送错误通知失败", error=str(notify_error))

    async def _place_entry_order(self, symbol: str, signal: Dict) -> Optional[Dict]:
        """入场下单公共逻辑（新开仓与加仓复用，v6.28）

        负责：设置杠杆 → 开仓前检查 → 下限价单 → 等待成交。

        Args:
            symbol: 交易对
            signal: 交易信号

        Returns:
            成交后的订单信息 dict；可预期失败返回 None（已记日志），意外异常向上抛
        """
        # 1. 设置杠杆倍数（必须为整数，覆盖层可能产生浮点数）
        leverage = int(signal['leverage']) if signal['leverage'] > 0 else 1
        try:
            logger.info(f"{symbol} 设置杠杆倍数: {signal['leverage']} → {leverage}")
            await self.binance.set_leverage(symbol, leverage)
        except Exception as e:
            logger.error(f"{symbol} 设置杠杆失败: {e}，终止交易")
            return None

        # 2. 开仓前检查（总仓位上限 + 总保证金比例）
        if not await self._check_entry_limits(symbol, signal):
            return None

        # 3. 下限价单开仓 + 等待成交
        return await self._place_and_wait_entry_order(symbol, signal)

    async def _check_entry_limits(self, symbol: str, signal: Dict) -> bool:
        """开仓前检查：总仓位不超过分配上限 + 总保证金比例（v6.28 拆分子函数）

        Args:
            symbol: 交易对
            signal: 交易信号

        Returns:
            True = 通过；False = 拒绝（已记日志）
        """
        # 2. 开仓前检查：总持仓保证金不超过生效限额
        #    口径统一为保证金（R3）：允许开仓 ⟺ 当前占用 + 新开仓保证金 ≤ 生效限额
        #    生效限额由 CapitalManager 统一解析（DB 月度分配额 → config → 静态兜底）
        #    判定逻辑由 shared.position_baseline.check_entry_within_limit 唯一实现（两策略共用）
        if not await check_entry_within_limit(self, symbol, signal, _MIN_LEVERAGE):
            # 被可用资金分配额拦截：回写拦截原因供 main.py 飞书推送区分展示
            signal['reject_reason'] = "可用资金分配额超限拦截"
            return False

        # 2.1 总持仓保证金不超过账户权益比例阈值（动态读取配置）
        if not await self._check_total_margin_ratio(signal):
            logger.warning(
                "总持仓保证金比例超限，已拒绝开仓",
                symbol=symbol,
                grade=signal.get('grade'),
                direction=signal.get('direction'),
            )
            # 被保证金比例拦截：回写拦截原因供 main.py 飞书推送区分展示
            signal['reject_reason'] = "总持仓保证金比例超限拦截"
            return False
        return True

    async def _place_and_wait_entry_order(self, symbol: str, signal: Dict) -> Optional[Dict]:
        """下限价单开仓并等待成交（v6.28 拆分子函数）

        Args:
            symbol: 交易对
            signal: 交易信号

        Returns:
            成交后的订单信息 dict；超时未成交返回 None（已取消订单）
        """
        # 确定开仓方向 + 下限价单开仓
        entry_side = "BUY" if signal['direction'] == "LONG" else "SELL"
        logger.info(
            f"{symbol} 下限价单开仓",
            side=entry_side,
            quantity=float(signal['quantity']),
            entry_price=float(signal['entry_price'])
        )
        entry_order = await self.binance.place_order(
            symbol=symbol,
            side=entry_side,
            quantity=signal['quantity'],
            price=signal['entry_price'],
            order_type="LIMIT"
        )
        entry_order_id = entry_order.get('orderId')
        logger.info(
            f"{symbol} 开仓订单已下单",
            order_id=entry_order_id,
            status=entry_order.get('status')
        )

        # 等待限价单成交（超时时间从配置读取）
        entry_timeout = self.risk_config.get('position_sizing', {}).get('entry_order_timeout_seconds', 60)
        entry_order = await self._wait_for_order_fill(
            symbol, entry_order_id, entry_timeout
        )
        if not entry_order:
            # 超时未成交，取消限价单，避免后续价格到达时突然成交
            logger.warning(f"{symbol} 限价单超时未成交，取消订单")
            try:
                await self.binance.cancel_order(symbol, entry_order_id)
            except Exception as cancel_e:
                logger.warning(f"{symbol} 取消限价单失败", error=str(cancel_e))
            return None

        return entry_order

    async def _add_position(self, symbol: str, position: PositionState, signal: Dict) -> bool:
        """加仓主流程（v6.28 加仓统一托管）：开新仓成交 → 取消旧单 → 合并 → 重建条件单。"""
        try:
            # 顺序1：开新仓成交（复用入场下单公共逻辑）
            entry_order = await self._place_entry_order(symbol, signal)
            if entry_order is None:
                return False

            # 顺序2：同步交易所实际持仓量（确认加仓成交且未触发平仓）
            sync = await self._sync_position_with_exchange(symbol, position, close_reason="ADD_POSITION")
            if sync.get('closed') or sync.get('actual_quantity') is None:
                logger.warning(
                    f"{symbol} 加仓后同步交易所持仓异常，回滚加仓流程",
                    sync_result=sync
                )
                return False

            # 顺序3：取消旧条件单（stop_loss/tp1/tp2/trailing 逐个取消）
            if not await self._cancel_orders_for_rebuild(symbol, position):
                position.rebuild_pending = True
                logger.warning(f"{symbol} 加仓：取消旧条件单失败，标记重建待收敛")
                return False

            # 顺序4：合并持仓（加权均价 + 更新数量/ATR/等级，原地修改）
            self._merge_position(position, signal, entry_order, sync['actual_quantity'])

            # 顺序5：按合并后总持仓重建条件单
            if not await self._rebuild_condition_orders(symbol, position):
                position.rebuild_pending = True
                logger.warning(f"{symbol} 加仓：重建条件单失败，标记重建待收敛")
                return False

            logger.info(
                f"{symbol} 加仓成功",
                added_quantity=float(signal['quantity']),
                total_quantity=float(position.current_quantity),
                entry_price=float(position.entry_price),
                grade=position.grade
            )
            return True

        except Exception as e:
            logger.error(f"{symbol} 加仓失败", error=str(e), exc_info=True)
            return False

    @staticmethod
    def _merge_position(
        position: PositionState,
        signal: Dict,
        entry_order: Dict,
        actual_quantity: Decimal,
    ) -> None:
        """合并持仓（原地修改，v6.28）

        加权均价 = (旧entry×旧量 + 新entry×本次增量) / 合并后总量；
        更新 initial/current_quantity 为交易所实际总量、ATR/grade 取新信号；
        保留 highest/lowest、tp1_hit/tp2_hit、trailing、entry_time 等状态。
        加仓窗口已保证 tp1_hit/tp2_hit 均为 False，故合并前
        current_quantity == initial_quantity，以 initial_quantity 作为旧量计算增量。

        Args:
            position: 持仓状态（原地修改）
            signal: 加仓信号
            entry_order: 本次加仓订单信息
            actual_quantity: 交易所实际持仓量（合并后总持仓）
        """
        old_quantity = position.initial_quantity
        old_entry = position.entry_price or Decimal('0')
        new_entry = Decimal(str(signal['entry_price']))
        total_quantity = Decimal(str(actual_quantity))
        added_quantity = total_quantity - old_quantity

        # 加权均价：仅当确有增量成交时重算，避免除零
        if added_quantity > 0 and old_quantity > 0:
            position.entry_price = (old_entry * old_quantity + new_entry * added_quantity) / total_quantity
        elif added_quantity > 0:
            position.entry_price = new_entry

        position.initial_quantity = total_quantity
        position.current_quantity = total_quantity
        position.atr = Decimal(str(signal['atr']))
        position.grade = signal.get('grade', position.grade)
        position.entry_order_id = entry_order.get('orderId')
        # highest_price/lowest_price 保留不重置；tp1_hit/tp2_hit 保持 False
        # trailing_stop_price/trailing_activated 保留（若已激活）；entry_time 保留最早入场时间

    async def _place_conditional_order_and_record(
        self,
        symbol: str,
        side: str,
        order_type: str,
        stop_price: Decimal,
        limit_price: Decimal,
        quantity: Decimal,
        order_kind: str,
        strategy_name: str,
    ) -> Optional[str]:
        """统一封装条件单下单+落库：统一账户 algoId、普通账户 orderId，失败返回 None（v6.28）"""
        try:
            order = await self.binance.place_conditional_order(
                symbol=symbol,
                side=side,
                stop_price=stop_price,
                price=limit_price,
                quantity=quantity,
                order_type=order_type,
                reduce_only=True
            )
            order_id = order.get('algoId') or order.get('orderId')
            logger.info(
                f"{symbol} 条件单已下单",
                order_type=order_kind,
                order_id=order_id,
                stop_price=float(stop_price),
                limit_price=float(limit_price),
                quantity=float(quantity)
            )
            if order_id and self.db_manager:
                record_kwargs = (
                    {'algo_id': order['algoId']} if order.get('algoId')
                    else {'order_id': order_id}
                )
                await record_condition_order(
                    self.db_manager, strategy_name, symbol,
                    order_type=order_kind, **record_kwargs
                )
            return order_id
        except Exception as e:
            logger.error(
                f"{symbol} 创建条件单失败",
                order_type=order_kind,
                error=str(e),
                exc_info=True
            )
            return None

    async def _place_tp_order(
        self,
        symbol: str,
        side: str,
        tp_price: Decimal,
        tp_limit: Decimal,
        tp_qty: Decimal,
        tp_label: Optional[str],
        strategy_name: str,
    ) -> Optional[str]:
        """统一挂 TP 止盈条件单（消除 TP1/TP2 重复下单逻辑，v6.28 重构）

        调用方传入已算好的触发价/限价/数量；本函数负责前置日志（可选）+ 下单。
        前置日志仅新开仓场景打印（tp_label 非 None）；加仓重建场景原无该日志，
        传 None 保持行为一致。

        Args:
            symbol: 交易对
            side: 条件单方向（SELL/BUY）
            tp_price: 止盈触发价
            tp_limit: 止盈限价（已做不利方向偏移）
            tp_qty: 止盈数量
            tp_label: 前置日志标签（"TP1"/"TP2"）；None 表示不打印
            strategy_name: 策略名称（孤儿单清理追踪用）

        Returns:
            条件单ID；失败返回 None
        """
        if tp_label is not None:
            logger.info(
                f"{symbol} 下{tp_label}止盈限价单",
                tp_side=side,
                tp_price=float(tp_price),
                limit_price=float(tp_limit),
                quantity=float(tp_qty)
            )
        return await self._place_conditional_order_and_record(
            symbol, side, "TAKE_PROFIT", tp_price, tp_limit,
            tp_qty, "TAKE_PROFIT", strategy_name
        )

    def _calculate_stop_price(self, position: PositionState) -> Decimal:
        """计算硬止损价：entry ∓ atr × stop_loss_atr_multiplier（按等级配置）

        Args:
            position: 持仓状态

        Returns:
            硬止损触发价（Decimal）
        """
        grade_risk = self._get_grade_risk(position.grade)
        stop_mult = Decimal(str(grade_risk.get('stop_loss_atr_multiplier', 1.5)))
        entry = position.entry_price or Decimal('0')
        atr = position.atr or Decimal('0')
        if position.direction == 'LONG':
            return entry - atr * stop_mult
        return entry + atr * stop_mult

    @staticmethod
    def _apply_limit_offset(price: Decimal, offset_pct: Decimal, direction: str) -> Decimal:
        """触发价向不利方向偏移得到限价（做多向下、做空向上），确保触发后成交

        Args:
            price: 触发价
            offset_pct: 偏移比例（如 0.002，从配置读取）
            direction: 持仓方向（LONG/SHORT）

        Returns:
            偏移后的限价
        """
        if direction == 'LONG':
            return price * (Decimal('1') - offset_pct)
        return price * (Decimal('1') + offset_pct)

    async def _get_precision_params(self, symbol: str) -> Tuple[str, Decimal]:
        """获取交易对精度参数（stepSize/tickSize），失败返回默认值

        Args:
            symbol: 交易对

        Returns:
            (step_size, tick_size) 元组
        """
        try:
            precision = await self._get_symbol_precision(symbol)
            return (
                str(precision.get('stepSize', '0.001')),
                Decimal(str(precision.get('tickSize', '0.01')))
            )
        except Exception:
            return '0.001', Decimal('0.01')

    async def _is_position_profitable(self, symbol: str, position: PositionState) -> bool:
        """判断持仓是否浮盈（可加仓，v6.28）

        LONG 需 current_price > entry_price；SHORT 需 current_price < entry_price。
        当前价获取失败或入场价无效时返回 True（放行，与 analyze 原行为一致）。

        Args:
            symbol: 交易对
            position: 持仓状态

        Returns:
            True = 浮盈（或无法判定）；False = 浮亏
        """
        if position.entry_price is None or position.entry_price <= 0:
            return True
        current_price = await self._get_current_price(symbol)
        if current_price is None:
            return True
        if position.direction == 'LONG':
            return current_price > position.entry_price
        return current_price < position.entry_price

    async def _cancel_orders_for_rebuild(self, symbol: str, position: PositionState) -> bool:
        """加仓重建前取消旧条件单（stop_loss/tp1/tp2/trailing，逐个取消）

        Args:
            symbol: 交易对
            position: 持仓状态

        Returns:
            True = 全部取消成功（或无需取消）；False = 存在失败（交由收敛机制重试）
        """
        order_refs = [
            ("stop_loss", position.stop_loss_order_id),
            ("tp1", position.tp1_order_id),
            ("tp2", position.tp2_order_id),
            ("trailing_stop", position.trailing_stop_order_id),
        ]
        ok = True
        for order_type, order_id in order_refs:
            if order_id is None:
                continue
            if not await self._cancel_single_order_with_retry(symbol, position, order_type, order_id, is_algo=True):
                ok = False
        return ok

    async def _load_order_rebuild_params(
        self, symbol: str, position: PositionState
    ) -> Tuple[Decimal, Decimal, str, Dict, Decimal, Decimal]:
        """读取加仓重建/补挂条件单所需精度与配置参数（v6.28 重构）

        Returns:
            (step_size, tick_size, stop_side, partial_cfg, stop_offset, tp_offset)
        """
        step_size, tick_size = await self._get_precision_params(symbol)
        stop_side = 'SELL' if position.direction == 'LONG' else 'BUY'
        partial_cfg = self._get_grade_risk(position.grade)['partial_take_profit']
        stop_offset = Decimal(str(self.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        tp_offset = Decimal(str(self.risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))
        return step_size, tick_size, stop_side, partial_cfg, stop_offset, tp_offset

    async def _rebuild_condition_orders(self, symbol: str, position: PositionState) -> bool:
        """按合并后总持仓重建 4 类条件单（硬止损/TP1/TP2/移动止损，v6.28）"""
        try:
            (step_size, tick_size, stop_side, partial_cfg,
             stop_offset, tp_offset) = await self._load_order_rebuild_params(symbol, position)
            initial_qty = position.initial_quantity

            # 1. 硬止损（全量）
            if not await self._rebuild_stop_loss(
                symbol, position, initial_qty, step_size, tick_size, stop_side, stop_offset
            ):
                return False

            # 2/3. TP1/TP2 止盈（level=1/2，统一走 _place_tp_order）
            for level, attr in ((1, 'tp1_order_id'), (2, 'tp2_order_id')):
                tp_price = self._calculate_tp_price(
                    position.entry_price, position.atr, position.direction, level, position.grade)
                tp_qty = self._adjust_quantity_precision(
                    initial_qty * Decimal(str(partial_cfg[f'tp{level}_close_ratio'])), step_size)
                tp_limit = self._adjust_price_precision(
                    self._apply_limit_offset(tp_price, tp_offset, position.direction), tick_size)
                tp_id = await self._place_tp_order(
                    symbol, stop_side, tp_price, tp_limit, tp_qty, None, self.strategy_name)
                if tp_id is None:
                    return False
                setattr(position, attr, tp_id)

            # 4. 移动止损（仅尾仓；未激活则跳过）
            if not await self._rebuild_trailing_stop(
                symbol, position, partial_cfg, initial_qty, step_size, tick_size, stop_side, stop_offset
            ):
                return False
            return True

        except Exception as e:
            logger.error(f"{symbol} 重建条件单异常", error=str(e), exc_info=True)
            return False

    async def _place_stop_loss_order(
        self, symbol: str, position: PositionState,
        stop_qty: Decimal, tick_size: Decimal,
        stop_side: str, stop_offset: Decimal,
    ) -> Optional[str]:
        """统一挂硬止损条件单（消除 _rebuild_stop_loss 与平尾仓补挂重复，v6.28 重构）

        触发价由 _calculate_stop_price 统一计算；失败返回 None（调用方决定终止/跳过）。
        """
        stop_price = self._calculate_stop_price(position)
        stop_limit = self._adjust_price_precision(
            self._apply_limit_offset(stop_price, stop_offset, position.direction), tick_size)
        return await self._place_conditional_order_and_record(
            symbol, stop_side, "STOP", stop_price, stop_limit,
            stop_qty, "STOP_LOSS", self.strategy_name
        )

    async def _rebuild_stop_loss(
        self, symbol: str, position: PositionState,
        initial_qty: Decimal, step_size: str, tick_size: Decimal,
        stop_side: str, stop_offset: Decimal,
    ) -> bool:
        """加仓重建：硬止损单（全量，v6.28 拆分子函数）

        Returns:
            True = 下单成功；False = 失败（阻断，标记 rebuild_pending）
        """
        stop_qty = self._adjust_quantity_precision(initial_qty, step_size)
        stop_id = await self._place_stop_loss_order(
            symbol, position, stop_qty, tick_size, stop_side, stop_offset)
        if stop_id is None:
            return False
        position.stop_loss_order_id = stop_id
        return True

    async def _rebuild_trailing_stop(
        self, symbol: str, position: PositionState, partial_cfg: Dict,
        initial_qty: Decimal, step_size: str, tick_size: Decimal,
        stop_side: str, stop_offset: Decimal,
    ) -> bool:
        """加仓重建：移动止损仅覆盖尾仓（v6.28 拆分子函数）

        未激活或尾仓精度调整后为 0 时跳过（logger.warning 说明）；失败返回 False。
        """
        if not (position.trailing_activated and position.trailing_stop_price is not None):
            return True
        remaining_ratio = Decimal(str(partial_cfg['remaining_ratio']))
        trail_qty = self._adjust_quantity_precision(initial_qty * remaining_ratio, step_size)
        if trail_qty <= 0:
            logger.warning(
                f"{symbol} 加仓重建：移动止损尾仓数量精度调整后为0，"
                f"跳过移动止损下单",
                initial_quantity=float(initial_qty),
                step_size=step_size
            )
            return True
        trail_limit = self._adjust_price_precision(
            self._apply_limit_offset(position.trailing_stop_price, stop_offset, position.direction), tick_size)
        trail_id = await self._place_conditional_order_and_record(
            symbol, stop_side, "STOP", position.trailing_stop_price, trail_limit,
            trail_qty, "STOP_LOSS", self.strategy_name
        )
        if trail_id is None:
            return False
        position.trailing_stop_order_id = trail_id
        return True

    async def _rebuild_remaining_protection(self, symbol: str, position: PositionState) -> None:
        """平尾仓后补挂剩余保护单：硬止损（全量剩余）+ TP2（initial×tp2_ratio，v6.28）

        _close_position 平仓前会取消交易所全部条件单（含 TP2/硬止损），
        平尾仓后剩余仓位需重新补挂保护，防止裸仓。

        Args:
            symbol: 交易对
            position: 持仓状态（current_quantity 为平尾仓后的剩余量）
        """
        if position.current_quantity <= 0 or position.tp2_hit:
            return

        (step_size, tick_size, stop_side, partial_cfg,
         stop_offset, tp_offset) = await self._load_order_rebuild_params(symbol, position)

        # 1. 补挂硬止损：数量=剩余持仓量，触发价=_calculate_stop_price
        stop_qty = self._adjust_quantity_precision(position.current_quantity, step_size)
        if stop_qty > 0:
            stop_id = await self._place_stop_loss_order(
                symbol, position, stop_qty, tick_size, stop_side, stop_offset)
            if stop_id:
                position.stop_loss_order_id = stop_id
        else:
            logger.warning(f"{symbol} 平尾仓补挂：剩余持仓量精度调整后为0，跳过硬止损补挂")

        # 2. 补挂 TP2：数量=initial×tp2_ratio，触发价=_calculate_tp_price(...,2,grade)
        tp2_price = self._calculate_tp_price(
            position.entry_price, position.atr, position.direction, 2, position.grade)
        tp2_qty = self._adjust_quantity_precision(
            position.initial_quantity * Decimal(str(partial_cfg['tp2_close_ratio'])), step_size
        )
        if tp2_qty > 0:
            tp2_limit = self._adjust_price_precision(
                self._apply_limit_offset(tp2_price, tp_offset, position.direction), tick_size)
            tp2_id = await self._place_tp_order(
                symbol, stop_side, tp2_price, tp2_limit, tp2_qty, None, self.strategy_name)
            if tp2_id:
                position.tp2_order_id = tp2_id
        else:
            logger.warning(f"{symbol} 平尾仓补挂：TP2尾仓数量精度调整后为0，跳过TP2补挂")

    async def _retry_rebuild_pending(self) -> None:
        """收敛 rebuild_pending 持仓（v6.28）

        加仓过程中取消旧单/重建条件单任一失败会标记 rebuild_pending=True，
        由 update_positions 周期末调用本方法：幂等取消旧单 + 按当前总持仓重建。
        成功则清除标记；失败保留标记，下个周期继续收敛。
        """
        for symbol, position in list(self.positions.items()):
            if not position.rebuild_pending:
                continue
            if position.current_quantity <= 0:
                position.rebuild_pending = False
                continue
            # 幂等取消旧条件单（已为 None 的自动跳过）
            if not await self._cancel_orders_for_rebuild(symbol, position):
                logger.warning(f"{symbol} 加仓重建收敛：取消旧条件单失败，下轮重试")
                continue
            # 按当前总持仓重建条件单
            if not await self._rebuild_condition_orders(symbol, position):
                logger.warning(f"{symbol} 加仓重建收敛：重建条件单失败，下轮重试")
                continue
            position.rebuild_pending = False
            logger.info(
                f"{symbol} 加仓重建收敛完成",
                quantity=float(position.current_quantity),
                grade=position.grade
            )
    
    async def _check_extreme_market(
        self, 
        symbol: str, 
        position: PositionState, 
        current_price: Decimal
    ) -> bool:
        """v6.16.10 极端行情处理：瞬间反向5% → 平仓50%，止损收紧至1.0×ATR"""
        config = self.risk_config.get('extreme_market', {})
        reverse_pct = Decimal(str(config.get('reverse_pct', 0.05)))

        if position.direction == 'LONG':
            loss_pct = (position.entry_price - current_price) / position.entry_price
        else:
            loss_pct = (current_price - position.entry_price) / position.entry_price

        if loss_pct >= reverse_pct:
            logger.warning(f"{symbol} 触发极端行情，反向{float(loss_pct)*100:.1f}%")
            # 平仓指定比例
            close_ratio = Decimal(str(config.get('close_ratio', 0.50)))
            close_qty = position.current_quantity * close_ratio
            await self._close_position(symbol, position, close_qty, "EXTREME")
            # 收紧止损至 1.0×ATR
            await self._tighten_extreme_stop(symbol, position, current_price)
            return True

        return False

    async def _tighten_extreme_stop(
        self, symbol: str, position: PositionState, current_price: Decimal,
    ) -> None:
        """极端行情收紧止损至 1.0×ATR（v6.28 拆分子函数）"""
        tighten_atr = Decimal(str(self.risk_config.get('extreme_market', {}).get('tighten_stop_atr', 1.0)))
        if position.direction == 'LONG':
            new_stop = current_price - position.atr * tighten_atr
        else:
            new_stop = current_price + position.atr * tighten_atr

        # 取消旧止损单，下新止损单
        if position.stop_loss_order_id:
            try:
                await self.binance.cancel_algo_order(symbol, position.stop_loss_order_id)
            except Exception as e:
                logger.warning(f"{symbol} 取消旧止损单失败: {e}")
        # 使用止损限价单（v6.21：从STOP_MARKET改为STOP）
        stop_offset_pct = Decimal(str(self.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        if position.direction == 'LONG':
            stop_limit_price = new_stop * (Decimal('1') - stop_offset_pct)
        else:
            stop_limit_price = new_stop * (Decimal('1') + stop_offset_pct)
        # 极端行情收紧止损单：保持直写 place_conditional_order，不复用 _place_conditional_order_and_record。
        # 原因：该封装会落库 condition_orders 且参数签名/返回值不同；此场景是极端行情临时收紧单，
        # 原逻辑不落库（由 _close_position 统一清理），复用会引入落库副作用，改变业务行为。
        new_stop_order = await self.binance.place_conditional_order(
            symbol, 'SELL' if position.direction == 'LONG' else 'BUY',
            new_stop, position.current_quantity, order_type='STOP', price=stop_limit_price,
            reduce_only=True
        )
        position.stop_loss_order_id = new_stop_order.get('algoId') or new_stop_order.get('orderId')
        logger.info(f"{symbol} 极端行情止损已收紧至{float(new_stop):.4f}")
    
    async def _check_liquidation_warning(
        self, 
        symbol: str, 
        position: PositionState
    ) -> bool:
        """
        v6.16.10 强平预警
        
        保证金率 ≤ 1.5 减仓50%，≤ 1.2 全部平仓
        """
        config = self.risk_config.get('liquidation_warning', {})
        
        try:
            # 获取账户信息中的持仓风险
            account_info = await self.binance.get_account_info()
            positions = account_info.get('positions', [])
            for p in positions:
                if p.get('symbol') == symbol:
                    margin_ratio = float(p.get('marginRatio', 999))
                    
                    if margin_ratio <= config.get('margin_ratio_close', 1.2):
                        logger.error(f"{symbol} 强平预警：保证金率{margin_ratio}，全部平仓")
                        await self._close_position(
                            symbol, position, position.current_quantity, "LIQUIDATION"
                        )
                        return True
                    
                    elif margin_ratio <= config.get('margin_ratio_reduce', 1.5):
                        logger.warning(f"{symbol} 强平预警：保证金率{margin_ratio}，减仓50%")
                        close_qty = position.current_quantity * Decimal(
                            str(config.get('reduce_ratio', 0.5))
                        )
                        await self._close_position(
                            symbol, position, close_qty, "LIQUIDATION_REDUCE"
                        )
                        return True
        except Exception as e:
            logger.error(f"{symbol} 强平检查失败: {e}")
        
        return False
    
    def _check_economic_calendar(self, current_time: datetime) -> Tuple[bool, str]:
        """
        检查经济日历事件（v6.16.10 新增）
        
        在重大经济事件（CPI、FOMC、非农就业 NFP 等）发布前后禁止交易，
        避免极端波动对策略造成不利影响。
        
        注意：配置中的事件时间使用 UTC 时间，与系统时间保持一致。
        
        Args:
            current_time: 当前时间（UTC）
        
        Returns:
            (是否可交易, 原因说明)
        """
        calendar_config = self.risk_config.get('economic_calendar', {})
        if not calendar_config.get('enabled', True):
            return True, "经济日历未启用"
        
        ban_window = calendar_config.get('ban_window_minutes', 60)
        events = calendar_config.get('events', [])
        
        if not events:
            return True, "无经济事件配置"
        
        current_date = current_time.date()
        
        for event in events:
            try:
                event_date_str = event.get('date', '')
                event_time_str = event.get('time', '')
                event_name = event.get('name', '未知事件')
                
                if not event_date_str or not event_time_str:
                    continue
                
                # 解析事件日期和时间（UTC）
                event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
                event_dt = datetime.strptime(
                    f"{event_date_str} {event_time_str}",
                    "%Y-%m-%d %H:%M"
                )
                
                # 计算禁止窗口（前后各 ban_window 分钟）
                window_start = event_dt - timedelta(minutes=ban_window)
                window_end = event_dt + timedelta(minutes=ban_window)
                
                # 跳过已过期且超过窗口的事件（优化性能）
                if event_date < current_date and window_end < current_time:
                    continue
                
                # 检查当前时间是否在禁止窗口内
                if window_start <= current_time <= window_end:
                    return False, (
                        f"经济事件禁止交易：{event_name} "
                        f"({event_date_str} {event_time_str} UTC)，"
                        f"禁止窗口 {window_start.strftime('%H:%M')} ~ {window_end.strftime('%H:%M')} UTC"
                    )
                
            except (ValueError, KeyError) as e:
                logger.warning(f"解析经济事件配置失败: {event}, 错误: {e}")
                continue
        
        return True, "不在经济事件禁止窗口内"
    
    async def update_positions(self):
        """
        更新持仓状态
        
        检查并执行：
        1. 利润提取提醒（v6.16.10）
        2. 强平预警
        3. 极端行情
        4. 分批止盈
        5. 动态利润保护
        6. 时间止损
        """
        try:
            for symbol, position in self.positions.items():
                if position.current_quantity <= 0:
                    continue

                # 获取当前价格
                current_price = await self._get_current_price(symbol)
                if current_price is None:
                    continue

                # 强平预警检查（v6.16.10：先救命再减伤）
                liq_triggered = await self._check_liquidation_warning(symbol, position)
                if liq_triggered:
                    continue  # 已全部平仓或减仓，跳过后续检查
                
                # 极端行情检查（v6.16.10）
                extreme_triggered = await self._check_extreme_market(symbol, position, current_price)
                if extreme_triggered:
                    continue  # 已处理极端行情，跳过后续检查
                
                # 更新最高/最低价
                if position.direction == 'LONG':
                    if position.highest_price is None or current_price > position.highest_price:
                        position.highest_price = current_price
                else:
                    if position.lowest_price is None or current_price < position.lowest_price:
                        position.lowest_price = current_price
                
                # 检查分批止盈
                await self._check_partial_take_profit(symbol, position, current_price)
                
                # 检查动态利润保护
                await self._check_dynamic_trailing(symbol, position, current_price)
                
                # 检查时间止损
                await self._check_time_stop(symbol, position)
                
        except Exception as e:
            logger.error(
                "更新持仓状态失败",
                error=str(e),
                exc_info=True
            )
        
        # 递增主循环计数（v6.23.1：用于条件单重试间隔控制）
        self._cycle_count += 1
        
        # 重试待取消的条件单（v6.23 孤儿条件单修复）
        await self._retry_pending_cancellations()
        
        # 清理已平仓持仓的残余条件单（第二层防护：兜底扫描）
        await self._cleanup_residual_orders()
        
        # 收敛加仓重建待处理持仓（v6.28：取消旧单/重建条件单失败的重试落点）
        await self._retry_rebuild_pending()
    
    async def _get_current_price(self, symbol: str) -> Optional[Decimal]:
        """
        获取当前价格
        
        Args:
            symbol: 交易对
        
        Returns:
            当前价格或None
        """
        try:
            ticker = await self.binance.get_ticker(symbol)
            return Decimal(str(ticker['lastPrice']))
        except Exception as e:
            logger.error(
                f"获取{symbol}当前价格失败",
                error=str(e)
            )
            return None
    
    async def _sync_position_with_exchange(self, symbol: str, position: PositionState, close_reason: str = "") -> Dict:
        """同步本地持仓状态与交易所实际持仓量

        Binance PM 账户对已平仓 symbol 返回空列表，不是 posAmt=0，
        所以必须把"API 返回空"也视为"交易所已无持仓"。

        Returns:
            {'closed': bool, 'partially_closed': bool, 'actual_quantity': Decimal | None}
        """
        try:
            exchange_positions = await self.binance.get_position(symbol)
            prev_quantity = float(position.current_quantity)
            matched_pos = next((p for p in exchange_positions if p.get('symbol') == symbol), None)

            if matched_pos is None:
                # Binance PM 对已平仓 symbol 返回空列表
                return self._mark_position_flat(
                    symbol, position, close_reason, prev_quantity, "交易所返回空持仓列表，视为已平仓")
            pos_amt = abs(float(matched_pos.get('positionAmt', 0)))
            if pos_amt < 0.0001:
                # 显式返回 posAmt=0 的边缘情况
                return self._mark_position_flat(
                    symbol, position, close_reason, prev_quantity, "交易所 posAmt≈0")
            if pos_amt < prev_quantity - 0.00001:
                # 持仓已被部分平仓（如止损/止盈条件单已成交部分）
                return self._update_synced_quantity(
                    symbol, position, close_reason, prev_quantity, pos_amt, True)
            if pos_amt > prev_quantity + 0.00001:
                # 持仓量增加（加仓成交），同步本地数量（v6.28 合并持仓统一托管）
                # 仅更新 current_quantity，保留 initial_quantity 供 _merge_position 计算加权均价
                return self._update_synced_quantity(
                    symbol, position, close_reason, prev_quantity, pos_amt, False)

            # 交易所持仓量 >= 本地记录，状态正常
            return {'closed': False, 'partially_closed': False, 'actual_quantity': Decimal(str(pos_amt))}
        except Exception as e:
            logger.warning(
                f"{symbol} 查询交易所持仓失败，继续使用本地持仓数据",
                close_reason=close_reason,
                error=str(e)
            )
            return {'closed': False, 'partially_closed': False, 'actual_quantity': None}

    @staticmethod
    def _mark_position_flat(
        symbol: str, position: PositionState,
        close_reason: str, prev_quantity: float, log_msg: str,
    ) -> Dict:
        """已平仓场景统一处理：置零持仓并返回 closed 标志（v6.28 拆分子函数）"""
        logger.info(
            f"{symbol} {log_msg}，同步本地",
            close_reason=close_reason,
            previous_quantity=prev_quantity
        )
        position.current_quantity = Decimal('0')
        position.direction = 'FLAT'
        return {'closed': True, 'partially_closed': False, 'actual_quantity': Decimal('0')}

    @staticmethod
    def _update_synced_quantity(
        symbol: str, position: PositionState, close_reason: str,
        prev_quantity: float, pos_amt: float, partially_closed: bool,
    ) -> Dict:
        """交易所数量变化同步：更新 current_quantity 并返回结构化结果（v6.28 拆分子函数）"""
        log_msg = "持仓已被部分平仓，同步本地数量" if partially_closed else "持仓量增加（加仓成交），同步本地数量"
        logger.info(
            f"{symbol} {log_msg}",
            close_reason=close_reason,
            previous_quantity=prev_quantity,
            actual_quantity=pos_amt
        )
        position.current_quantity = Decimal(str(pos_amt))
        return {
            'closed': False,
            'partially_closed': partially_closed,
            'actual_quantity': Decimal(str(pos_amt))
        }

    async def _cancel_symbol_conditional_orders(self, symbol: str, close_reason: str = "") -> bool:
        """
        取消指定交易对在交易所挂着的所有条件单（止盈/止损/动态追踪止盈等）。

        下单平仓前必须先清掉已占用仓位的条件单，否则会触发：
          [-2022] ReduceOnly Order is rejected （条件单已平仓，无持仓可平）
          [-4118] ReduceOnly Order Failed      （条件单占仓，新平仓单超卖）

        Returns:
            True = 成功取消或确认无挂单；False = 取消失败但不阻断主流程
        """
        try:
            await self.binance.cancel_all_algo_orders(symbol)
            logger.info(
                f"{symbol} 交易所条件单已全部取消",
                close_reason=close_reason
            )
            return True
        except BinanceAPIError as e:
            if e.code == -4046:
                # 没有挂着的条件单，正常情况
                logger.debug(f"{symbol} 无挂着的条件单，无需取消")
                return True
            logger.warning(
                f"{symbol} 取消交易所条件单失败（{e.code}），继续尝试平仓",
                close_reason=close_reason,
                error=str(e)
            )
            return False
        except Exception as e:
            logger.warning(
                f"{symbol} 取消交易所条件单异常，继续尝试平仓",
                close_reason=close_reason,
                error=str(e)
            )
            return False

    async def _close_position(
        self,
        symbol: str,
        position: PositionState,
        close_quantity: Decimal,
        close_reason: str,
        current_price: Optional[Decimal] = None
    ) -> bool:
        """
        执行平仓操作
        
        Args:
            symbol: 交易对
            position: 持仓状态
            close_quantity: 平仓数量（币的数量）
            close_reason: 平仓原因（TP1/TP2/TRAILING_STOP/TIME_STOP）
            current_price: 当前价格（可选，用于日志记录）
        
        Returns:
            是否平仓成功
        """
        try:
            # 确定平仓方向（与持仓方向相反）
            close_side = "SELL" if position.direction == "LONG" else "BUY"

            # 入口持仓同步：先查交易所实际持仓量（条件单可能已部分成交）
            sync_result = await self._sync_position_with_exchange(symbol, position, close_reason)
            if sync_result['closed']:
                # 交易所已无持仓，说明已被条件单平仓，视为平仓成功
                logger.info(
                    f"{symbol} 平仓前同步发现交易所已无持仓，跳过下单",
                    close_reason=close_reason
                )
                # 条件单已平仓，用当前价估算并回写盈亏（无 order_result，走模式二降级匹配）
                pnl = await self._write_close_pnl(
                    symbol, position, close_side, close_reason,
                    current_price=current_price, close_quantity=close_quantity
                )
                # 止损打标：本次平仓若为止损（非止盈）且由条件单促成，记录止损供看板统计
                await self._mark_stop_loss_if_needed(symbol, position, close_reason, pnl)
                return True

            # 下单前先取消交易所所有条件单（止盈/止损/动态追踪等）
            # 避免条件单占用仓位触发 [-4118] 或条件单已平仓触发 [-2022]
            await self._cancel_symbol_conditional_orders(symbol, close_reason)

            # 确保平仓数量不超过当前持仓数量
            actual_close_quantity = min(close_quantity, position.current_quantity)
            
            if actual_close_quantity <= 0:
                logger.warning(
                    f"{symbol} 平仓数量无效",
                    close_quantity=float(close_quantity),
                    current_quantity=float(position.current_quantity)
                )
                return False
            
            # 获取交易对精度信息
            precision_info = await self._get_symbol_precision(symbol)
            step_size = precision_info.get('stepSize', '0.001')
            
            # 调整平仓数量精度
            actual_close_quantity = self._adjust_quantity_precision(
                actual_close_quantity,
                step_size
            )
            tick_size = precision_info.get('tickSize', Decimal('0.01'))
            
            # 再次检查调整后的数量
            if actual_close_quantity <= 0:
                # 如果调整后数量归零，但持仓量充足，改为全部平仓
                if position.current_quantity > 0:
                    logger.warning(
                        f"{symbol} 精度调整后平仓数量为0，改为全部平仓",
                        original_quantity=float(close_quantity),
                        step_size=step_size
                    )
                    actual_close_quantity = position.current_quantity
                else:
                    logger.warning(
                        f"{symbol} 精度调整后平仓数量为0，且无持仓",
                        original_quantity=float(close_quantity),
                        step_size=step_size
                    )
                    return False
            
            # 获取当前价格（用于计算名义价值）
            if current_price is None:
                # 如果没有传入价格，获取最新价格
                ticker = await self.binance.get_ticker_price(symbol)
                current_price = Decimal(str(ticker))
            
            # 检查订单名义价值是否满足最小要求（20 USDT）
            notional_value = actual_close_quantity * current_price
            # 从配置读取平仓最小名义价值
            position_sizing_config = self.risk_config['position_sizing']
            min_notional = Decimal(str(position_sizing_config.get('min_close_notional_usdt', 20)))
            
            if notional_value < min_notional:
                logger.warning(
                    f"{symbol} 平仓名义价值不足，改为全部平仓",
                    close_reason=close_reason,
                    close_quantity=float(actual_close_quantity),
                    current_price=float(current_price),
                    notional_value=float(notional_value),
                    min_notional=float(min_notional),
                    remaining_quantity=float(position.current_quantity)
                )
                # 改为全部平仓
                actual_close_quantity = position.current_quantity
                # 重新调整精度
                actual_close_quantity = self._adjust_quantity_precision(
                    actual_close_quantity,
                    step_size
                )
                # 再次检查
                if actual_close_quantity <= 0:
                    logger.warning(
                        f"{symbol} 全部平仓数量调整后为0，跳过",
                        position_quantity=float(position.current_quantity)
                    )
                    return False
            
            logger.info(
                f"{symbol} 开始执行平仓",
                close_reason=close_reason,
                close_side=close_side,
                close_quantity=float(actual_close_quantity),
                current_quantity=float(position.current_quantity),
                current_price=float(current_price) if current_price else None
            )
            
            # 所有平仓统一使用限价单（v6.21：移除市价单降级逻辑）
            close_limit_config = self.risk_config.get('close_limit_order', {})
            max_retries = close_limit_config.get('max_retries', 3)
            retry_interval = close_limit_config.get('retry_interval_seconds', 2)
            poll_interval = close_limit_config.get('poll_interval_seconds', 2)
            timeout = close_limit_config.get('timeout_seconds', 10)

            filled = False
            order_result = None
            last_error = None

            for retry_attempt in range(max_retries + 1):
                try:
                    # 获取订单簿最优价
                    orderbook = await self.binance.get_orderbook(symbol, limit=5)
                    if position.direction == 'LONG':
                        limit_price = Decimal(str(orderbook['bids'][0][0]))
                    else:
                        limit_price = Decimal(str(orderbook['asks'][0][0]))

                    # 调整价格精度
                    limit_price = self._adjust_price_precision(limit_price, tick_size)

                    logger.info(
                        f"{symbol} 限价单平仓（第{retry_attempt + 1}次）",
                        close_reason=close_reason,
                        limit_price=float(limit_price),
                        direction=position.direction
                    )

                    # 下限价单
                    order_result = await self.binance.place_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=actual_close_quantity,
                        order_type="LIMIT",
                        price=limit_price,
                        reduce_only=True
                    )

                    # 轮询等待成交
                    elapsed = 0
                    while elapsed < timeout:
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval

                        open_orders = await self.binance.get_open_orders(symbol)
                        order_still_open = any(
                            str(o.get('orderId')) == str(order_result['orderId'])
                            for o in open_orders
                        )

                        if not order_still_open:
                            logger.info(
                                f"{symbol} 限价平仓已成交",
                                close_reason=close_reason,
                                order_id=order_result.get('orderId'),
                                elapsed_seconds=elapsed,
                                retry_attempt=retry_attempt
                            )
                            filled = True
                            break

                    if filled:
                        break

                    # 超时未成交，撤销后重试（用新最优价）
                    try:
                        await self.binance.cancel_order(symbol, order_id=str(order_result['orderId']))
                        logger.info(
                            f"{symbol} 限价平仓超时，撤销后重试（第{retry_attempt + 1}/{max_retries}次）",
                            close_reason=close_reason,
                            limit_price=float(limit_price),
                            elapsed_seconds=elapsed
                        )
                    except BinanceAPIError as cancel_error:
                        if cancel_error.code == -2011:
                            # 订单在轮询与取消之间成交
                            logger.info(
                                f"{symbol} 限价平仓单已成交（取消时确认）",
                                close_reason=close_reason,
                                order_id=order_result.get('orderId'),
                                elapsed_seconds=elapsed
                            )
                            filled = True
                            break
                        else:
                            raise

                    if retry_attempt < max_retries:
                        await asyncio.sleep(retry_interval)

                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"{symbol} 限价平仓异常（第{retry_attempt + 1}次）",
                        error=str(e),
                        close_reason=close_reason
                    )
                    
                    # ReduceOnly 被拒：先清掉挂着的条件单，再同步持仓量
                    # [-2022] 条件单已平仓，无持仓可平
                    # [-4118] 条件单占仓，新平仓单超卖
                    if isinstance(e, BinanceAPIError) and e.code in (-2022, -4118):
                        error_code = e.code
                        logger.warning(
                            f"{symbol} ReduceOnly 被拒（{error_code}），先取消条件单再同步持仓",
                            close_reason=close_reason
                        )
                        # 第一步：清掉所有条件单，释放被占用的仓位
                        await self._cancel_symbol_conditional_orders(symbol, close_reason)
                        # 第二步：同步交易所实际持仓量
                        sync_result = await self._sync_position_with_exchange(symbol, position, close_reason)
                        if sync_result['closed']:
                            # 交易所已无持仓（条件单已平仓），视为成功
                            # 直接返回 True，避免 order_result 为空导致末尾空引用
                            logger.info(
                                f"{symbol} ReduceOnly被拒 → 交易所已无持仓，平仓成功",
                                close_reason=close_reason,
                                error_code=error_code
                            )
                            # 条件单已平仓，用当前价估算并回写盈亏（无 order_result，走模式二降级匹配）
                            pnl = await self._write_close_pnl(
                                symbol, position, close_side, close_reason,
                                current_price=current_price, close_quantity=close_quantity
                            )
                            # 止损打标：与入口 closed 分支保持一致
                            await self._mark_stop_loss_if_needed(symbol, position, close_reason, pnl)
                            return True
                        elif sync_result['partially_closed']:
                            # 交易所持仓量 < 本地记录，调整平仓量后继续重试
                            actual_qty = sync_result['actual_quantity']
                            actual_close_quantity = self._adjust_quantity_precision(
                                min(actual_close_quantity, actual_qty),
                                step_size
                            )
                            if actual_close_quantity <= 0:
                                logger.info(
                                    f"{symbol} 调整后平仓量为0，跳过本次循环",
                                    close_reason=close_reason
                                )
                                break
                            logger.info(
                                f"{symbol} ReduceOnly被拒 → 缩小平仓量后重试",
                                close_reason=close_reason,
                                adjusted_close_quantity=float(actual_close_quantity),
                                exchange_quantity=float(actual_qty),
                                error_code=error_code
                            )
                            # 继续重试，不 break
                        else:
                            # 同步成功但持仓量未变，无法继续重试，跳出循环
                            logger.info(
                                f"{symbol} ReduceOnly被拒 → 同步后持仓量未变，无法继续重试",
                                close_reason=close_reason,
                                exchange_quantity=str(sync_result.get('actual_quantity')),
                                error_code=error_code
                            )
                            break
                    
                    if retry_attempt < max_retries and not filled:
                        await asyncio.sleep(retry_interval)

            if not filled:
                # 所有重试均失败，保留限价单不成交，下个周期再尝试
                logger.warning(
                    f"{symbol} 限价平仓所有重试均未成交，保留仓位等待下次循环",
                    close_reason=close_reason,
                    last_error=str(last_error) if last_error else "超时未成交"
                )
                return False
            
            # 记录平仓成功日志
            logger.info(
                f"{symbol} 平仓成功",
                close_reason=close_reason,
                order_id=order_result.get('orderId'),
                close_quantity=float(actual_close_quantity),
                close_price=float(order_result.get('avgPrice', 0)),
                remaining_quantity=float(position.current_quantity - actual_close_quantity)
            )
            
            # 更新持仓数量
            position.current_quantity -= actual_close_quantity
            
            # 计算平仓盈亏并回写 trade_records.realized_pnl
            # 注意：回写失败不影响平仓主流程，异常被内部捕获仅记日志
            try:
                # 从 order_result 获取成交均价，可能为 "0"（限价单刚成交时 API 不返回）
                exit_price = Decimal(str(order_result.get('avgPrice', '0')))
                if exit_price <= 0:
                    exit_price = current_price or Decimal('0')

                if exit_price > 0 and position.entry_price and position.entry_price > 0:
                    # 使用集中管理的公式计算平仓盈亏
                    pnl = TradeLogger.calculate_pnl(
                        direction=position.direction,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        quantity=actual_close_quantity
                    )

                    # 通过 getattr 获取 trade_logger 实例，避免直接依赖
                    trade_logger = getattr(self.binance, 'trade_logger', None)
                    if trade_logger:
                        await trade_logger.update_realized_pnl(
                            order_id=str(order_result.get('orderId', '')),
                            realized_pnl=pnl,
                            side=close_side,
                            symbol=symbol,
                            executed_at=datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
                        )
                        # 止损打标：平仓成交后，若本次为止损（非止盈）则记录止损供看板统计
                        await self._mark_stop_loss_if_needed(symbol, position, close_reason, pnl)
            except Exception as pnl_error:
                logger.warning(
                    f"{symbol} 回写平仓盈亏失败，不影响平仓流程",
                    error=str(pnl_error),
                    exc_info=True
                )
            
            # 发送平仓通知
            try:
                # 计算通知价格：avgPrice键值可能为"0"（限价单刚成交时API不返回成交价）
                # 此时回退到current_price，避免"价格必须大于0: 0.0"错误
                avg_price_raw = order_result.get('avgPrice', '0')
                try:
                    avg_price_val = float(avg_price_raw) if avg_price_raw else 0.0
                except (ValueError, TypeError):
                    avg_price_val = 0.0

                if avg_price_val <= 0:
                    notify_price = float(current_price) if current_price else 0.0
                else:
                    notify_price = avg_price_val

                if notify_price > 0:
                    await self.notification.send_trade_notification(
                        strategy=self.strategy_name,
                        symbol=symbol,
                        action=f"CLOSE_{close_reason}",
                        quantity=float(actual_close_quantity),
                        price=notify_price,
                        remaining_quantity=float(position.current_quantity)
                    )
            except Exception as notify_error:
                logger.error(
                    f"{symbol} 发送平仓通知失败",
                    error=str(notify_error)
                )
            
            # 首次完全平仓后异步取消残余条件单（第一层防护）
            if position.current_quantity <= 0:
                position.cancel_pending = True
                asyncio.ensure_future(self._cleanup_position_orders(symbol, position))
                logger.info(
                    f"{symbol} 已触发异步条件单清理",
                    cancel_pending=position.cancel_pending
                )
            
            return True
            
        except Exception as e:
            logger.error(
                f"{symbol} 平仓失败",
                close_reason=close_reason,
                close_quantity=float(close_quantity),
                error=str(e),
                exc_info=True
            )
            
            # 发送错误通知
            try:
                await self.notification.send_error_notification(
                    strategy=self.strategy_name,
                    error_message=f"平仓失败: {close_reason} - {str(e)}",
                    symbol=symbol
                )
            except Exception as notify_error:
                logger.error(
                    f"{symbol} 发送错误通知失败",
                    error=str(notify_error)
                )
            
            return False

    async def _write_close_pnl(
        self,
        symbol: str,
        position: PositionState,
        close_side: str,
        close_reason: str,
        current_price: Optional[Decimal] = None,
        close_quantity: Optional[Decimal] = None,
    ) -> Optional[Decimal]:
        """条件单已平仓场景的盈亏回写（order_result 为 None，用当前价估算）。"""
        try:
            pnl_direction = "LONG" if close_side == "SELL" else "SHORT"
            # closed 时 current_quantity 已置 0，数量必须来自 close_quantity 参数；无效则跳过回写
            quantity = close_quantity if close_quantity is not None else Decimal('0')
            exit_price = current_price or Decimal('0')
            if exit_price <= 0 or not position.entry_price or position.entry_price <= 0 or quantity <= 0:
                logger.debug(f"{symbol} 条件单平仓参数无效，跳过盈亏回写", close_reason=close_reason)
                return None
            pnl = TradeLogger.calculate_pnl(
                direction=pnl_direction,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=quantity,
            )
            trade_logger = getattr(self.binance, 'trade_logger', None)
            if trade_logger is None:
                return None
            executed_at = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
            # order_id 传空串，走模式二降级匹配（strategy+symbol+side+±300秒 且 realized_pnl IS NULL）
            updated = await trade_logger.update_realized_pnl(
                order_id='',
                realized_pnl=pnl,
                side=close_side,
                symbol=symbol,
                executed_at=executed_at,
            )
            if not updated and close_reason in _TAKE_PROFIT_CLOSE_REASONS:
                # 止盈类无 trade_records 可 UPDATE 时插入 PnL 汇总兜底；止损类由内部 log_stop_loss 兜底
                await trade_logger.insert_pnl_summary(
                    realized_pnl=pnl,
                    symbol=symbol,
                    side=close_side,
                    strategy=trade_logger.strategy_name,
                    executed_at=executed_at,
                    close_reason=close_reason,
                )
            logger.info(f"{symbol} 条件单平仓盈亏回写完成", close_reason=close_reason, realized_pnl=str(pnl))
            return pnl
        except Exception as e:
            logger.warning(f"{symbol} 条件单平仓盈亏回写异常，不影响平仓流程", close_reason=close_reason, error=str(e), exc_info=True)
            return None

    async def _mark_stop_loss_if_needed(self, symbol: str, position: PositionState,
                                        close_reason: str,
                                        realized_pnl: Optional[Decimal] = None) -> None:
        """止损打标（供风控看板"最近止损次数"统计）。

        仅非止盈平仓（时间止损 / 条件单止损 / 强平预警 / 极端行情等保护性平仓）才打
        STOP_LOSS 标记；止盈（TP1/TP2/移动止盈）不打标记，避免误伤风控统计。
        通过 getattr 探测 trade_logger，缺失或调用异常时静默跳过，不影响平仓主流程。
        """
        if close_reason in _TAKE_PROFIT_CLOSE_REASONS:
            return
        trade_logger = getattr(self.binance, 'trade_logger', None)
        if trade_logger is None or not hasattr(trade_logger, 'log_stop_loss'):
            return
        try:
            await trade_logger.log_stop_loss(
                symbol=symbol,
                side="SELL" if position.direction == "LONG" else "BUY",
                realized_pnl=realized_pnl,
            )
        except Exception as e:
            logger.warning(
                f"{symbol} 打标止损记录失败",
                close_reason=close_reason,
                error=str(e)
            )
    
    async def _check_partial_take_profit(
        self,
        symbol: str,
        position: PositionState,
        current_price: Decimal
    ):
        """
        检查并执行分批止盈
        
        Args:
            symbol: 交易对
            position: 持仓状态
            current_price: 当前价格
        """
        grade_risk = self._get_grade_risk(position.grade)
        partial_config = grade_risk['partial_take_profit']
        tp1_ratio = Decimal(str(partial_config['tp1_close_ratio']))
        remaining_ratio = Decimal(str(partial_config['remaining_ratio']))

        # 先同步交易所仓位，检测交易所条件单是否已部分平仓（防内存重复平仓）
        synced = await self._sync_position_with_exchange(symbol, position, close_reason="PARTIAL_TP")
        if synced.get('closed'):
            position.tp1_hit = True
            position.tp2_hit = True
            return
        # 推断已触发的 TP 级别：用 current_quantity 相对 initial_quantity 的缺口
        if position.initial_quantity and position.initial_quantity > 0:
            remaining_after_tp2 = position.initial_quantity * remaining_ratio
            remaining_after_tp1 = position.initial_quantity * (Decimal('1') - tp1_ratio)
            # 容差：避免浮点/精度误差（步长级误差），比例从配置读取
            eps_ratio = Decimal(str(self.risk_config.get('quantity_inference_eps_ratio', 0.005)))
            eps = position.initial_quantity * eps_ratio
            if position.current_quantity <= remaining_after_tp2 + eps:
                position.tp1_hit = True
                position.tp2_hit = True
            elif position.current_quantity <= remaining_after_tp1 + eps:
                position.tp1_hit = True
        
        # 检查TP1
        if not position.tp1_hit:
            tp1_price = self._calculate_tp_price(
                position.entry_price,
                position.atr,
                position.direction,
                1,
                position.grade
            )
            
            hit = (
                (position.direction == 'LONG' and current_price >= tp1_price) or
                (position.direction == 'SHORT' and current_price <= tp1_price)
            )
            
            if hit:
                # 平仓30%（TP1固定止盈比例，从配置读取）
                close_ratio = Decimal(str(partial_config['tp1_close_ratio']))
                close_quantity = position.initial_quantity * close_ratio
                
                logger.info(
                    f"{symbol} 触发TP1止盈",
                    tp1_price=float(tp1_price),
                    close_quantity=float(close_quantity)
                )
                
                # 执行平仓
                success = await self._close_position(
                    symbol=symbol,
                    position=position,
                    close_quantity=close_quantity,
                    close_reason="TP1",
                    current_price=current_price
                )
                
                if success:
                    position.tp1_hit = True
                    # 激活动态利润保护
                    if not position.trailing_activated:
                        position.trailing_activated = True
                        logger.info(f"{symbol} 动态利润保护已激活（TP1触发）")
                    # TP1全部平仓后，跳过后续检查，避免TP2/动态利润保护/时间止损误报
                    if position.current_quantity <= 0:
                        return
                else:
                    logger.error(f"{symbol} TP1平仓失败，保持持仓状态")
        
        # 检查TP2（仓位可能已被TP1全部清空）
        if position.tp1_hit and not position.tp2_hit and position.current_quantity > 0:
            tp2_price = self._calculate_tp_price(
                position.entry_price,
                position.atr,
                position.direction,
                2,
                position.grade
            )
            
            hit = (
                (position.direction == 'LONG' and current_price >= tp2_price) or
                (position.direction == 'SHORT' and current_price <= tp2_price)
            )
            
            if hit:
                # 平仓40%（TP2固定止盈比例，从配置读取）
                close_ratio = Decimal(str(partial_config['tp2_close_ratio']))
                close_quantity = position.initial_quantity * close_ratio
                
                logger.info(
                    f"{symbol} 触发TP2止盈",
                    tp2_price=float(tp2_price),
                    close_quantity=float(close_quantity)
                )
                
                # 执行平仓
                success = await self._close_position(
                    symbol=symbol,
                    position=position,
                    close_quantity=close_quantity,
                    close_reason="TP2",
                    current_price=current_price
                )
                
                if success:
                    position.tp2_hit = True
                else:
                    logger.error(f"{symbol} TP2平仓失败，保持持仓状态")
    
    async def _cancel_trailing_order(self, symbol: str, position: PositionState) -> None:
        """平仓前取消交易所上的移动止损条件单（v6.28 拆分 _check_dynamic_trailing）"""
        if position.trailing_stop_order_id is None:
            return
        try:
            await self.binance.cancel_algo_order(symbol, position.trailing_stop_order_id)
        except BinanceAPIError as e:
            if e.code not in self.risk_config['cleanup_silent_error_codes']:
                logger.warning(
                    f"{symbol} 取消移动止损条件单失败",
                    algo_id=position.trailing_stop_order_id,
                    error_code=e.code
                )
        except Exception as e:
            logger.warning(
                f"{symbol} 取消移动止损条件单异常",
                algo_id=position.trailing_stop_order_id,
                error=str(e)
            )
        position.trailing_stop_order_id = None

    async def _handle_trailing_trigger(
        self,
        symbol: str,
        position: PositionState,
        current_price: Decimal,
        trailing_stop: Decimal,
    ) -> None:
        """峰值回落触发保护：取消移动止损单 + 平尾仓 + 补挂剩余保护（v6.28 拆分）"""
        await self._cancel_trailing_order(symbol, position)

        # 平仓量 = initial_quantity × remaining_ratio（尾仓，v6.28）
        # 修复：原逻辑用 current_quantity 全平剩余，TP1 触发后价格回落即全平跳过 TP2
        partial_cfg = self._get_grade_risk(position.grade)['partial_take_profit']
        remaining_ratio = Decimal(str(partial_cfg['remaining_ratio']))
        step_size, _tick_size = await self._get_precision_params(symbol)
        close_quantity = self._adjust_quantity_precision(
            position.initial_quantity * remaining_ratio, step_size
        )
        if close_quantity <= 0:
            # 精度调整后为 0：转全平剩余（兜底）
            close_quantity = position.current_quantity
        else:
            # 限幅：不超过当前剩余持仓，防止超卖
            close_quantity = min(close_quantity, position.current_quantity)

        logger.info(
            f"{symbol} 触发动态利润保护止损",
            current_price=float(current_price),
            trailing_stop=float(trailing_stop),
            unrealized_pnl_pct=position.pending_profit_pct,
            close_quantity=float(close_quantity)
        )

        await self._close_position(
            symbol=symbol,
            position=position,
            close_quantity=close_quantity,
            close_reason="TRAILING_STOP",
            current_price=current_price
        )

        # 平尾仓后补挂剩余保护单（硬止损 + TP2），防止剩余仓位裸仓
        await self._rebuild_remaining_protection(symbol, position)

    async def _check_dynamic_trailing(
        self,
        symbol: str,
        position: PositionState,
        current_price: Decimal
    ):
        """检查并执行动态利润保护：计算动态止损价，突破即平仓，改善则同步交易所条件单"""
        if position.current_quantity <= 0:
            return

        # 保存旧止损价，用于判断是否改善
        old_trailing_stop = position.trailing_stop_price

        trailing_stop = await self._calculate_dynamic_trailing_stop(
            symbol, position, current_price
        )

        if trailing_stop is None:
            return

        # 情况1：当前价已突破动态止损价 → 直接平仓（峰值回落保护）
        # 场景：价格从峰值大幅回落，已低于基于峰值计算的止损价
        triggered = False
        if position.direction == 'LONG' and current_price <= trailing_stop:
            triggered = True
        elif position.direction == 'SHORT' and current_price >= trailing_stop:
            triggered = True

        if triggered:
            await self._handle_trailing_trigger(
                symbol, position, current_price, trailing_stop)
            return

        # 情况2：止损价未改善，无需更新交易所条件单
        # _calculate_dynamic_trailing_stop 内部已处理单向移动保护
        if old_trailing_stop is not None and trailing_stop == old_trailing_stop:
            return

        # 情况3：止损价改善（首次激活或价格向有利方向移动）
        # → 同步到交易所条件单，让交易所自动触发止损
        await self._sync_trailing_stop_order(symbol, position, trailing_stop)

    async def _calculate_dynamic_trailing_stop(
        self,
        symbol: str,
        position: PositionState,
        current_price: Decimal
    ) -> Optional[Decimal]:
        """
        计算动态利润保护止损价
        
        核心逻辑：
        1. 检查是否激活（浮盈>1.5% 或 TP1触发）
        2. 基于最高/最低价计算浮盈百分比
        3. 根据回撤阶梯确定允许回撤比例
        4. 计算波动率调节因子
        5. 基于最高/最低价计算动态止损价
        6. 与硬止损取MAX/MIN得到最终止损价
        
        Args:
            symbol: 交易对
            position: 持仓状态
            current_price: 当前价格（仅用于触发检查，不用于计算）
        
        Returns:
            Decimal: 最终止损价（如果激活），None（未激活时）
        """
        # 从信号等级的风险配置中获取动态止损参数（v6.16.10: 按等级独立配置）
        # 修复：之前错误地从 risk.dynamic_trailing 读取，实际配置在 risk.signal_levels.{grade}.dynamic_trailing
        grade = getattr(position, 'grade', 'A')
        grade_risk = self._get_grade_risk(grade)
        dt_config = grade_risk.get('dynamic_trailing', {})
        if not dt_config.get('enabled', True):
            return None
        
        activation_config = dt_config.get('activation', {})
        tiers = dt_config.get('regression_tiers', [])
        
        # 基于最高/最低价计算浮盈百分比（而非当前价）
        # 设计依据：用峰值计算允许回撤，才能在价格回落时锁住利润
        if position.direction == 'LONG':
            if position.entry_price is None or position.entry_price <= 0:
                return None
            # 取最高价作为参考价（若无历史最高价，回退到当前价）
            reference_price = (
                position.highest_price
                if position.highest_price and position.highest_price > position.entry_price
                else current_price
            )
            unrealized_pnl_pct = float((reference_price - position.entry_price) / position.entry_price) * 100
            if unrealized_pnl_pct < 0:
                unrealized_pnl_pct = 0.0  # 浮亏不计入
        else:
            if position.entry_price is None or position.entry_price <= 0:
                return None
            # 取最低价作为参考价（若无历史最低价，回退到当前价）
            reference_price = (
                position.lowest_price
                if position.lowest_price and position.lowest_price > 0
                and position.lowest_price < position.entry_price
                else current_price
            )
            unrealized_pnl_pct = float((position.entry_price - reference_price) / position.entry_price) * 100
            if unrealized_pnl_pct < 0:
                unrealized_pnl_pct = 0.0  # 浮亏不计入
        
        position.pending_profit_pct = unrealized_pnl_pct
        
        # 激活判断：浮盈 >= min_profit_pct 或 TP1已触发
        min_profit = activation_config.get('min_profit_pct', 1.5)
        profit_activated = unrealized_pnl_pct >= min_profit
        tp1_activated = activation_config.get('also_on_tp1', True) and position.tp1_hit
        
        if not profit_activated and not tp1_activated:
            # 如果已激活但浮盈回落，保持激活状态不退出
            if position.trailing_activated:
                pass
            else:
                return None
        
        # 标记已激活
        if not position.trailing_activated:
            logger.info(
                f"{symbol} 动态利润保护激活",
                profit_pct=round(unrealized_pnl_pct, 2),
                activated_by="TP1" if tp1_activated else "profit"
            )
        position.trailing_activated = True
        
        # 检查 tiers 配置是否为空
        if not tiers:
            logger.warning(f"{symbol} 动态利润保护配置错误：regression_tiers 为空")
            return None

        # 保本模式：浮盈 < 1.5% 且 TP1未触发
        first_tier_ceiling = float(tiers[0]['profit_ceiling'])
        if unrealized_pnl_pct < first_tier_ceiling and not position.tp1_hit:
            stop_price = position.entry_price
            position.current_tier_index = 0
        else:
            # 确定回撤阶梯
            tier_index = -1
            for i, tier in enumerate(tiers):
                if unrealized_pnl_pct < float(tier['profit_ceiling']):
                    tier_index = i
                    break
            if tier_index == -1:
                tier_index = len(tiers) - 1
            
            position.current_tier_index = tier_index
            retrace_ratio = float(tiers[tier_index]['retrace_ratio'])
            
            # 计算波动率调节因子
            vol_adj = await self._get_volatility_adjustment(symbol, position)
            
            # 计算允许回撤（基于参考价，而非当前价）
            if position.direction == 'LONG':
                profit_per_unit = reference_price - position.entry_price
                allowed_retrace = profit_per_unit * Decimal(str(retrace_ratio)) * Decimal(str(vol_adj))
                stop_price = reference_price - allowed_retrace
            else:
                profit_per_unit = position.entry_price - reference_price
                allowed_retrace = profit_per_unit * Decimal(str(retrace_ratio)) * Decimal(str(vol_adj))
                stop_price = reference_price + allowed_retrace
        
        # 计算硬止损价（兜底）
        hard_stop_mult = Decimal(str(grade_risk.get('stop_loss_atr_multiplier', 1.5)))
        if position.direction == 'LONG':
            hard_stop_price = position.entry_price - position.atr * hard_stop_mult
        else:
            hard_stop_price = position.entry_price + position.atr * hard_stop_mult
        
        # 最终止损价：做多取MAX，做空取MIN
        if position.direction == 'LONG':
            final_stop = max(stop_price, hard_stop_price)
        else:
            final_stop = min(stop_price, hard_stop_price)
        
        # 单向移动保护：做多只能上移，做空只能下移
        if position.trailing_stop_price is not None:
            if position.direction == 'LONG' and final_stop <= position.trailing_stop_price:
                final_stop = position.trailing_stop_price
            elif position.direction == 'SHORT' and final_stop >= position.trailing_stop_price:
                final_stop = position.trailing_stop_price
        
        position.trailing_stop_price = final_stop
        
        return final_stop
    
    async def _get_volatility_adjustment(
        self,
        symbol: str,
        position: PositionState
    ) -> float:
        """
        计算波动率调节因子
        
        基于历史日线ATR中位数，衡量当前币种的相对波动水平。
        波动率越高，调节因子越大，允许回撤比例越高。
        
        公式：
            当前ATR% = 当前ATR / 当前价格
            基准ATR%中位数 = 历史30日日线ATR%中位数
            波动率调节因子 = 当前ATR% / 基准ATR%历史中位数
        
        Returns:
            float: 波动率调节因子（clamp到 [0.5, 2.0]）
        """
        import time
        
        vol_config = self.risk_config.get('dynamic_trailing', {}).get('volatility_adjustment', {})
        if not vol_config.get('enabled', True):
            return 1.0
        
        # 检查缓存
        cache_key = f"base_atr_pct_{symbol}"
        if not hasattr(self, '_base_atr_cache'):
            self._base_atr_cache = {}
        
        cached = self._base_atr_cache.get(cache_key)
        cache_ttl = vol_config.get('cache_ttl_seconds', 3600)
        if cached and (time.time() - cached['time'] < cache_ttl):
            return cached['value']
        
        try:
            lookback_days = vol_config.get('atr_lookback_days', 30)
            atr_period = vol_config.get('atr_period', 14)
            
            # 获取历史日线数据
            klines = await self.kline_service.get_klines(symbol, '1d', limit=lookback_days + atr_period + 10)
            if klines is None or len(klines) < lookback_days + atr_period:
                logger.warning(f"{symbol} 历史日线数据不足，使用默认波动率调节因子 1.0")
                return 1.0
            
            # 计算日线ATR和ATR%
            import pandas as pd
            df = pd.DataFrame(klines)
            # 确保字段名正确
            close_col = 'close' if 'close' in df.columns else 'close_price'
            # 将Decimal转为float
            if close_col in df.columns:
                df['close'] = pd.to_numeric(df[close_col], errors='coerce')
            if 'high' in df.columns:
                df['high'] = pd.to_numeric(df['high'], errors='coerce')
            elif 'high_price' in df.columns:
                df['high'] = pd.to_numeric(df['high_price'], errors='coerce')
            if 'low' in df.columns:
                df['low'] = pd.to_numeric(df['low'], errors='coerce')
            elif 'low_price' in df.columns:
                df['low'] = pd.to_numeric(df['low_price'], errors='coerce')
            
            from shared.indicators import TechnicalIndicators
            atr_series = TechnicalIndicators.calculate_atr(df, period=atr_period)
            atr_pct_series = atr_series / df['close']
            base_atr_pct = float(atr_pct_series.median())
            
            # 当前ATR%
            current_price = position.entry_price if position.entry_price and position.entry_price > 0 else Decimal('1')
            current_atr_pct = float(position.atr / current_price)
            
            # 计算波动率调节因子
            vol_adj = current_atr_pct / base_atr_pct if base_atr_pct > 0 else 1.0
            vol_adj = max(0.5, min(2.0, vol_adj))
            
            # 缓存
            self._base_atr_cache[cache_key] = {
                'value': vol_adj,
                'time': time.time(),
                'base_atr_pct': base_atr_pct,
                'current_atr_pct': current_atr_pct
            }
            
            logger.debug(
                f"{symbol} 波动率调节因子",
                base_atr_pct=round(base_atr_pct, 6),
                current_atr_pct=round(current_atr_pct, 6),
                vol_adj=round(vol_adj, 4)
            )
            
            return vol_adj
        
        except Exception as e:
            logger.error(
                f"{symbol} 计算波动率调节因子失败",
                error=str(e),
                exc_info=True
            )
            return 1.0
    
    async def _sync_trailing_stop_order(
        self,
        symbol: str,
        position: PositionState,
        trailing_stop: Decimal
    ):
        """
        将动态止损价同步到交易所条件单
        
        取消旧条件单，创建新条件单，让交易所自动触发止损。
        首次激活只创建移动止损条件单，硬止损全仓单保留不动。
        
        Args:
            symbol: 交易对
            position: 持仓状态
            trailing_stop: 计算出的动态止损价
        """
        stop_side = 'SELL' if position.direction == 'LONG' else 'BUY'
        stop_offset_pct = Decimal(str(self.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        silent_error_codes = set(self.risk_config['cleanup_silent_error_codes'])
        
        # 1. 取消旧移动止损条件单（如果存在）
        if position.trailing_stop_order_id is not None:
            try:
                await self.binance.cancel_algo_order(symbol, position.trailing_stop_order_id)
                logger.info(
                    f"{symbol} 旧移动止损条件单已取消",
                    algo_id=position.trailing_stop_order_id
                )
            except BinanceAPIError as e:
                if e.code in silent_error_codes:
                    logger.debug(
                        f"{symbol} 旧移动止损条件单取消失败（可能已成交）",
                        algo_id=position.trailing_stop_order_id,
                        error_code=e.code
                    )
                else:
                    logger.warning(
                        f"{symbol} 取消旧移动止损条件单异常",
                        algo_id=position.trailing_stop_order_id,
                        error_code=e.code,
                        error_msg=e.message
                    )
            except Exception as e:
                logger.warning(
                    f"{symbol} 取消旧移动止损条件单异常",
                    algo_id=position.trailing_stop_order_id,
                    error=str(e)
                )
            position.trailing_stop_order_id = None
        
        # 3. 计算止损限价（触发价向不利方向偏移，确保成交）
        if position.direction == 'LONG':
            stop_limit_price = trailing_stop * (Decimal('1') - stop_offset_pct)
        else:
            stop_limit_price = trailing_stop * (Decimal('1') + stop_offset_pct)
        
        # 精度调整
        try:
            precision = await self._get_symbol_precision(symbol)
            tick_size = Decimal(str(precision.get('tick_size', '0.01')))
            step_size = Decimal(str(precision.get('step_size', '0.001')))
        except Exception:
            tick_size = Decimal('0.01')
            step_size = Decimal('0.001')
        
        stop_limit_price = self._adjust_price_precision(stop_limit_price, tick_size)
        # 移动止损单数量：按尾仓比例（remaining_ratio）计算，而非全仓
        trailing_remaining_ratio = Decimal(
            str(self._get_grade_risk(position.grade)['partial_take_profit']['remaining_ratio'])
        )
        close_quantity = self._adjust_quantity_precision(
            position.initial_quantity * trailing_remaining_ratio, step_size
        )
        
        # 4. 下新止损条件单（统一封装：下单 + 落库，v6.28）
        logger.info(
            f"{symbol} 下移动止损条件单",
            stop_side=stop_side,
            stop_price=float(trailing_stop),
            limit_price=float(stop_limit_price),
            quantity=float(close_quantity)
        )
        new_order_id = await self._place_conditional_order_and_record(
            symbol, stop_side, "STOP", trailing_stop, stop_limit_price,
            close_quantity, "STOP_LOSS", self.strategy_name
        )
        if new_order_id:
            position.trailing_stop_order_id = new_order_id
            logger.info(
                f"{symbol} 移动止损条件单已创建",
                order_id=new_order_id,
                trailing_stop=float(trailing_stop)
            )
    
    async def _should_keep_position(self, symbol: str, position: PositionState,
                                    indicators: Dict, klines: Dict) -> Tuple[bool, str]:
        """v6.27 时间平仓复核编排：数据预检 + 依序复用三机制。

        返回: (True, "")=该持有；(False, reason)=任一机制判定不该持有。
        注：仅编排不重写三机制；_check_volatility_regime 首参为 symbol。
        """
        ranging_config = self.risk_config.get('ranging_strategy', {})
        try:
            # 步骤1：数据可用性预检（任一关键指标缺失 → 保守不该持有）
            required = [('1d', 'EMA21'), ('1d', 'EMA55'), ('4h', 'RSI'),
                        ('4h', 'ATR'), ('4h', 'ATR_long')]
            for timeframe, field in required:
                if _safe_last(indicators, timeframe, field) is None:
                    return False, f"复核数据缺失：{timeframe}.{field}，无法评估风险，保守平仓"
            if not klines.get('1d') or not klines.get('4h'):
                return False, "复核K线数据为空，无法评估风险，保守平仓"

            # 步骤2：机制① 方向一致性对齐（持仓方向与日线排列是否仍一致）
            ok, reason = self._check_direction_alignment(position.direction, indicators, ranging_config)
            logger.info(f"{symbol} 时间平仓复核·机制①方向一致性", ok=ok, reason=reason,
                        ema21=_safe_last(indicators, '1d', 'EMA21'),
                        ema55=_safe_last(indicators, '1d', 'EMA55'))
            if not ok:
                return False, reason

            # 步骤3：机制② 重度过热
            ok, reason = self._check_overheat_ban(position.direction, indicators, klines, ranging_config)
            logger.info(f"{symbol} 时间平仓复核·机制②重度过热", ok=ok, reason=reason,
                        rsi_4h=_safe_last(indicators, '4h', 'RSI'))
            if not ok:
                return False, reason

            # 步骤4：机制③ 波动熔断（首参 symbol，会写 symbol_extreme_pause_until，属预期）
            ok, reason = self._check_volatility_regime(symbol, indicators, ranging_config)
            logger.info(f"{symbol} 时间平仓复核·机制③波动熔断", ok=ok, reason=reason,
                        atr=_safe_last(indicators, '4h', 'ATR'),
                        atr_long=_safe_last(indicators, '4h', 'ATR_long'))
            if not ok:
                return False, reason

            return True, ""
        except Exception as e:
            # 编排层兜底：任何异常 → 保守「不该持有」，不允许中断主循环
            logger.error(f"{symbol} 时间平仓复核异常，保守平仓", error=str(e))
            return False, f"复核异常：{e}"

    async def _do_time_stop_close(self, symbol: str, position: PositionState,
                                  close_ratio: Decimal, reason: str,
                                  trigger_reason: str = "",
                                  review_exhausted: bool = False,
                                  set_tp1_hit: bool = False) -> None:
        """执行时间平仓（原逻辑 / 复核 / 兜底共用，v6.27 消除重复平仓代码）。

        成功：置位防重复标记（复核与兜底置 time_stop_review_done；
        review_enabled=false 原逻辑额外置 tp1_hit 保持现状）。
        失败：保持持仓状态与复核状态，下周期幂等重试。
        """
        close_quantity = position.current_quantity * close_ratio
        log_fields = {
            "holding_hours": (datetime.now() - position.entry_time).total_seconds() / 3600,
            "close_quantity": float(close_quantity),
            "reason": reason,
        }
        if trigger_reason:
            log_fields["trigger"] = trigger_reason
        if review_exhausted:
            log_fields["note"] = "复核期耗尽"
        logger.info(f"{symbol} 触发时间平仓", **log_fields)

        success = await self._close_position(
            symbol=symbol,
            position=position,
            close_quantity=close_quantity,
            close_reason=reason,
            current_price=None,
        )
        if success:
            # v6.27 独立防重复标记（不置 tp1_hit，避免误激活动态止盈 also_on_tp1）
            position.time_stop_review_done = True
            if set_tp1_hit:
                position.tp1_hit = True  # 仅 review_enabled=false 路径保持原逻辑（D6）
        else:
            logger.error(f"{symbol} 时间平仓失败，保持持仓状态", reason=reason)

    def _build_indicators(self, klines: Dict) -> Dict:
        """由多时间框架 K 线构建技术指标（v6.27 提取公共逻辑，消除 analyze 与复核路径重复代码）。

        从 risk_config 读取长周期 ATR 周期（默认 50），遍历各时间框架将原始 K 线
        转为 DataFrame 并逐列强制数值化后，统一调用 TechnicalIndicators.calculate_all，
        保证生产 analyze 与时间平仓复核路径的指标口径完全一致。

        Args:
            klines: 多时间框架 K 线数据 {timeframe: [K线记录]}

        Returns:
            各时间框架技术指标字典 {timeframe: indicators}
        """
        # v6.26：长周期ATR周期从配置读取（CP-4），生产与回测统一口径
        atr_long_period = self.risk_config.get('ranging_strategy', {}).get(
            'volatility_regime', {}).get('atr_long_period', 50)
        indicators = {}
        for timeframe, data in klines.items():
            df = pd.DataFrame(data)
            for col in ('open', 'high', 'low', 'close', 'volume'):
                df[col] = pd.to_numeric(df[col], errors='coerce')
            indicators[timeframe] = TechnicalIndicators.calculate_all(
                df, atr_long_period=atr_long_period)
        return indicators

    async def _get_review_market_data(self, symbol: str) -> Tuple[Optional[Dict], Optional[Dict]]:
        """复核期按需拉取多周期 K 线并计算指标（v6.27，口径与 analyze 一致）。

        返回 (indicators, klines)；任一失败返回 (None, None)，由调用方保守平仓。
        """
        try:
            klines = await self.kline_service.get_multi_timeframe_data(
                symbol=symbol, intervals=self.timeframes)
            if not klines or not klines.get('4h') or not klines.get('1d'):
                logger.warning(f"{symbol} 复核K线数据不完整")
                return None, None

            indicators = self._build_indicators(klines)
            return indicators, klines
        except Exception as e:
            logger.error(f"{symbol} 复核数据获取失败", error=str(e))
            return None, None

    async def _check_time_stop(self, symbol: str, position: PositionState):
        """检查并执行时间止损（v6.27 时间平仓复核制）。

        流程：未到期→return；review_enabled=false→原逻辑平仓；复核期耗尽→兜底平仓；
        未完成复核→按需拉数据+三机制复核。
        """
        # 守卫（维持现状 + v6.27 复核已完成标记）：TP1已触发/无持仓/复核已完成 → 不检查
        if position.tp1_hit or position.current_quantity <= 0 or position.time_stop_review_done:
            return
        grade_risk = self._get_grade_risk(position.grade)
        time_stop_config = grade_risk['time_stop']
        max_holding_hours = time_stop_config['max_holding_hours']
        close_ratio = Decimal(str(time_stop_config['close_ratio']))
        # v6.27 复核制开关与复核期上限（配置缺失默认 False/0，保证向后兼容）
        review_enabled = time_stop_config.get('review_enabled', False)
        max_review_hours = time_stop_config.get('max_review_hours', 0)
        holding_hours = (datetime.now() - position.entry_time).total_seconds() / 3600
        if holding_hours < max_holding_hours:
            return  # 未到时间平仓阈值
        # 兜底（最高优先级）：复核期耗尽，无条件强制平仓（reason 与 review_enabled=false 统一）
        if review_enabled and holding_hours >= max_holding_hours + max_review_hours:
            await self._do_time_stop_close(symbol, position, close_ratio,
                                           reason="TIME_STOP", review_exhausted=True)
            return

        # review_enabled=false：完全恢复原逻辑（无条件到点平仓，置 tp1_hit 防重复）
        if not review_enabled:
            await self._do_time_stop_close(symbol, position, close_ratio,
                                           reason="TIME_STOP", set_tp1_hit=True)
            return

        # 进入复核期：按需拉取数据（仅复核期持仓触发，其余持仓零开销）
        indicators, klines = await self._get_review_market_data(symbol)
        if indicators is None or klines is None:
            await self._do_time_stop_close(symbol, position, close_ratio,
                                           reason="TIME_STOP_REVIEW",
                                           trigger_reason="复核数据获取失败")
            return

        keep, reason = await self._should_keep_position(symbol, position, indicators, klines)
        if keep:
            logger.info(f"{symbol} 时间平仓复核：该持有，继续持有",
                        holding_hours=holding_hours,
                        max_holding_hours=max_holding_hours,
                        review_enabled=review_enabled)
            return

        # 不该持有 → 平 close_ratio
        await self._do_time_stop_close(symbol, position, close_ratio,
                                       reason="TIME_STOP_REVIEW", trigger_reason=reason)
    
    async def _cancel_single_order_with_retry(
        self,
        symbol: str,
        position: PositionState,
        order_type: str,
        order_id: Optional[int],
        is_algo: bool = True,
    ) -> bool:
        """取消单个条件单并处理重试：成功/静默/已执行视为已清理返回 True，可重试错误递增计数"""
        if order_id is None:
            return True
        silent_error_codes = set(self.risk_config['cleanup_silent_error_codes'])
        max_retry = self._get_cancel_retry_config('max_retries', 10)

        # 锁保护并发修改 cancel_retry_count / order_id（v6.23.1）
        async with self._cancel_lock:
            try:
                if is_algo:
                    await self.binance.cancel_algo_order(symbol, order_id)
                else:
                    await self.binance.cancel_order(symbol, order_id=str(order_id))
                logger.info(f"{symbol} {order_type}订单已取消", algo_id=order_id)
                self._clear_order_slot(position, order_type)
                return True
            except BinanceAPIError as e:
                if e.code in silent_error_codes:
                    # 订单不存在或已取消，视为已处理
                    self._clear_order_slot(position, order_type)
                    return True
                if e.code == -2021:
                    # 条件单已执行（触发后成交）
                    self._clear_order_slot(position, order_type)
                    await self._record_executed_conditional_order(symbol, order_type, order_id)
                    return True
                # 可重试错误
                return await self._handle_cancel_retry(
                    symbol, position, order_type, order_id, max_retry, False, e.code)
            except Exception as e:
                # 未知异常，按可重试错误处理
                return await self._handle_cancel_retry(
                    symbol, position, order_type, order_id, max_retry, True, str(e))

    @staticmethod
    def _clear_order_slot(position: PositionState, order_type: str) -> None:
        """清空订单槽位与重试计数（v6.28 拆分子函数）"""
        setattr(position, f"{order_type}_order_id", None)
        position.cancel_retry_count.pop(order_type, None)

    async def _handle_cancel_retry(
        self, symbol: str, position: PositionState, order_type: str,
        order_id: Optional[int], max_retry: int, unknown: bool, err_detail: object,
    ) -> bool:
        """可重试取消错误统一处理：递增重试计数，达上限通知后放弃（v6.28 拆分子函数）

        Args:
            unknown: True=未知异常（日志带"未知异常"且用 error 字段）；False=BinanceAPIError（error_code）
            err_detail: 错误详情（BinanceAPIError 传错误码，未知异常传 str(e)）
        """
        current = position.cancel_retry_count.get(order_type, 0) + 1
        position.cancel_retry_count[order_type] = current
        if current >= max_retry:
            self._clear_order_slot(position, order_type)
            await self._notify_cancel_timeout(symbol, order_type, order_id)
            return True
        if unknown:
            logger.warning(
                f"{symbol} {order_type}取消失败（未知异常），将重试",
                algo_id=order_id, retry_count=current, max_retries=max_retry, error=str(err_detail)
            )
        else:
            logger.warning(
                f"{symbol} {order_type}取消失败，将重试",
                algo_id=order_id, retry_count=current, max_retries=max_retry, error_code=err_detail
            )
        return False

    async def _cleanup_position_orders(self, symbol: str, position: PositionState):
        """清理单个持仓的残余条件单（止损/止盈/未成交入场单，统一走 _cancel_single_order_with_retry）"""
        # 1. 止损条件单
        if position.stop_loss_order_id is not None:
            await self._cancel_single_order_with_retry(
                symbol, position, "stop_loss", position.stop_loss_order_id, is_algo=True
            )
        
        # 2. 取消TP1止盈条件单
        if position.tp1_order_id is not None:
            await self._cancel_single_order_with_retry(
                symbol, position, "tp1", position.tp1_order_id, is_algo=True
            )
        
        # 3. 取消TP2止盈条件单
        if position.tp2_order_id is not None:
            await self._cancel_single_order_with_retry(
                symbol, position, "tp2", position.tp2_order_id, is_algo=True
            )
        
        # 4. 取消移动止损条件单
        if position.trailing_stop_order_id is not None:
            await self._cancel_single_order_with_retry(
                symbol, position, "trailing_stop", position.trailing_stop_order_id, is_algo=True
            )
        
        # 5. 取消未成交的入场限价单
        if position.entry_order_id is not None:
            await self._cancel_single_order_with_retry(
                symbol, position, "entry", position.entry_order_id, is_algo=False
            )
        
        # 判断是否所有条件单都已清理完毕
        has_pending_retry = any(
            position.cancel_retry_count.get(key, 0) > 0
            for key in ['stop_loss', 'tp1', 'tp2', 'trailing_stop', 'entry']
        )
        if (position.stop_loss_order_id is None
                and position.tp1_order_id is None
                and position.tp2_order_id is None
                and position.trailing_stop_order_id is None
                and position.entry_order_id is None
                and not has_pending_retry):
            position.cancel_pending = False
    
    async def _retry_pending_cancellations(self):
        """
        每周期重试待取消的条件单（v6.23 孤儿条件单修复）
        
        遍历 self.positions，对 cancel_pending=True 且 cancel_retry_count 不为空的持仓，
        重新调用 _cleanup_position_orders() 执行取消重试。
        
        v6.23.1 新增：
        - 重试间隔控制：仅当 current_cycle - last_retry_cycle >= retry_interval_cycles 时执行
        - 强制清理超时：首次重试时间超过 max_cleanup_hours 时放弃重试
        """
        retry_interval = self._get_cancel_retry_config('retry_interval_cycles', 1)
        max_cleanup_hours = self._get_cancel_retry_config('max_cleanup_hours', 48)
        current_cycle = self._cycle_count
        now = datetime.now()
        
        for symbol, position in list(self.positions.items()):
            if not position.cancel_pending:
                continue
            if not position.cancel_retry_count:
                continue
            
            # 检查重试间隔
            if current_cycle - position.last_retry_cycle < retry_interval:
                continue
            
            # 检查强制清理超时：首次重试时间超过 max_cleanup_hours 则放弃
            if position.first_retry_time is not None:
                elapsed_hours = (now - position.first_retry_time).total_seconds() / 3600
                if elapsed_hours > max_cleanup_hours:
                    logger.warning(
                        f"{symbol} 条件单重试超时（超过{max_cleanup_hours}小时），强制放弃重试",
                        first_retry_time=str(position.first_retry_time),
                        elapsed_hours=round(elapsed_hours, 1),
                        retry_count=position.cancel_retry_count
                    )
                    position.cancel_retry_count.clear()
                    position.cancel_pending = False
                    continue
            
            logger.info(
                f"{symbol} 检测到待重试条件单，执行重试",
                retry_count=position.cancel_retry_count,
                cycle=current_cycle
            )
            
            # 记录首次重试时间
            if position.first_retry_time is None:
                position.first_retry_time = now
            
            position.last_retry_cycle = current_cycle
            await self._cleanup_position_orders(symbol, position)

    async def _cleanup_residual_orders(self):
        """
        清理已平仓持仓的残余条件单（第二层防护：兜底扫描）
        
        遍历 self.positions，对 current_quantity <= 0 的持仓执行条件单清理，
        清理完成后从 self.positions 中删除该持仓记录。
        使用 list() 避免迭代中修改字典。
        """
        # 跳过第一层异步清理正在处理中的持仓，避免重复取消
        symbols_to_clean = [
            symbol for symbol, pos in self.positions.items()
            if pos.current_quantity <= 0 and not pos.cancel_pending
        ]
        
        if not symbols_to_clean:
            return
        
        for symbol in symbols_to_clean:
            position = self.positions.get(symbol)
            if position is None:
                continue
            
            logger.info(
                f"{symbol} 扫描发现已平仓持仓，执行残余条件单清理",
                cancel_pending=position.cancel_pending
            )
            
            await self._cleanup_position_orders(symbol, position)
            
            # v6.23：检查是否仍有待重试的条件单，有则保留持仓记录
            if position.cancel_retry_count:
                logger.info(
                    f"{symbol} 仍有待重试条件单，保留持仓记录",
                    retry_count=position.cancel_retry_count
                )
                position.cancel_pending = True
                continue
            
            # 清理完成后删除持仓记录
            if symbol in self.positions:
                del self.positions[symbol]
                logger.info(
                    f"{symbol} 已平仓持仓记录已删除",
                    final_cancel_pending=position.cancel_pending
                )
        
        # 兜底：长时间处于 cancel_pending 状态但无待重试项的持仓
        # v6.23.1：同时检查是否还有未取消的 order_id，避免第1层异步失败后丢失追踪
        for symbol, pos in list(self.positions.items()):
            if pos.current_quantity <= 0 and pos.cancel_pending:
                if not pos.cancel_retry_count:
                    has_residual_orders = (
                        pos.stop_loss_order_id is not None
                        or pos.tp1_order_id is not None
                        or pos.tp2_order_id is not None
                        or pos.trailing_stop_order_id is not None
                        or pos.entry_order_id is not None
                    )
                    if has_residual_orders:
                        # 还有残留条件单未取消，重新触发清理
                        logger.info(f"{symbol} 兜底：发现残留条件单，重新触发清理",
                                     stop_loss=pos.stop_loss_order_id, tp1=pos.tp1_order_id, tp2=pos.tp2_order_id)
                        await self._cleanup_position_orders(symbol, pos)
                        if pos.cancel_retry_count:
                            pos.cancel_pending = True
                            continue
                    pos.cancel_pending = False
                    logger.info(f"{symbol} 兜底清理：cancel_pending 已无待重试项")
    
    async def _record_executed_conditional_order(self, symbol: str, order_type: str, algo_id: int):
        """
        记录条件单触发后的成交（v6.23 孤儿条件单修复）
        
        当 _cleanup_position_orders 遇到 -2021（已执行）错误码时调用，
        更新 condition_orders 表状态为 EXECUTED，
        并查询 trade_records 表确认是否有匹配的成交记录（v6.23.1）。
        
        Args:
            symbol: 交易对
            order_type: 条件单类型
            algo_id: 条件单 ID
        """
        if not self.db_manager:
            return
        
        try:
            from shared.condition_orders import mark_order_executed
            await mark_order_executed(self.db_manager, algo_id=algo_id)
            logger.info(
                f"{symbol} 条件单已执行，状态已更新",
                algo_id=algo_id,
                order_type=order_type
            )
        except Exception as e:
            logger.warning(f"记录条件单执行状态失败", algo_id=algo_id, error=str(e))
            return
        
        # v6.23.1：查询 trade_records 表确认是否有匹配的成交记录
        try:
            # 明确指定北京时区（UTC+8），与 trade_records.executed_at 时区一致
            beijing_tz = timezone(timedelta(hours=8))
            now = datetime.now(beijing_tz)
            lookup_window = self._get_cancel_retry_config('trade_lookup_window_minutes', 5)
            window_start = now - timedelta(minutes=lookup_window)
            window_end = now + timedelta(minutes=lookup_window)
            
            trade_records = await self.db_manager.fetch_all(
                """SELECT id, order_id, side, quantity, price, executed_at
                   FROM trading.trade_records
                   WHERE symbol = $1
                     AND executed_at >= $2
                     AND executed_at <= $3
                   ORDER BY executed_at DESC
                   LIMIT 5""",
                symbol, window_start, window_end
            )
            
            if trade_records:
                logger.info(
                    f"{symbol} 条件单已执行，找到匹配的成交记录",
                    algo_id=algo_id,
                    order_type=order_type,
                    trade_count=len(trade_records),
                    trade_ids=[r['id'] for r in trade_records]
                )
            else:
                logger.warning(
                    f"{symbol} 条件单已执行但无成交记录，需人工核查",
                    algo_id=algo_id,
                    order_type=order_type,
                    message="条件单已执行，但 trade_records 表中未找到匹配的成交记录"
                )
        except Exception as e:
            logger.warning(f"查询成交记录失败", algo_id=algo_id, error=str(e))
    
    def _get_cancel_retry_config(self, key: str, default=None):
        """读取条件单取消重试配置（v6.23）"""
        cancel_retry_config = self.risk_config.get('cancel_retry', {})
        return cancel_retry_config.get(key, default)
    
    async def _notify_cancel_timeout(self, symbol: str, order_type: str, algo_id: int):
        """条件单取消超时告警（v6.23）"""
        logger.error(
            f"{symbol} 条件单取消超时",
            algo_id=algo_id,
            order_type=order_type,
            message="已达最大重试次数，放弃取消，孤儿条件单将在交易所存活"
        )
        
        # v6.23.1：根据配置决定是否发送通知
        notify_on_timeout = self._get_cancel_retry_config('notify_on_timeout', True)
        if not notify_on_timeout:
            logger.info("配置已禁用超时通知，跳过发送")
            return
        
        try:
            await self.notification.send_error_notification(
                strategy=self.strategy_name,
                error_message=f"条件单取消超时: {symbol} {order_type} (algo_id={algo_id})，已达最大重试次数，请人工核查",
                symbol=symbol
            )
        except Exception as e:
            logger.warning(f"发送取消超时告警失败", error=str(e))

    async def _wait_for_order_fill(
        self,
        symbol: str,
        order_id: int,
        timeout_seconds: int = 60,
        check_interval: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """
        等待限价单成交，超时返回 None

        Args:
            symbol: 交易对
            order_id: 订单 ID
            timeout_seconds: 超时秒数
            check_interval: 检查间隔（秒）

        Returns:
            成交后的订单信息，超时返回 None
        """
        try:
            deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
            while datetime.now(timezone.utc) < deadline:
                order = await self.binance.get_order(symbol, order_id)
                status = order.get("status", "")

                if status == "FILLED":
                    logger.info(
                        f"{symbol} 限价单已成交",
                        order_id=order_id,
                        executed_qty=order.get("executedQty"),
                        cummulative_quote=order.get("cummulativeQuoteQty"),
                    )
                    return order

                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.warning(
                        f"{symbol} 限价单已取消/过期/拒绝",
                        order_id=order_id,
                        status=status,
                    )
                    return None

                await asyncio.sleep(check_interval)

            logger.warning(
                f"{symbol} 限价单超时未成交",
                order_id=order_id,
                timeout_seconds=timeout_seconds,
            )
            return None
        except Exception as e:
            logger.error(
                f"{symbol} 检查限价单成交状态异常",
                order_id=order_id,
                error=str(e),
            )
            return None

    async def cleanup_orphan_algo_orders(self):
        """
        清理孤儿条件单（第三层防护：进程重启后兜底，已废弃API）

        条件单查询API（/papi/v1/um/algo/openOrders）已废弃，
        不再通过交易所查询未关联条件单。
        孤儿条件单清理由以下机制替代：
        - 第1层：平仓时通过 _cleanup_position_orders() 使用本地记录的 algoId 取消
        - 第2层：每周期扫描残留订单 _cleanup_residual_orders() 使用本地记录的 algoId 取消
        - 持仓同步：_sync_positions_with_exchange() 清理交易所已不存在的持仓记录
        """
        logger.info("条件单查询API已废弃，跳过启动时孤儿条件单清理，由第1/2层防护替代")

    async def _startup_orphan_cleanup(self):
        """
        启动时孤儿条件单检测与清理（v6.23 孤儿条件单修复）
        
        v6.23.1 新增：
        - 超时保护：asyncio.wait_for 30秒超时
        - 降级兜底：批量取消失败时回退到逐个取消
        - 批量更新：使用一条 SQL 批量更新 condition_orders 状态
        - 通知修复：使用 notification.send() 替代 send_trade_notification
        
        通过 condition_orders 表查询策略的 OPEN 状态条件单，
        与交易所当前持仓对比，对无对应持仓的孤儿条件单执行批量取消。
        """
        if not self.db_manager:
            logger.info("无数据库管理器，跳过启动时孤儿条件单检测")
            return
        
        cleanup_timeout = self._get_cancel_retry_config('cleanup_timeout_seconds', 30)
        try:
            await asyncio.wait_for(self._do_startup_orphan_cleanup(), timeout=cleanup_timeout)
        except asyncio.TimeoutError:
            logger.warning(f"启动时孤儿条件单检测超时（{cleanup_timeout}秒），跳过清理")
        except Exception as e:
            logger.error("启动时孤儿条件单检测失败", error=str(e), exc_info=True)
    
    async def _do_startup_orphan_cleanup(self):
        """
        启动时孤儿条件单检测与清理的内部实现（v6.23.1 提取为独立方法）
        
        由 _startup_orphan_cleanup() 调用，添加了超时保护。
        """
        try:
            # 1. 查询 OPEN 条件单
            from shared.condition_orders import get_open_orders, mark_order_canceled
            open_orders = await get_open_orders(self.db_manager, "btc_eth")
            if not open_orders:
                logger.info("启动时孤儿条件单检测：无 OPEN 条件单，跳过")
                return
            
            logger.info("启动时孤儿条件单检测：发现 OPEN 条件单", count=len(open_orders))
            
            # 2. 查询交易所当前持仓
            exchange_positions = await self.binance.get_position()
            held_symbols = set()
            for pos in exchange_positions:
                amt = float(pos.get('positionAmt', 0))
                if abs(amt) > self.min_position_amt:
                    held_symbols.add(pos.get('symbol', ''))
            
            # 3. 识别孤儿条件单
            orphan_orders = [o for o in open_orders if o.get('symbol', '') not in held_symbols]
            if not orphan_orders:
                logger.info("启动时孤儿条件单检测：无孤儿条件单，跳过")
                return
            
            # 4. 按 symbol 分组，尝试批量取消（v6.23.1：失败时降级兜底）
            #    归属守卫（方案C）：跳过归属其他策略的 symbol，不清理他人仓
            orphan_symbols = set(o['symbol'] for o in orphan_orders)
            _my_name = getattr(self, 'my_record_name', None)
            _guard_kept = set()
            for _symbol in orphan_symbols:
                try:
                    _owner = await resolve_position_owner(self.db_manager, _symbol)
                except Exception as _e:
                    _owner = None
                if _owner is not None and _owner != _my_name:
                    logger.warning(
                        "孤儿条件单归属其他策略，跳过清理",
                        symbol=_symbol,
                        owner=_owner,
                    )
                    continue
                _guard_kept.add(_symbol)
            orphan_symbols = _guard_kept
            if not orphan_symbols:
                logger.info("孤儿条件单清理：全部被归属守卫过滤，跳过")
                return
            cancel_success = 0
            cancel_fail = 0
            
            for symbol in orphan_symbols:
                try:
                    await self.binance.cancel_all_algo_orders(symbol)
                    logger.info(f"{symbol} 孤儿条件单批量取消成功")
                    cancel_success += 1
                except Exception as e:
                    logger.warning(f"{symbol} 孤儿条件单批量取消失败，回退到逐个取消", error=str(e))
                    # v6.23.1：降级兜底，逐个取消该 symbol 下的孤儿条件单
                    fallback_success = 0
                    fallback_fail = 0
                    for order in orphan_orders:
                        if order.get('symbol') != symbol:
                            continue
                        algo_id = order.get('algo_id')
                        if algo_id:
                            try:
                                await self.binance.cancel_algo_order(symbol, algo_id)
                                fallback_success += 1
                            except Exception as e2:
                                logger.debug(f"逐个取消孤儿条件单失败", algo_id=algo_id, error=str(e2))
                                fallback_fail += 1
                    if fallback_success > 0:
                        logger.info(f"{symbol} 逐个取消孤儿条件单完成", success=fallback_success, fail=fallback_fail)
                        cancel_success += 1
                    else:
                        cancel_fail += 1
            
            # 5. 批量更新 condition_orders 表状态（v6.23.1：一条 SQL 批量更新）
            orphan_algo_ids = [o.get('algo_id') for o in orphan_orders if o.get('algo_id')]
            if orphan_algo_ids:
                try:
                    # 使用参数化查询批量更新
                    placeholders = ",".join([f"${i+1}" for i in range(len(orphan_algo_ids))])
                    await self.db_manager.execute(
                        "UPDATE condition_orders SET status='CANCELED', updated_at=NOW() "
                        f"WHERE algo_id IN ({placeholders}) AND status='OPEN'",
                        *orphan_algo_ids
                    )
                    logger.info("批量更新孤儿条件单状态完成", count=len(orphan_algo_ids))
                except Exception as e:
                    logger.warning(f"批量更新条件单状态失败，回退到逐个更新", error=str(e))
                    # 回退：逐个更新
                    for order in orphan_orders:
                        try:
                            algo_id = order.get('algo_id')
                            if algo_id:
                                await mark_order_canceled(self.db_manager, algo_id=algo_id)
                        except Exception as e2:
                            logger.debug(f"更新条件单状态失败: {e2}")
            
            # 6. 发送飞书通知（v6.23.1：改用 notification.send 避免 ValueError）
            try:
                message = (
                    f"启动时孤儿条件单清理完成: "
                    f"成功{cancel_success}个symbol, 失败{cancel_fail}个symbol, "
                    f"共{len(orphan_orders)}个条件单"
                )
                await self.notification.send(
                    message=message,
                    level="info",
                    project="btc_eth"
                )
            except Exception as e:
                logger.warning(f"发送孤儿条件单清理通知失败", error=str(e))
            
            logger.info("启动时孤儿条件单清理完成", success=cancel_success, fail=cancel_fail, total=len(orphan_orders))
            
        except Exception as e:
            logger.error("启动时孤儿条件单检测失败", error=str(e), exc_info=True)

    async def _ensure_position_protection(self):
        """确保持仓有止损止盈保护单（v6.20.5）

        获取交易所当前持仓，检查已有持仓记录的条件单ID完整性。
        对缺少保护单的持仓自动补单（计算ATR、止损价、止盈价）。

        防重复创建（v6.20.5）：创建新条件单前先查询 condition_orders 表
        该 symbol 是否已有 OPEN 条件单，若有则跳过创建，避免容器重启后重复创建。
        """
        logger.info("开始持仓保护检查...")
        try:
            exchange_positions = await self.binance.get_position()
        except Exception as e:
            logger.warning("获取交易所持仓失败，跳过持仓保护检查", error=str(e))
            return
        if not exchange_positions:
            logger.info("交易所无持仓，无需保护")
            return

        # 查询已有 OPEN 条件单（防重复创建）+ 读取止盈止损配置
        existing_open_orders = await self._load_existing_open_orders()
        stop_offset_pct, tp_offset_pct, stop_loss_atr = self._calc_protection_params()
        managed_symbols = set(self.symbols)

        for pos_data in exchange_positions:
            await self._ensure_symbol_protection(
                pos_data, managed_symbols, existing_open_orders,
                stop_offset_pct, tp_offset_pct, stop_loss_atr)
        logger.info("持仓保护检查完成")

    async def _load_existing_open_orders(self) -> Dict[str, Dict[str, list]]:
        """查询 condition_orders 表中已有 OPEN 条件单（防重复创建，v6.28 拆分子函数）"""
        existing_open_orders: Dict[str, Dict[str, list]] = {}
        if not self.db_manager:
            return existing_open_orders
        try:
            orders = await get_open_orders(self.db_manager, self.strategy_name)
            for o in orders:
                sym = o.get('symbol')
                existing_open_orders.setdefault(sym, {}).setdefault(
                    o.get('order_type'), []).append(o.get('algo_id'))
            if existing_open_orders:
                logger.info("检测到已有OPEN条件单，跳过重复创建", symbols=list(existing_open_orders.keys()))
        except Exception as e:
            logger.warning("查询 condition_orders 表失败，不影响后续创建", error=str(e))
        return existing_open_orders

    def _calc_protection_params(self) -> Tuple[Decimal, Decimal, Decimal]:
        """读取止盈止损配置参数（v6.28 拆分子函数）"""
        risk_config = self.risk_config or {}
        stop_offset_pct = Decimal(str(risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        tp_offset_pct = Decimal(str(risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))
        stop_loss_atr = Decimal(str(risk_config.get('stop_loss_atr_multiplier', 1.0)))
        return stop_offset_pct, tp_offset_pct, stop_loss_atr

    async def _ensure_symbol_protection(
        self, pos_data: Dict, managed_symbols: set,
        existing_open_orders: Dict[str, Dict[str, list]],
        stop_offset_pct: Decimal, tp_offset_pct: Decimal, stop_loss_atr: Decimal,
    ) -> None:
        """单个持仓的保护单检查与补挂（v6.28 拆分子函数）"""
        symbol = pos_data.get('symbol', '')
        position_amt = float(pos_data.get('positionAmt', 0))
        if symbol not in managed_symbols or abs(position_amt) < self.min_position_amt:
            return
        direction = 'LONG' if position_amt > 0 else 'SHORT'
        current_quantity = Decimal(str(abs(position_amt)))
        logger.info(
            f"{symbol} 检测到交易所持仓",
            direction=direction,
            quantity=float(current_quantity)
        )

        existing_pos = self.positions.get(symbol)
        has_stop, has_tp1, has_tp2 = self._check_existing_protection(
            symbol, existing_pos, existing_open_orders)
        if has_stop and has_tp1 and has_tp2:
            self._log_full_protection_present(symbol, existing_pos, existing_open_orders)
            return

        # 持仓归属校验（方案C）：不为对家策略开的仓位补挂保护单
        owner = await resolve_position_owner(self.db_manager, symbol)
        if owner is None or owner != self.my_record_name:
            reason = "无法判定归属" if owner is None else f"归属{owner}"
            logger.warning(
                f"{symbol} 归属校验拒绝补挂保护单",
                owner=owner,
                my_record_name=self.my_record_name,
            )
            try:
                await self.notification.send(
                    message=f"{symbol} 持仓归属校验失败（{reason}），跳过补挂保护单",
                    level="warning",
                    project="btc_eth",
                )
            except Exception as _e:
                logger.warning("发送归属告警失败", error=str(_e))
            return

        # 计算 ATR / 精度 / 保护价格
        current_price = await self._get_current_price(symbol)
        if current_price is None:
            logger.warning(f"{symbol} 获取当前价格失败，跳过保护")
            return
        atr = await self._calc_protection_atr(symbol, current_price)
        tick_size, step_size = await self._get_protection_precision(symbol)
        grade = existing_pos.grade if existing_pos and existing_pos.grade else 'A'
        prices = self._calc_protection_prices(
            direction, current_price, atr, grade, current_quantity,
            stop_offset_pct, tp_offset_pct, stop_loss_atr, tick_size, step_size)

        # 补挂硬止损（内部处理创建持仓状态）；TP1/TP2 独立补挂
        if not has_stop:
            existing_pos = await self._place_protection_stop(
                symbol, direction, current_price, atr, current_quantity, prices, existing_pos)
        existing_pos = self._get_or_create_position_state(
            symbol, existing_pos, current_price, direction, current_quantity, atr, grade='A')
        await self._place_missing_tp_orders(
            symbol, direction, existing_pos, has_tp1, has_tp2, prices)

    @staticmethod
    def _check_existing_protection(
        symbol: str, existing_pos: Optional[PositionState],
        existing_open_orders: Dict[str, Dict[str, list]],
    ) -> Tuple[bool, bool, bool]:
        """检查该持仓已有保护单（self.positions + condition_orders 表，v6.28 拆分子函数）"""
        has_stop = existing_pos and existing_pos.stop_loss_order_id is not None
        has_tp1 = existing_pos and existing_pos.tp1_order_id is not None
        has_tp2 = existing_pos and existing_pos.tp2_order_id is not None
        existing_orders = existing_open_orders.get(symbol, {})
        stop_orders = existing_orders.get('STOP_LOSS', [])
        tp_orders = existing_orders.get('TAKE_PROFIT', [])
        if not has_stop and stop_orders:
            has_stop = True
            logger.info(
                f"{symbol} 从 condition_orders 表检测到已有 STOP_LOSS",
                algo_id=stop_orders[0]
            )
        if not has_tp1 and len(tp_orders) >= 1:
            has_tp1 = True
            logger.info(
                f"{symbol} 从 condition_orders 表检测到已有 TP1 止盈单",
                algo_id=tp_orders[0]
            )
        if not has_tp2 and len(tp_orders) >= 2:
            has_tp2 = True
            logger.info(
                f"{symbol} 从 condition_orders 表检测到已有 TP2 止盈单",
                algo_id=tp_orders[1]
            )
        return has_stop, has_tp1, has_tp2

    @staticmethod
    def _log_full_protection_present(
        symbol: str, existing_pos: Optional[PositionState],
        existing_open_orders: Dict[str, Dict[str, list]],
    ) -> None:
        """已有完整保护单时输出含订单ID的跳过日志（v6.28 拆分子函数）"""
        stop_orders = existing_open_orders.get(symbol, {}).get('STOP_LOSS', [])
        tp_orders = existing_open_orders.get(symbol, {}).get('TAKE_PROFIT', [])
        stop_order_id = (
            existing_pos.stop_loss_order_id if existing_pos
            else (stop_orders[0] if stop_orders else None)
        )
        tp1_order_id = (
            existing_pos.tp1_order_id if existing_pos
            else (tp_orders[0] if len(tp_orders) >= 1 else None)
        )
        tp2_order_id = (
            existing_pos.tp2_order_id if existing_pos
            else (tp_orders[1] if len(tp_orders) >= 2 else None)
        )
        logger.info(
            f"{symbol} 已有完整保护单，跳过",
            stop_order_id=stop_order_id,
            tp1_order_id=tp1_order_id,
            tp2_order_id=tp2_order_id
        )

    async def _calc_protection_atr(self, symbol: str, current_price: Decimal) -> Decimal:
        """计算保护单用的 ATR，异常/缺失时回退默认 1%（v6.28 拆分子函数）"""
        try:
            klines = await self.kline_service.get_klines(symbol, '1h', limit=60)
            if klines is None or len(klines) <= 20:
                logger.warning(f"{symbol} K线数据不足，使用默认ATR(1%)")
                return current_price * Decimal('0.01')
            df = pd.DataFrame(klines)
            indicators_data = TechnicalIndicators.calculate_all(df)
            atr_series = indicators_data.get("ATR")
            if atr_series is None or len(atr_series) == 0:
                logger.warning(f"{symbol} ATR数据为空，使用默认ATR(1%)")
                return current_price * Decimal('0.01')
            atr_value = atr_series.iloc[-1]
            if pd.isna(atr_value) or abs(atr_value) > 1e30:
                logger.warning(f"{symbol} ATR值异常，使用默认ATR(1%)")
                return current_price * Decimal('0.01')
            return Decimal(str(float(atr_value)))
        except Exception as e:
            logger.warning(f"{symbol} ATR计算异常，使用默认ATR(1%)", error=str(e))
            return current_price * Decimal('0.01')

    async def _get_protection_precision(self, symbol: str) -> Tuple[Decimal, Decimal]:
        """获取交易对精度（tick_size/step_size），异常时用默认值（v6.28 拆分子函数）"""
        try:
            precision = await self._get_symbol_precision(symbol)
            tick_size = Decimal(str(precision.get('tick_size', '0.01')))
            step_size = Decimal(str(precision.get('step_size', '0.001')))
        except Exception:
            tick_size = Decimal('0.01')
            step_size = Decimal('0.001')
        return tick_size, step_size

    def _calc_protection_prices(
        self, direction: str, current_price: Decimal, atr: Decimal, grade: str,
        current_quantity: Decimal, stop_offset_pct: Decimal, tp_offset_pct: Decimal,
        stop_loss_atr: Decimal, tick_size: Decimal, step_size: Decimal,
    ) -> Dict:
        """计算止损/止盈触发价、限价与数量（含精度调整，v6.28 拆分子函数）"""
        tp_grade_risk = self._get_grade_risk(grade)
        tp1_atr_mult = Decimal(str(tp_grade_risk['partial_take_profit']['tp1_atr_multiplier']))
        tp2_atr_mult = Decimal(str(tp_grade_risk['partial_take_profit']['tp2_atr_multiplier']))
        tp1_close_ratio = Decimal(str(tp_grade_risk['partial_take_profit']['tp1_close_ratio']))
        tp2_close_ratio = Decimal(str(tp_grade_risk['partial_take_profit']['tp2_close_ratio']))
        tp1_quantity = self._adjust_quantity_precision(current_quantity * tp1_close_ratio, step_size)
        tp2_quantity = self._adjust_quantity_precision(current_quantity * tp2_close_ratio, step_size)

        if direction == 'LONG':
            stop_price = current_price - atr * stop_loss_atr
            tp1_price = current_price + atr * tp1_atr_mult
            tp2_price = current_price + atr * tp2_atr_mult
            # 止损/止盈限价：向不利方向偏移
            stop_limit_price = stop_price * (Decimal('1') - stop_offset_pct)
            tp1_limit_price = tp1_price * (Decimal('1') - tp_offset_pct)
            tp2_limit_price = tp2_price * (Decimal('1') - tp_offset_pct)
        else:  # SHORT
            stop_price = current_price + atr * stop_loss_atr
            tp1_price = current_price - atr * tp1_atr_mult
            tp2_price = current_price - atr * tp2_atr_mult
            # 止损/止盈限价：向不利方向偏移
            stop_limit_price = stop_price * (Decimal('1') + stop_offset_pct)
            tp1_limit_price = tp1_price * (Decimal('1') + tp_offset_pct)
            tp2_limit_price = tp2_price * (Decimal('1') + tp_offset_pct)

        stop_limit_price = self._adjust_price_precision(stop_limit_price, tick_size)
        tp1_limit_price = self._adjust_price_precision(tp1_limit_price, tick_size)
        tp2_limit_price = self._adjust_price_precision(tp2_limit_price, tick_size)
        close_quantity = self._adjust_quantity_precision(current_quantity, step_size)
        return {
            'stop_price': stop_price,
            'stop_limit_price': stop_limit_price,
            'tp1_price': tp1_price,
            'tp1_limit_price': tp1_limit_price,
            'tp2_price': tp2_price,
            'tp2_limit_price': tp2_limit_price,
            'tp1_quantity': tp1_quantity,
            'tp2_quantity': tp2_quantity,
            'close_quantity': close_quantity,
        }

    async def _place_protection_stop(
        self, symbol: str, direction: str, current_price: Decimal, atr: Decimal,
        current_quantity: Decimal, prices: Dict, existing_pos: Optional[PositionState],
    ) -> Optional[PositionState]:
        """补挂硬止损单并更新持仓状态（v6.28 拆分子函数）"""
        stop_side = 'SELL' if direction == 'LONG' else 'BUY'
        algo_id = await self._place_conditional_order_and_record(
            symbol, stop_side, "STOP", prices['stop_price'], prices['stop_limit_price'],
            prices['close_quantity'], "STOP_LOSS", self.strategy_name
        )
        if algo_id:
            logger.info(
                f"{symbol} 止损限价单已创建",
                stop_price=float(prices['stop_price']),
                limit_price=float(prices['stop_limit_price']),
                algo_id=algo_id
            )
            if existing_pos is None:
                existing_pos = self._get_or_create_position_state(
                    symbol, existing_pos, current_price, direction, current_quantity, atr)
            existing_pos.stop_loss_order_id = algo_id
        return existing_pos

    def _get_or_create_position_state(
        self, symbol: str, existing_pos: Optional[PositionState],
        current_price: Decimal, direction: str, current_quantity: Decimal,
        atr: Decimal, grade: Optional[str] = None,
    ) -> PositionState:
        """获取或创建持仓状态（v6.28 拆分子函数）

        grade 参数仅兜底创建场景使用（重启补挂时等级未知，原逻辑设 'A'）。
        """
        if existing_pos is not None:
            return existing_pos
        existing_pos = PositionState()
        existing_pos.entry_price = current_price
        existing_pos.entry_time = datetime.now()
        existing_pos.direction = direction
        existing_pos.initial_quantity = current_quantity
        existing_pos.current_quantity = current_quantity
        existing_pos.atr = atr
        if grade is not None:
            existing_pos.grade = grade
        self.positions[symbol] = existing_pos
        return existing_pos

    async def _place_missing_tp_orders(
        self, symbol: str, direction: str, existing_pos: PositionState,
        has_tp1: bool, has_tp2: bool, prices: Dict,
    ) -> None:
        """补挂缺失的 TP1/TP2 止盈单（v6.28 拆分子函数）"""
        if not has_tp1:
            try:
                tp1_order_id = await self._place_tp_protection_order(
                    symbol, direction, prices['tp1_price'], prices['tp1_limit_price'],
                    prices['tp1_quantity'])
                if tp1_order_id:
                    existing_pos.tp1_order_id = tp1_order_id
            except Exception as e:
                logger.warning(f"{symbol} 创建TP1止盈限价单失败", error=str(e))
        if not has_tp2:
            try:
                tp2_order_id = await self._place_tp_protection_order(
                    symbol, direction, prices['tp2_price'], prices['tp2_limit_price'],
                    prices['tp2_quantity'])
                if tp2_order_id:
                    existing_pos.tp2_order_id = tp2_order_id
            except Exception as e:
                logger.warning(f"{symbol} 创建TP2止盈限价单失败", error=str(e))

    async def _place_tp_protection_order(
        self,
        symbol: str,
        direction: str,
        tp_price: Decimal,
        tp_limit_price: Decimal,
        tp_quantity: Decimal,
    ) -> Optional[str]:
        """补挂单个止盈条件单并记录到数据库，返回条件单ID。

        Args:
            symbol: 交易对
            direction: 持仓方向（LONG/SHORT）
            tp_price: 止盈触发价
            tp_limit_price: 止盈限价
            tp_quantity: 止盈数量

        Returns:
            条件单ID（algoId 或 orderId），下单失败返回 None
        """
        tp_side = 'SELL' if direction == 'LONG' else 'BUY'
        algo_id = await self._place_conditional_order_and_record(
            symbol, tp_side, "TAKE_PROFIT", tp_price, tp_limit_price,
            tp_quantity, "TAKE_PROFIT", self.strategy_name
        )
        if algo_id:
            logger.info(
                f"{symbol} 补挂止盈条件单",
                tp_price=float(tp_price),
                limit_price=float(tp_limit_price),
                algo_id=algo_id
            )
        return algo_id

    async def _sync_positions_with_exchange(self):
        """
        同步持仓状态：清除交易所已不存在的僵尸持仓记录（v6.20.4）
        
        获取交易所当前持仓，将 strategy.positions 中交易所已不存在的
        持仓记录清理掉。但保留有未成交入场限价单的持仓（避免信号已生成
        但限价单未成交时被误清理）。
        """
        try:
            exchange_positions = await self.binance.get_position()
        except Exception as e:
            logger.warning("获取交易所持仓失败，跳过持仓同步", error=str(e))
            return

        # 构建交易所持仓集合
        exchange_symbols = set()
        for pos in exchange_positions or []:
            position_amt = float(pos.get('positionAmt', 0))
            if abs(position_amt) > self.min_position_amt:
                exchange_symbols.add(pos.get('symbol', ''))

        # 检查策略持仓中哪些交易所已不存在
        symbols_to_remove = []
        for symbol in list(self.positions.keys()):
            if symbol not in exchange_symbols:
                position = self.positions[symbol]

                # 检查是否有未成交的入场限价单
                if position.entry_order_id is not None:
                    try:
                        open_orders = await self.binance.get_open_orders(symbol)
                        has_pending_entry = any(
                            str(o.get('orderId')) == str(position.entry_order_id)
                            for o in open_orders
                        )
                        if has_pending_entry:
                            logger.info(
                                f"{symbol} 入场限价单(order_id={position.entry_order_id})尚未成交，跳过清理"
                            )
                            continue
                    except Exception as e:
                        logger.warning(f"{symbol} 检查入场订单状态失败", error=str(e))

                # 交易所无该币种持仓且无未成交入场单，清理记录
                symbols_to_remove.append(symbol)
                logger.info(
                    f"{symbol} 交易所已无持仓，清理策略持仓记录",
                    direction=position.direction,
                    quantity=float(position.current_quantity)
                )

        # 执行清理
        for symbol in symbols_to_remove:
            del self.positions[symbol]

        if symbols_to_remove:
            logger.info("持仓同步完成", removed_count=len(symbols_to_remove))
        else:
            logger.debug("持仓同步完成，无需清理")

    async def _cancel_orphan_algo_orders_by_symbol(self, symbol: str):
        """
        清理单个币种的孤儿条件单（兜底清理，已废弃API）

        条件单查询API（/papi/v1/um/algo/openOrders）已废弃，
        不再通过交易所查询残留条件单。
        孤儿条件单清理由以下机制替代：
        - 第1层：平仓时通过 _cleanup_position_orders() 使用本地记录的 algoId 取消
        - 第2层：每周期 _cleanup_residual_orders() 使用本地记录的 algoId 取消
        - 持仓同步：_sync_positions_with_exchange() 清理交易所已不存在的持仓记录

        Args:
            symbol: 交易对
        """
        logger.debug(
            "条件单查询API已废弃，跳过每周期孤儿条件单清理，由第1/2层防护替代",
            symbol=symbol
        )
