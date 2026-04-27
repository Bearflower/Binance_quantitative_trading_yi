#!/usr/bin/env python3
"""
交易频率控制器 - v6.12 规范实现

实现以下频率控制机制：
1. 每日最大总交易数：4 笔（所有等级合计）
2. 单品种每日最大交易数：2 笔
3. 同品种冷却期：12 小时
4. 连续亏损暂停：连续 5 笔亏损暂停 1 天
5. 每日最大亏损限额：5% 总资金（25U）

基于 v6.12 规范文档：500U 合约交易规范_v6.12.md - 3.7 频率控制

版本: v2.0.0 (重构版 - 使用服务基类)
更新时间: 2026-04-27
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple

from services.base import BaseService, service_method
from models.database import get_db_manager


class FrequencyController(BaseService):
    """
    交易频率控制器

    继承自 BaseService，提供统一的频率控制功能。

    功能：
    1. 每日交易次数限制
    2. 单品种交易次数限制
    3. 冷却期管理
    4. 连续亏损暂停
    5. 每日亏损限额
    """

    def __init__(self, db_manager=None, **kwargs):
        """
        初始化频率控制器

        Args:
            db_manager: 数据库管理器实例
            **kwargs: 传递给 BaseService 的参数
        """
        self.db = db_manager
        super().__init__(service_name="FrequencyController", **kwargs)

    def _initialize(self):
        """
        初始化频率控制器

        加载配置参数，初始化数据库连接
        """
        # 从配置加载参数
        self.max_trades_per_day = self.get_config_value(
            'frequency_control.max_trades_per_day',
            default=4,
            required=True
        )

        self.max_trades_per_symbol_per_day = self.get_config_value(
            'frequency_control.max_trades_per_symbol_per_day',
            default=2,
            required=True
        )

        self.cooldown_hours = self.get_config_value(
            'frequency_control.cooldown_hours',
            default=12,
            required=True
        )

        self.max_consecutive_losses = self.get_config_value(
            'frequency_control.max_consecutive_losses',
            default=5,
            required=True
        )

        self.max_daily_loss_amount = Decimal(str(self.get_config_value(
            'frequency_control.max_daily_loss_amount',
            default=25,
            required=True
        )))

        self.total_capital = Decimal(str(self.get_config_value(
            'account.total_capital',
            default=500,
            required=True
        )))

        # 初始化数据库连接
        if self.db is None:
            self.db = get_db_manager()

        # 记录初始化信息
        self.log_info("=" * 60)
        self.log_info("频率控制器 v6.12 初始化完成")
        self.log_info("=" * 60)
        self.log_info(f"✅ 每日最大总交易数：{self.max_trades_per_day} 笔")
        self.log_info(f"✅ 单品种每日最大交易数：{self.max_trades_per_symbol_per_day} 笔")
        self.log_info(f"✅ 同品种冷却期：{self.cooldown_hours} 小时")
        self.log_info(f"✅ 连续亏损暂停：{self.max_consecutive_losses} 笔")
        self.log_info(f"✅ 每日最大亏损限额：{self.max_daily_loss_amount}U")
        self.log_info("=" * 60)

    @service_method()
    def check_trade_allowed(self, symbol: str) -> Tuple[bool, str]:
        """
        检查是否允许开仓（执行交易前的综合检查）

        Args:
            symbol: 交易对（如 BTCUSDT）

        Returns:
            (是否允许交易，原因说明)
        """
        # 1. 检查每日总交易数
        daily_total = self._get_daily_total_trades()
        if daily_total >= self.max_trades_per_day:
            self.log_warning(f"⛔ 频率控制：当日已达交易上限（{daily_total}/{self.max_trades_per_day}）")
            return False, f"当日已达交易上限（{daily_total}/{self.max_trades_per_day}）"

        # 2. 检查单品种每日交易数
        symbol_daily = self._get_symbol_daily_trades(symbol)
        if symbol_daily >= self.max_trades_per_symbol_per_day:
            self.log_warning(f"⛔ 频率控制：{symbol} 当日已达交易上限（{symbol_daily}/{self.max_trades_per_symbol_per_day}）")
            return False, f"{symbol} 当日已达交易上限"

        # 3. 检查冷却期
        in_cooldown, cooldown_end = self._check_cooldown(symbol)
        if in_cooldown:
            self.log_warning(f"⛔ 频率控制：{symbol} 处于冷却期，直到 {cooldown_end.strftime('%H:%M')}")
            return False, f"{symbol} 处于冷却期（{cooldown_end.strftime('%H:%M')} 结束）"

        # 4. 检查连续亏损
        consecutive_losses = self._get_consecutive_losses()
        if consecutive_losses >= self.max_consecutive_losses:
            self.log_warning(f"⛔ 频率控制：连续亏损 {consecutive_losses} 笔，暂停交易 1 天")
            return False, f"连续亏损 {consecutive_losses} 笔，暂停交易"

        # 5. 检查每日亏损限额
        daily_pnl = self._get_daily_pnl()
        if daily_pnl < -self.max_daily_loss_amount:
            self.log_warning(f"⛔ 频率控制：当日亏损 {abs(daily_pnl):.2f}U，超过限额 {self.max_daily_loss_amount}U")
            return False, f"当日亏损超限（{abs(daily_pnl):.2f}U/{self.max_daily_loss_amount}U）"

        self.log_info(f"✅ 频率控制检查通过：{symbol} (今日 {daily_total}/{self.max_trades_per_day} 笔)")
        return True, "频率控制检查通过"

    @service_method()
    def record_trade(self, symbol: str, trade_time: datetime,
                     pnl: Decimal = Decimal('0'), direction: str = ''):
        """
        记录交易（用于频率跟踪）

        Args:
            symbol: 交易对
            trade_time: 交易时间
            pnl: 盈亏金额（正数盈利，负数亏损）
            direction: 方向（'多'/'空'）
        """
        try:
            # 插入交易记录到数据库
            query = """
                INSERT INTO trade_records
                (symbol, direction, open_time, pnl, status, created_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """
            self.db._execute_query(
                query,
                (symbol, direction, trade_time, pnl, 'CLOSED')
            )

            self.log_info(f"✅ 交易记录已保存：{symbol} {direction} (盈亏：{pnl:.2f}U)")

        except Exception as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'record_trade'})

    @service_method()
    def get_trade_stats(self) -> Dict[str, Any]:
        """
        获取交易统计信息

        Returns:
            统计信息字典
        """
        today = datetime.now().date()

        # 查询今日交易数
        daily_total = self._get_daily_total_trades()

        # 查询连续亏损
        consecutive_losses = self._get_consecutive_losses()

        # 查询每日盈亏
        daily_pnl = self._get_daily_pnl()

        # 查询各币种今日交易数
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        symbol_trades = {}
        for symbol in symbols:
            symbol_trades[symbol] = self._get_symbol_daily_trades(symbol)

        return {
            'date': today.isoformat(),
            'daily_total': daily_total,
            'daily_max': self.max_trades_per_day,
            'daily_pnl': float(daily_pnl),
            'daily_pnl_limit': float(self.max_daily_loss_amount),
            'consecutive_losses': consecutive_losses,
            'max_consecutive_losses': self.max_consecutive_losses,
            'symbol_trades': symbol_trades,
            'is_paused': consecutive_losses >= self.max_consecutive_losses,
            'daily_limit_reached': daily_pnl < -self.max_daily_loss_amount
        }

    def _get_daily_total_trades(self) -> int:
        """获取今日总交易数"""
        try:
            today = datetime.now().date()
            query = """
                SELECT COUNT(*) as count
                FROM trade_records
                WHERE DATE(open_time) = %s AND status = 'CLOSED'
            """
            result = self.db._execute_one(query, (today,))
            return result['count'] if result else 0
        except Exception as e:
            self.handle_error(e, context={'operation': 'get_daily_total_trades'})
            return 0

    def _get_symbol_daily_trades(self, symbol: str) -> int:
        """获取指定交易对今日交易数"""
        try:
            today = datetime.now().date()
            query = """
                SELECT COUNT(*) as count
                FROM trade_records
                WHERE symbol = %s AND DATE(open_time) = %s AND status = 'CLOSED'
            """
            result = self.db._execute_one(query, (symbol, today))
            return result['count'] if result else 0
        except Exception as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'get_symbol_daily_trades'})
            return 0

    def _check_cooldown(self, symbol: str) -> Tuple[bool, Optional[datetime]]:
        """
        检查冷却期

        Args:
            symbol: 交易对

        Returns:
            (是否在冷却期，冷却期结束时间)
        """
        try:
            # 查询该交易对最后一次交易时间
            query = """
                SELECT open_time
                FROM trade_records
                WHERE symbol = %s AND status = 'CLOSED'
                ORDER BY open_time DESC
                LIMIT 1
            """
            result = self.db._execute_one(query, (symbol,))

            if not result:
                return False, None  # 无交易记录，不在冷却期

            last_trade_time = result['open_time']
            cooldown_end = last_trade_time + timedelta(hours=self.cooldown_hours)

            if datetime.now() < cooldown_end:
                return True, cooldown_end  # 在冷却期
            else:
                return False, None  # 冷却期已过

        except Exception as e:
            self.handle_error(e, context={'symbol': symbol, 'operation': 'check_cooldown'})
            return False, None

    def _get_consecutive_losses(self) -> int:
        """获取连续亏损次数"""
        try:
            # 查询最近的交易记录（按时间倒序）
            query = """
                SELECT pnl
                FROM trade_records
                WHERE status = 'CLOSED'
                ORDER BY open_time DESC
                LIMIT 10
            """
            results = self.db._execute_query(query)

            if not results:
                return 0

            # 统计连续亏损次数
            consecutive = 0
            for record in results:
                pnl = Decimal(str(record['pnl'])) if record['pnl'] else Decimal('0')
                if pnl < 0:
                    consecutive += 1
                else:
                    break  # 遇到盈利，中断连续亏损

            return consecutive

        except Exception as e:
            self.handle_error(e, context={'operation': 'get_consecutive_losses'})
            return 0

    def _get_daily_pnl(self) -> Decimal:
        """获取每日盈亏"""
        try:
            today = datetime.now().date()
            query = """
                SELECT COALESCE(SUM(pnl), 0) as total_pnl
                FROM trade_records
                WHERE DATE(open_time) = %s AND status = 'CLOSED'
            """
            result = self.db._execute_one(query, (today,))
            return Decimal(str(result['total_pnl'])) if result else Decimal('0')
        except Exception as e:
            self.handle_error(e, context={'operation': 'get_daily_pnl'})
            return Decimal('0')

    def reset_daily_stats(self):
        """重置每日统计（每日凌晨执行）"""
        self.log_info("🔄 重置每日交易统计")
        # 数据库自动按日期统计，无需手动重置
        pass


# 全局实例
_global_controller: Optional[FrequencyController] = None


def get_frequency_controller(db_manager=None, **kwargs) -> FrequencyController:
    """
    获取频率控制器实例（单例模式）

    Args:
        db_manager: 数据库管理器实例
        **kwargs: 传递给 FrequencyController 的参数

    Returns:
        FrequencyController 实例
    """
    global _global_controller
    if _global_controller is None:
        _global_controller = FrequencyController(db_manager, **kwargs)
    return _global_controller
