"""
BTC/ETH策略逻辑
基于评分引擎的趋势跟踪策略
"""
import asyncio
from typing import Dict, Optional, Tuple
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
import structlog

from shared.binance_api import BinanceClient, BinanceAPIError
from shared.kline_service import KLineService
from shared.notification import NotificationClient
from shared.indicators import TechnicalIndicators
from shared.dynamic_atr_filter import DynamicATRFilter
from shared.condition_orders import record_condition_order, get_open_orders
from shared.trade_logger import TradeLogger
from strategies.btc_eth.market_state import (
    get_market_state,
    get_market_state_behavior,
    MarketState
)


logger = structlog.get_logger()


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
            return False, f"单周亏损已达{weekly_loss_ratio*100:.0f}%阈值，暂停{pause_days}天，剩余{remaining.days}天{remaining.seconds // 3600}小时"
        
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
        atr_config = self.risk_config.get('dynamic_atr', {})
        if atr_config.get('enabled', True):
            self.atr_filter = DynamicATRFilter(atr_config)
        else:
            self.atr_filter = None
        
        # 持仓状态管理
        self.positions: Dict[str, PositionState] = {}
        
        # 交易对精度信息缓存
        self.symbol_precision: Dict[str, Dict] = {}
        
        # 主循环计数（v6.23.1：用于条件单重试间隔控制）
        self._cycle_count: int = 0
        
        # 条件单取消操作锁（v6.23.1：防止异步路径和主循环路径并发修改）
        self._cancel_lock = asyncio.Lock()
        
        # 最小持仓量阈值（v6.23.1：从配置读取，禁止硬编码）
        self.min_position_amt = float(
            config.get('strategy', {}).get('position_sync', {}).get('min_position_amt', 0.00001)
        )
        
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
            
            # 1.2 亏损时禁止加仓检查（v6.16.10）
            if symbol in self.positions:
                position = self.positions[symbol]
                if position.current_quantity > 0:
                    current_price = await self._get_current_price(symbol)
                    if current_price is not None:
                        if position.direction == 'LONG' and current_price < position.entry_price:
                            logger.info(
                                f"{symbol} 已有做多持仓且浮亏，禁止加仓",
                                entry_price=float(position.entry_price),
                                current_price=float(current_price)
                            )
                            analysis_result['reason'] = "已有做多持仓浮亏，禁止加仓"
                            return analysis_result
                        elif position.direction == 'SHORT' and current_price > position.entry_price:
                            logger.info(
                                f"{symbol} 已有做空持仓且浮亏，禁止加仓",
                                entry_price=float(position.entry_price),
                                current_price=float(current_price)
                            )
                            analysis_result['reason'] = "已有做空持仓浮亏，禁止加仓"
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
            indicators = {}
            for timeframe, data in klines.items():
                df = pd.DataFrame(data)
                df['open'] = pd.to_numeric(df['open'], errors='coerce')
                df['high'] = pd.to_numeric(df['high'], errors='coerce')
                df['low'] = pd.to_numeric(df['low'], errors='coerce')
                df['close'] = pd.to_numeric(df['close'], errors='coerce')
                df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
                
                indicators[timeframe] = TechnicalIndicators.calculate_all(df)
            
            # 3.5 市场状态识别（v6.18 激进收紧版）
            market_state_config = self.risk_config.get('market_state', {})
            market_state = None
            if market_state_config.get('enabled', True) and '4h' in klines:
                # 提取4h收盘价用于计算价格变化
                close_4h = pd.Series([float(k['close']) for k in klines['4h']])
                market_state, state_desc = get_market_state(
                    indicators['4h'], close_prices=close_4h, config=market_state_config
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
            market_max_trades = market_behavior.get('max_daily_trades', 999)
            today_str = current_time.date().isoformat()
            if today_str not in self.frequency_controller.daily_trades:
                self.frequency_controller.daily_trades[today_str] = 0
            if self.frequency_controller.daily_trades[today_str] >= market_max_trades:
                logger.info(
                    f"{symbol} 市场状态{market_state_name}已达每日最大交易数{market_max_trades}笔"
                )
                analysis_result['reason'] = f"频率限制: {market_state_name}已达每日最大交易数{market_max_trades}笔"
                return analysis_result
            
            # 3.7 v6.22.1：市场状态特定冷却期检查（从 can_trade() 移至此处）
            # 趋势市冷却期: 72h，震荡市冷却期: 3h
            if symbol in self.frequency_controller.symbol_last_trade_time:
                cooldown_hours = market_behavior.get('ranging_symbol_cooldown_hours', 72)
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
            
            # 10.0 应用市场状态仓位乘数（v6.17）
            position_ratio_mult = Decimal(str(market_behavior.get('position_ratio_mult', 1.0)))
            if position_ratio_mult != Decimal('1'):
                position_size_usdt = position_size_usdt * position_ratio_mult
                logger.info(f"{symbol} 市场状态仓位调整: ×{float(position_ratio_mult)}")
            
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
                sl_atr_mult = self.risk_config['stop_loss_atr_multiplier']
            
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
                'tp1_price': self._calculate_tp_price(current_price, atr, direction, 1),
                'tp2_price': self._calculate_tp_price(current_price, atr, direction, 2),
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
    
    def _check_ranging_entry(
        self,
        symbol: str,
        indicators: Dict,
        klines: Dict
    ) -> Tuple[bool, str]:
        """
        震荡市入场条件检查（v6.22 新增）
        
        震荡市使用反转信号入场，至少满足以下条件之一：
        1. BB触轨：价格触及布林带上轨（做空）或下轨（做多）
        2. RSI极端：RSI超买做空（>80），超卖做多（<20）
        3. 反转K线形态：吞没形态
        
        取多数条件一致的方向入场，若方向不一致则拒绝。
        
        Args:
            symbol: 交易对
            indicators: 技术指标字典
            klines: K线数据字典
        
        Returns:
            (是否通过, 方向或失败原因)
        """
        ranging_config = self.risk_config.get('ranging_strategy', {})
        if not ranging_config.get('enabled', True):
            return False, "震荡市策略未启用"
        
        try:
            entry_conditions = ranging_config.get('entry_conditions', {})
            if '4h' not in indicators or '4h' not in klines:
                return False, "4h数据缺失"
            
            df_4h = pd.DataFrame(klines['4h'])
            close_4h = float(df_4h['close'].iloc[-1])
            long_votes = 0
            short_votes = 0
            conditions_met = []
            
            # === 条件1: BB触轨 ===
            dist_to_lower = 1.0
            dist_to_upper = 1.0
            rsi = 0
            if entry_conditions.get('bb_touch', True):
                bb_upper = float(indicators['4h']['BB_Upper'].iloc[-1])
                bb_lower = float(indicators['4h']['BB_Lower'].iloc[-1])
                bb_range = bb_upper - bb_lower
                if bb_range > 0:
                    threshold = entry_conditions.get('bb_touch_threshold', 0.05)
                    dist_to_lower = (close_4h - bb_lower) / bb_range
                    dist_to_upper = (bb_upper - close_4h) / bb_range
                    
                    if dist_to_lower <= threshold:
                        long_votes += 1
                        conditions_met.append(f"BB下轨触轨({dist_to_lower*100:.1f}%)")
                    if dist_to_upper <= threshold:
                        short_votes += 1
                        conditions_met.append(f"BB上轨触轨({dist_to_upper*100:.1f}%)")
            
            # === 条件2: RSI极端 ===
            if entry_conditions.get('rsi_extreme', True):
                rsi = float(indicators['4h']['RSI'].iloc[-1])
                if pd.notna(rsi):
                    oversold = entry_conditions.get('rsi_oversold', 20)
                    overbought = entry_conditions.get('rsi_overbought', 80)
                    if rsi < oversold:
                        long_votes += 1
                        conditions_met.append(f"RSI超卖({rsi:.1f}<{oversold})")
                    if rsi > overbought:
                        short_votes += 1
                        conditions_met.append(f"RSI超买({rsi:.1f}>{overbought})")
            
            # === 条件3: 反转K线形态 ===
            if entry_conditions.get('reversal_pattern', True):
                if len(df_4h) >= 2:
                    prev_open = float(df_4h['open'].iloc[-2])
                    prev_close = float(df_4h['close'].iloc[-2])
                    curr_open = float(df_4h['open'].iloc[-1])
                    curr_close = float(df_4h['close'].iloc[-1])
                    
                    # 看涨吞没：前阴后阳，后实体包住前实体
                    if (prev_close < prev_open and
                        curr_close > curr_open and
                        curr_open <= prev_close and
                        curr_close >= prev_open):
                        long_votes += 1
                        conditions_met.append("看涨吞没")
                    
                    # 看跌吞没：前阳后阴，后实体包住前实体
                    if (prev_close > prev_open and
                        curr_close < curr_open and
                        curr_open >= prev_close and
                        curr_close <= prev_open):
                        short_votes += 1
                        conditions_met.append("看跌吞没")
            
            # === 判定方向 ===
            if long_votes == 0 and short_votes == 0:
                # 构建条件禁用/实际值的友好提示
                bb_info = (
                    f"BB距下轨{dist_to_lower*100:.1f}%/距上轨{dist_to_upper*100:.1f}%"
                    if entry_conditions.get('bb_touch', True)
                    else "BB=禁用"
                )
                rsi_info = (
                    f"RSI={rsi:.1f}"
                    if entry_conditions.get('rsi_extreme', True)
                    else "RSI=禁用"
                )
                return False, f"无震荡入场条件满足({bb_info}, {rsi_info})"
            
            if long_votes > short_votes:
                direction = 'LONG'
            elif short_votes > long_votes:
                direction = 'SHORT'
            else:
                return False, f"方向不一致(long={long_votes}, short={short_votes})"
            
            logger.info(
                f"{symbol} 震荡入场通过",
                direction=direction,
                conditions=conditions_met,
                long_votes=long_votes,
                short_votes=short_votes
            )
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
            # 获取账户余额
            account_info = await self.binance.get_account_info()
            available_balance = Decimal(str(account_info['availableBalance']))
            
            # 获取配置
            position_sizing_config = self.risk_config['position_sizing']
            safety_margin_ratio = Decimal(str(position_sizing_config['safety_margin_ratio']))
            min_margin = Decimal(str(position_sizing_config['min_margin_usdt']))
            max_position = Decimal(str(position_sizing_config['max_single_position_usdt']))
            
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
            
            # 计算可用资金（扣除安全垫）
            usable_balance = available_balance * (Decimal('1') - safety_margin_ratio)
            
            # 检查最小保证金
            if usable_balance < min_margin:
                reason = f"可用资金不足({float(usable_balance):.2f}U < {float(min_margin)}U)"
                logger.warning(reason, available_balance=available_balance)
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
            
            # 计算仓位大小
            position_size = usable_balance * position_ratio
            
            # 限制最大仓位
            position_size = min(position_size, max_position)
            
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
                available_balance=float(available_balance),
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
        tp_level: int
    ) -> Decimal:
        """
        计算止盈价格
        
        Args:
            entry_price: 入场价格
            atr: ATR值
            direction: 方向
            tp_level: 止盈级别（1或2）
        
        Returns:
            止盈价格
        """
        partial_config = self.risk_config['partial_take_profit']
        
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
    
    async def execute_signal(self, signal: Dict) -> bool:
        """
        执行交易信号
        
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
            
            # 记录交易（频率控制）
            await self.frequency_controller.record_trade(
                symbol,
                signal['timestamp']
            )
            
            # 1. 设置杠杆倍数
            logger.info(f"{symbol} 设置杠杆倍数: {signal['leverage']}")
            await self.binance.set_leverage(symbol, signal['leverage'])
            
            # 2. 确定开仓方向
            entry_side = "BUY" if signal['direction'] == "LONG" else "SELL"
            
            # 3. 下限价单开仓
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
            
            # 4. 下止损单（使用止损限价单 STOP）
            stop_side = "SELL" if signal['direction'] == "LONG" else "BUY"
            
            # 计算止损限价：触发价向不利方向偏移，确保成交
            stop_offset_pct = Decimal(str(self.risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
            # 确保 initial_stop_loss 为 Decimal 类型
            initial_stop = Decimal(str(signal['initial_stop_loss']))
            if signal['direction'] == 'LONG':
                stop_limit_price = initial_stop * (Decimal('1') - stop_offset_pct)
            else:
                stop_limit_price = initial_stop * (Decimal('1') + stop_offset_pct)
            
            logger.info(
                f"{symbol} 下止损限价单",
                stop_side=stop_side,
                stop_price=float(signal['initial_stop_loss']),
                limit_price=float(stop_limit_price),
                quantity=float(signal['quantity'])
            )
            
            stop_loss_order = await self.binance.place_conditional_order(
                symbol=symbol,
                side=stop_side,
                stop_price=initial_stop,
                price=stop_limit_price,
                quantity=signal['quantity'],
                order_type="STOP",
                reduce_only=True
            )
            
            # 统一账户条件单返回algoId，普通账户返回orderId
            stop_loss_order_id = stop_loss_order.get('algoId') or stop_loss_order.get('orderId')
            logger.info(
                f"{symbol} 止损单已下单",
                order_id=stop_loss_order_id,
                response=stop_loss_order
            )

            # 记录止损条件单到数据库（用于孤儿单清理）
            if stop_loss_order_id and self.db_manager:
                if stop_loss_order.get('algoId'):
                    await record_condition_order(
                        self.db_manager, "btc_eth", symbol,
                        algo_id=stop_loss_order['algoId'],
                        order_type="STOP_LOSS"
                    )
                else:
                    await record_condition_order(
                        self.db_manager, "btc_eth", symbol,
                        order_id=stop_loss_order_id,
                        order_type="STOP_LOSS"
                    )

            # 5. 下TP1止盈单（使用止盈限价单 TAKE_PROFIT）
            # 计算止盈限价：触发价向不利方向偏移，确保成交
            tp_offset_pct = Decimal(str(self.risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))
            initial_tp1 = Decimal(str(signal['tp1_price']))
            if signal['direction'] == 'LONG':
                tp_limit_price = initial_tp1 * (Decimal('1') - tp_offset_pct)
            else:
                tp_limit_price = initial_tp1 * (Decimal('1') + tp_offset_pct)
            
            logger.info(
                f"{symbol} 下TP1止盈限价单",
                tp_side=stop_side,  # 止盈方向与止损方向相同
                tp_price=float(signal['tp1_price']),
                limit_price=float(tp_limit_price),
                quantity=float(signal['quantity'])
            )
            
            tp1_order = await self.binance.place_conditional_order(
                symbol=symbol,
                side=stop_side,
                stop_price=initial_tp1,
                price=tp_limit_price,
                quantity=signal['quantity'],
                order_type="TAKE_PROFIT",
                reduce_only=True
            )
            
            # 统一账户条件单返回algoId，普通账户返回orderId
            tp1_order_id = tp1_order.get('algoId') or tp1_order.get('orderId')
            logger.info(
                f"{symbol} TP1止盈单已下单",
                order_id=tp1_order_id,
                response=tp1_order
            )

            # 记录 TP1 止盈条件单到数据库（用于孤儿单清理）
            if tp1_order_id and self.db_manager:
                if tp1_order.get('algoId'):
                    await record_condition_order(
                        self.db_manager, "btc_eth", symbol,
                        algo_id=tp1_order['algoId'],
                        order_type="TAKE_PROFIT"
                    )
                else:
                    await record_condition_order(
                        self.db_manager, "btc_eth", symbol,
                        order_id=tp1_order_id,
                        order_type="TAKE_PROFIT"
                    )

            # 6. 初始化持仓状态
            position = PositionState()
            position.entry_price = signal['entry_price']
            position.entry_time = signal['timestamp']
            position.direction = signal['direction']
            position.initial_quantity = signal['quantity']
            position.current_quantity = signal['quantity']
            position.atr = signal['atr']
            
            # 记录订单ID
            position.entry_order_id = entry_order_id
            position.stop_loss_order_id = stop_loss_order_id
            position.tp1_order_id = tp1_order_id
            
            # 保存持仓状态
            self.positions[symbol] = position
            
            # 7. 交易通知已禁用（不再发送飞书通知）
            # await self.notification.send_trade_notification(
            #     strategy="btc_eth",
            #     symbol=symbol,
            #     action=signal['direction'],
            #     quantity=float(signal['quantity']),
            #     price=float(signal['entry_price']),
            #     grade=signal['grade'],
            #     score=signal['score'],
            #     stop_loss=float(signal['initial_stop_loss']),
            #     take_profit=float(signal['tp1_price']),
            #     leverage=signal['leverage']
            # )
            
            logger.info(
                f"交易信号执行完成: {symbol}",
                entry_order_id=entry_order_id,
                stop_loss_order_id=stop_loss_order_id,
                tp1_order_id=tp1_order_id
            )
            return True
            
        except Exception as e:
            logger.error(
                f"执行交易信号失败: {symbol}",
                error=str(e),
                exc_info=True
            )
            
            # 发送错误通知
            try:
                await self.notification.send_error_notification(
                    strategy="btc_eth",
                    error_type="SIGNAL_EXECUTION_FAILED",
                    error_message=f"{symbol} 信号执行失败: {str(e)}",
                    context={
                        'symbol': symbol,
                        'direction': signal.get('direction'),
                        'grade': signal.get('grade'),
                        'score': signal.get('score')
                    }
                )
            except Exception as notify_error:
                logger.error(
                    f"{symbol} 发送错误通知失败",
                    error=str(notify_error)
                )
            
            return False
    
    async def _check_extreme_market(
        self, 
        symbol: str, 
        position: PositionState, 
        current_price: Decimal
    ) -> bool:
        """
        v6.16.10 极端行情处理
        
        瞬间反向5% → 平仓50%，止损收紧至1.0×ATR
        """
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
            
            # 收紧止损至1.0×ATR
            tighten_atr = Decimal(str(config.get('tighten_stop_atr', 1.0)))
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
            new_stop_order = await self.binance.place_conditional_order(
                symbol, 'SELL' if position.direction == 'LONG' else 'BUY',
                new_stop, position.current_quantity, 'STOP', price=stop_limit_price,
                reduce_only=True
            )
            position.stop_loss_order_id = new_stop_order.get('algoId') or new_stop_order.get('orderId')
            logger.info(f"{symbol} 极端行情止损已收紧至{float(new_stop):.4f}")
            return True
        
        return False
    
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
                    if retry_attempt < max_retries:
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
                        strategy="btc_eth",
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
                    strategy="btc_eth",
                    error_message=f"平仓失败: {close_reason} - {str(e)}",
                    symbol=symbol
                )
            except Exception as notify_error:
                logger.error(
                    f"{symbol} 发送错误通知失败",
                    error=str(notify_error)
                )
            
            return False
    
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
        partial_config = self.risk_config['partial_take_profit']
        
        # 检查TP1
        if not position.tp1_hit:
            tp1_price = self._calculate_tp_price(
                position.entry_price,
                position.atr,
                position.direction,
                1
            )
            
            hit = (
                (position.direction == 'LONG' and current_price >= tp1_price) or
                (position.direction == 'SHORT' and current_price <= tp1_price)
            )
            
            if hit:
                # 平仓25%
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
                2
            )
            
            hit = (
                (position.direction == 'LONG' and current_price >= tp2_price) or
                (position.direction == 'SHORT' and current_price <= tp2_price)
            )
            
            if hit:
                # 平仓25%
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
    
    async def _check_dynamic_trailing(
        self,
        symbol: str,
        position: PositionState,
        current_price: Decimal
    ):
        """
        检查并执行动态利润保护
        
        功能：
        1. 计算动态止损价
        2. 如果当前价已突破止损价 → 直接平仓（峰值回落保护）
        3. 如果止损价改善 → 同步到交易所条件单（取消旧单，创建新单）
           - 交易所自动触发止损，无需每周期监控价格
           - 首次激活时，取消原有硬止损单
        
        Args:
            symbol: 交易对
            position: 持仓状态
            current_price: 当前价格
        """
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
            # 平仓前取消交易所上的移动止损条件单（如果有）
            if position.trailing_stop_order_id is not None:
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
            
            logger.info(
                f"{symbol} 触发动态利润保护止损",
                current_price=float(current_price),
                trailing_stop=float(trailing_stop),
                unrealized_pnl_pct=position.pending_profit_pct,
                close_quantity=float(position.current_quantity)
            )
            
            await self._close_position(
                symbol=symbol,
                position=position,
                close_quantity=position.current_quantity,
                close_reason="TRAILING_STOP",
                current_price=current_price
            )
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
        dt_config = self.risk_config.get('dynamic_trailing', {})
        if not dt_config.get('enabled', True):
            return None
        
        activation_config = dt_config['activation']
        tiers = dt_config['regression_tiers']
        
        # 基于最高/最低价计算浮盈百分比（而非当前价）
        # 设计依据：用峰值计算允许回撤，才能在价格回落时锁住利润
        if position.direction == 'LONG':
            if position.entry_price is None or position.entry_price <= 0:
                return None
            # 取最高价作为参考价（若无历史最高价，回退到当前价）
            reference_price = position.highest_price if position.highest_price and position.highest_price > position.entry_price else current_price
            unrealized_pnl_pct = float((reference_price - position.entry_price) / position.entry_price) * 100
            if unrealized_pnl_pct < 0:
                unrealized_pnl_pct = 0.0  # 浮亏不计入
        else:
            if position.entry_price is None or position.entry_price <= 0:
                return None
            # 取最低价作为参考价（若无历史最低价，回退到当前价）
            reference_price = position.lowest_price if position.lowest_price and position.lowest_price > 0 and position.lowest_price < position.entry_price else current_price
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
        hard_stop_mult = Decimal(str(self.risk_config.get('stop_loss_atr_multiplier', 1.5)))
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
        首次激活时同时取消原有硬止损单（已被动态止损替代）。
        
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
        
        # 2. 首次激活时，取消原有硬止损单（已被动态止损替代）
        if position.stop_loss_order_id is not None:
            try:
                await self.binance.cancel_algo_order(symbol, position.stop_loss_order_id)
                logger.info(
                    f"{symbol} 硬止损单已取消（由动态止损替代）",
                    algo_id=position.stop_loss_order_id
                )
            except BinanceAPIError as e:
                if e.code in silent_error_codes:
                    logger.debug(
                        f"{symbol} 硬止损单取消失败（可能已成交）",
                        algo_id=position.stop_loss_order_id,
                        error_code=e.code
                    )
                else:
                    logger.warning(
                        f"{symbol} 取消硬止损单异常",
                        algo_id=position.stop_loss_order_id,
                        error_code=e.code,
                        error_msg=e.message
                    )
            except Exception as e:
                logger.warning(
                    f"{symbol} 取消硬止损单异常",
                    algo_id=position.stop_loss_order_id,
                    error=str(e)
                )
            position.stop_loss_order_id = None
        
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
        close_quantity = self._adjust_quantity_precision(position.current_quantity, step_size)
        
        # 4. 下新止损条件单
        logger.info(
            f"{symbol} 下移动止损条件单",
            stop_side=stop_side,
            stop_price=float(trailing_stop),
            limit_price=float(stop_limit_price),
            quantity=float(close_quantity)
        )
        
        try:
            new_order = await self.binance.place_conditional_order(
                symbol=symbol,
                side=stop_side,
                stop_price=trailing_stop,
                price=stop_limit_price,
                quantity=close_quantity,
                order_type="STOP",
                reduce_only=True
            )
            
            new_order_id = new_order.get('algoId') or new_order.get('orderId')
            position.trailing_stop_order_id = new_order_id
            
            logger.info(
                f"{symbol} 移动止损条件单已创建",
                order_id=new_order_id,
                trailing_stop=float(trailing_stop)
            )
            
            # 记录条件单到数据库（用于孤儿单清理追踪）
            if new_order_id and self.db_manager and new_order.get('algoId'):
                await record_condition_order(
                    self.db_manager, "btc_eth", symbol,
                    algo_id=new_order['algoId'],
                    order_type="STOP_LOSS"
                )
        except Exception as e:
            logger.error(
                f"{symbol} 创建移动止损条件单失败",
                error=str(e),
                exc_info=True
            )
    
    async def _check_time_stop(self, symbol: str, position: PositionState):
        """
        检查并执行时间止损
        
        Args:
            symbol: 交易对
            position: 持仓状态
        """
        # 只在未触发TP1且仍有持仓时检查时间止损
        if position.tp1_hit or position.current_quantity <= 0:
            return
        
        time_stop_config = self.risk_config['time_stop']
        max_holding_hours = time_stop_config['max_holding_hours']
        close_ratio = Decimal(str(time_stop_config['close_ratio']))
        
        # 计算持仓时间
        holding_time = datetime.now() - position.entry_time
        holding_hours = holding_time.total_seconds() / 3600
        
        if holding_hours >= max_holding_hours:
            # 平仓50%
            close_quantity = position.current_quantity * close_ratio
            
            logger.info(
                f"{symbol} 触发时间止损",
                holding_hours=holding_hours,
                close_quantity=float(close_quantity)
            )
            
            # 执行平仓
            success = await self._close_position(
                symbol=symbol,
                position=position,
                close_quantity=close_quantity,
                close_reason="TIME_STOP",
                current_price=None
            )
            
            if success:
                # 标记为已处理，避免重复触发
                position.tp1_hit = True  # 使用tp1_hit标记避免重复触发
            else:
                logger.error(f"{symbol} 时间止损平仓失败，保持持仓状态")
    
    async def _cleanup_position_orders(self, symbol: str, position: PositionState):
        """
        清理单个持仓的残余条件单（第一层防护辅助方法）
        
        取消该持仓关联的所有条件单（止损单、止盈单）和未成交入场限价单。
        每个取消操作独立用 try/except 包裹，单个失败不影响其他清理。
        取消失败时自动重试，并记录重试次数（v6.23 孤儿条件单修复）。
        
        Args:
            symbol: 交易对
            position: 持仓状态
        """
        silent_error_codes = set(self.risk_config['cleanup_silent_error_codes'])
        max_retry = self._get_cancel_retry_config('max_retries', 10)

        # 条件单取消通用处理逻辑
        async def _cancel_one(order_type: str, order_id: int, is_algo: bool = True):
            if order_id is None:
                return
            
            # 使用锁保护并发修改 cancel_retry_count 和 order_id（v6.23.1）
            async with self._cancel_lock:
                try:
                    if is_algo:
                        await self.binance.cancel_algo_order(symbol, order_id)
                    else:
                        await self.binance.cancel_order(symbol, order_id=str(order_id))
                    # 成功
                    logger.info(f"{symbol} {order_type}订单已取消", algo_id=order_id if is_algo else order_id)
                    setattr(position, f"{order_type}_order_id", None)
                    position.cancel_retry_count.pop(order_type, None)
                except BinanceAPIError as e:
                    if e.code in silent_error_codes:
                        # 订单不存在或已取消，视为已处理
                        setattr(position, f"{order_type}_order_id", None)
                        position.cancel_retry_count.pop(order_type, None)
                    elif e.code == -2021:
                        # 条件单已执行（触发后成交）
                        setattr(position, f"{order_type}_order_id", None)
                        position.cancel_retry_count.pop(order_type, None)
                        # 记录成交
                        await self._record_executed_conditional_order(symbol, order_type, order_id)
                    else:
                        # 可重试错误
                        current = position.cancel_retry_count.get(order_type, 0) + 1
                        position.cancel_retry_count[order_type] = current
                        if current >= max_retry:
                            setattr(position, f"{order_type}_order_id", None)
                            position.cancel_retry_count.pop(order_type, None)
                            await self._notify_cancel_timeout(symbol, order_type, order_id)
                        else:
                            logger.warning(f"{symbol} {order_type}取消失败，将重试", algo_id=order_id, retry_count=current, max_retries=max_retry, error_code=e.code)
                except Exception as e:
                    current = position.cancel_retry_count.get(order_type, 0) + 1
                    position.cancel_retry_count[order_type] = current
                    if current >= max_retry:
                        setattr(position, f"{order_type}_order_id", None)
                        position.cancel_retry_count.pop(order_type, None)
                        await self._notify_cancel_timeout(symbol, order_type, order_id)
                    else:
                        logger.warning(f"{symbol} {order_type}取消失败（未知异常），将重试", algo_id=order_id, retry_count=current, max_retries=max_retry, error=str(e))

        # 1. 止损条件单
        if position.stop_loss_order_id is not None:
            await _cancel_one("stop_loss", position.stop_loss_order_id, is_algo=True)
        
        # 2. 取消TP1止盈条件单
        if position.tp1_order_id is not None:
            await _cancel_one("tp1", position.tp1_order_id, is_algo=True)
        
        # 3. 取消TP2止盈条件单
        if position.tp2_order_id is not None:
            await _cancel_one("tp2", position.tp2_order_id, is_algo=True)
        
        # 4. 取消移动止损条件单
        if position.trailing_stop_order_id is not None:
            await _cancel_one("trailing_stop", position.trailing_stop_order_id, is_algo=True)
        
        # 5. 取消未成交的入场限价单
        if position.entry_order_id is not None:
            await _cancel_one("entry", position.entry_order_id, is_algo=False)
        
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
                strategy="btc_eth",
                error_message=f"条件单取消超时: {symbol} {order_type} (algo_id={algo_id})，已达最大重试次数，请人工核查",
                symbol=symbol
            )
        except Exception as e:
            logger.warning(f"发送取消超时告警失败", error=str(e))

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
            orphan_symbols = set(o['symbol'] for o in orphan_orders)
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
                        f"UPDATE condition_orders SET status='CANCELED', updated_at=NOW() WHERE algo_id IN ({placeholders}) AND status='OPEN'",
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
        """
        确保持仓有止损止盈保护单（v6.20.5）
        
        获取交易所当前持仓，检查已有持仓记录的条件单ID完整性。
        对缺少保护单的持仓自动补单（计算ATR、止损价、止盈价）。
        
        防重复创建（v6.20.5）：
          在创建新条件单前，先查询 condition_orders 表中该 symbol 是否已有 OPEN 的条件单。
          若已有，则跳过创建，避免容器重启后重复创建。
        """
        logger.info("开始持仓保护检查...")

        try:
            # 1. 获取交易所当前持仓
            exchange_positions = await self.binance.get_position()
        except Exception as e:
            logger.warning("获取交易所持仓失败，跳过持仓保护检查", error=str(e))
            return

        if not exchange_positions:
            logger.info("交易所无持仓，无需保护")
            return

        # 2. 查询 condition_orders 表中已有的 OPEN 条件单（防重复创建）
        existing_open_orders = {}
        try:
            if self.db_manager:
                orders = await get_open_orders(self.db_manager, "btc_eth")
                for o in orders:
                    sym = o.get('symbol')
                    if sym not in existing_open_orders:
                        existing_open_orders[sym] = {}
                    order_type = o.get('order_type')
                    existing_open_orders[sym][order_type] = o.get('algo_id')
                if existing_open_orders:
                    logger.info("检测到已有OPEN条件单，跳过重复创建", symbols=list(existing_open_orders.keys()))
        except Exception as e:
            logger.warning("查询 condition_orders 表失败，不影响后续创建", error=str(e))

        # 3. 只处理本策略管理的币种
        managed_symbols = set(self.symbols)

        # 获取止盈止损配置
        risk_config = self.risk_config or {}
        stop_offset_pct = Decimal(str(risk_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
        tp_offset_pct = Decimal(str(risk_config.get('tp_limit_order', {}).get('offset_pct', 0.0015)))
        stop_loss_atr = Decimal(str(risk_config.get('stop_loss_atr_multiplier', 1.0)))
        take_profit_atr = Decimal(str(risk_config.get('take_profit_atr_multiplier', 2.0)))

        for pos_data in exchange_positions:
            symbol = pos_data.get('symbol', '')
            position_amt = float(pos_data.get('positionAmt', 0))

            # 跳过非策略管理和无实际仓位的记录
            if symbol not in managed_symbols:
                continue
            if abs(position_amt) < self.min_position_amt:
                continue

            direction = 'LONG' if position_amt > 0 else 'SHORT'
            current_quantity = Decimal(str(abs(position_amt)))

            logger.info(
                f"{symbol} 检测到交易所持仓",
                direction=direction,
                quantity=float(current_quantity)
            )

            # 3. 检查该持仓是否已在 self.positions 中且有关联条件单
            existing_pos = self.positions.get(symbol)
            has_stop = existing_pos and existing_pos.stop_loss_order_id is not None
            has_tp = existing_pos and existing_pos.tp1_order_id is not None

            # 3.1 检查 condition_orders 表中是否已有 OPEN 条件单（防容器重启后重复创建）
            if not has_stop or not has_tp:
                existing_orders = existing_open_orders.get(symbol, {})
                if not has_stop and existing_orders.get('STOP_LOSS'):
                    has_stop = True
                    logger.info(
                        f"{symbol} 从 condition_orders 表检测到已有 STOP_LOSS",
                        algo_id=existing_orders['STOP_LOSS']
                    )
                if not has_tp and existing_orders.get('TAKE_PROFIT'):
                    has_tp = True
                    logger.info(
                        f"{symbol} 从 condition_orders 表检测到已有 TAKE_PROFIT",
                        algo_id=existing_orders['TAKE_PROFIT']
                    )

            if has_stop and has_tp:
                logger.info(
                    f"{symbol} 已有完整保护单，跳过",
                    stop_order_id=existing_pos.stop_loss_order_id if existing_pos else existing_orders.get('STOP_LOSS'),
                    tp_order_id=existing_pos.tp1_order_id if existing_pos else existing_orders.get('TAKE_PROFIT')
                )
                continue

            # 4. 获取当前价格和计算ATR
            current_price = await self._get_current_price(symbol)
            if current_price is None:
                logger.warning(f"{symbol} 获取当前价格失败，跳过保护")
                continue

            try:
                klines = await self.kline_service.get_klines(symbol, '1h', limit=60)
                if klines is not None and len(klines) > 20:
                    df = pd.DataFrame(klines)
                    indicators_data = TechnicalIndicators.calculate_all(df)
                    atr_series = indicators_data.get("ATR")
                    if atr_series is None or len(atr_series) == 0:
                        atr = current_price * Decimal('0.01')
                        logger.warning(f"{symbol} ATR数据为空，使用默认ATR(1%)")
                    else:
                        atr_value = atr_series.iloc[-1]
                        if pd.isna(atr_value) or abs(atr_value) > 1e30:
                            atr = current_price * Decimal('0.01')
                            logger.warning(f"{symbol} ATR值异常，使用默认ATR(1%)")
                        else:
                            atr = Decimal(str(float(atr_value)))
                else:
                    atr = current_price * Decimal('0.01')
                    logger.warning(f"{symbol} K线数据不足，使用默认ATR(1%)")
            except Exception as e:
                atr = current_price * Decimal('0.01')
                logger.warning(f"{symbol} ATR计算异常，使用默认ATR(1%)", error=str(e))

            # 5. 计算止损价和止盈价
            if direction == 'LONG':
                stop_price = current_price - atr * stop_loss_atr
                tp_price = current_price + atr * take_profit_atr
                # 止损限价：向不利方向偏移
                stop_limit_price = stop_price * (Decimal('1') - stop_offset_pct)
                # 止盈限价：向不利方向偏移
                tp1_limit_price = tp_price * (Decimal('1') - tp_offset_pct)
            else:  # SHORT
                stop_price = current_price + atr * stop_loss_atr
                tp_price = current_price - atr * take_profit_atr
                # 止损限价：向不利方向偏移
                stop_limit_price = stop_price * (Decimal('1') + stop_offset_pct)
                # 止盈限价：向不利方向偏移
                tp1_limit_price = tp_price * (Decimal('1') + tp_offset_pct)

            # 获取精度
            try:
                precision = await self._get_symbol_precision(symbol)
                tick_size = Decimal(str(precision.get('tick_size', '0.01')))
                step_size = Decimal(str(precision.get('step_size', '0.001')))
            except Exception:
                tick_size = Decimal('0.01')
                step_size = Decimal('0.001')

            # 精度调整
            stop_limit_price = self._adjust_price_precision(stop_limit_price, tick_size)
            tp1_limit_price = self._adjust_price_precision(tp1_limit_price, tick_size)
            close_quantity = self._adjust_quantity_precision(current_quantity, step_size)

            # 6. 下限价止损单
            if not has_stop:
                try:
                    stop_side = 'SELL' if direction == 'LONG' else 'BUY'
                    stop_order = await self.binance.place_conditional_order(
                        symbol=symbol,
                        side=stop_side,
                        quantity=close_quantity,
                        order_type="STOP",
                        stop_price=stop_price,
                        price=stop_limit_price,
                        reduce_only=True
                    )
                    algo_id = stop_order.get('algoId') or stop_order.get('orderId')
                    logger.info(
                        f"{symbol} 止损限价单已创建",
                        stop_price=float(stop_price),
                        limit_price=float(stop_limit_price),
                        algo_id=algo_id
                    )

                    # 记录条件单到数据库（用于孤儿单清理追踪）
                    if algo_id and self.db_manager:
                        await record_condition_order(
                            self.db_manager, "btc_eth", symbol,
                            algo_id=algo_id,
                            order_type="STOP_LOSS"
                        )

                    # 更新持仓状态
                    if existing_pos:
                        existing_pos.stop_loss_order_id = algo_id
                    else:
                        existing_pos = PositionState()
                        existing_pos.entry_price = current_price
                        existing_pos.entry_time = datetime.now()
                        existing_pos.direction = direction
                        existing_pos.initial_quantity = current_quantity
                        existing_pos.current_quantity = current_quantity
                        existing_pos.atr = atr
                        existing_pos.stop_loss_order_id = algo_id
                        self.positions[symbol] = existing_pos
                except Exception as e:
                    logger.warning(f"{symbol} 创建止损限价单失败", error=str(e))

            # 7. 下限价止盈单
            if not has_tp:
                try:
                    tp_side = 'SELL' if direction == 'LONG' else 'BUY'
                    tp_order = await self.binance.place_conditional_order(
                        symbol=symbol,
                        side=tp_side,
                        quantity=close_quantity,
                        order_type="TAKE_PROFIT",
                        stop_price=tp_price,
                        price=tp1_limit_price,
                        reduce_only=True
                    )
                    algo_id = tp_order.get('algoId') or tp_order.get('orderId')
                    logger.info(
                        f"{symbol} 止盈限价单已创建",
                        tp_price=float(tp_price),
                        limit_price=float(tp1_limit_price),
                        algo_id=algo_id
                    )

                    # 记录条件单到数据库（用于孤儿单清理追踪）
                    if algo_id and self.db_manager:
                        await record_condition_order(
                            self.db_manager, "btc_eth", symbol,
                            algo_id=algo_id,
                            order_type="TAKE_PROFIT"
                        )

                    # 更新持仓状态
                    if existing_pos:
                        existing_pos.tp1_order_id = algo_id
                    else:
                        existing_pos = PositionState()
                        existing_pos.entry_price = current_price
                        existing_pos.entry_time = datetime.now()
                        existing_pos.direction = direction
                        existing_pos.initial_quantity = current_quantity
                        existing_pos.current_quantity = current_quantity
                        existing_pos.atr = atr
                        existing_pos.tp1_order_id = algo_id
                        self.positions[symbol] = existing_pos
                except Exception as e:
                    logger.warning(f"{symbol} 创建止盈限价单失败", error=str(e))

        logger.info("持仓保护检查完成")

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
