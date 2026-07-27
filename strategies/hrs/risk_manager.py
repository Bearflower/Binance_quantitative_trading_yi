"""
风控模块
负责 HRS 策略的风险控制：连续亏损暂停、最大回撤熔断、黑名单、每日开仓限制等
"""
from typing import Dict, Any, Set, Optional, Callable
from datetime import datetime, timedelta, timezone
import threading
import structlog


logger = structlog.get_logger()


class RiskManager:
    """
    风控管理器

    功能：
    - 每笔最大亏损控制（账户总资金2%）
    - 单日最多开仓3个币种、同一方向最多2个
    - 连续亏损暂停（3笔后暂停2天）
    - 最大回撤熔断（累计亏损≥15%暂停一周）
    - 黑名单机制（止损后反向波动超过5%永久禁止）
    - P2-6: 单币种极端行情熔断（1小时涨跌超阈值暂停该币种）
    """

    def __init__(
        self,
        config: Dict[str, Any],
        notification_callback: Optional[Callable] = None,
        should_notify_callback: Optional[Callable] = None,
    ):
        """
        初始化风控管理器

        Args:
            config: 配置字典
            notification_callback: 可选的通知回调函数，签名为 async def callback(message, level, project)
            should_notify_callback: 可选的通知事件开关回调，签名为 should_notify(event_name) -> bool
        """
        self.config = config
        self._notification_callback = notification_callback
        self._should_notify = should_notify_callback
        trading_config = config.get("trading", {})

        # 亏损控制
        self.max_loss_percent = trading_config.get("max_loss_percent", 0.02)

        # 每日开仓限制
        self.max_daily_positions = trading_config.get("max_daily_positions", 3)
        self.max_daily_same_direction = trading_config.get("max_daily_same_direction", 2)

        # 连续亏损
        loss_config = trading_config.get("consecutive_loss", {})
        self.max_consecutive_losses = loss_config.get("max_count", 3)
        self.pause_days = loss_config.get("pause_days", 2)

        # 最大回撤
        drawdown_config = trading_config.get("max_drawdown", {})
        self.drawdown_threshold = drawdown_config.get("threshold", 0.15)
        self.drawdown_pause_days = drawdown_config.get("pause_days", 7)

        # 黑名单
        blacklist_config = trading_config.get("blacklist", {})
        self.blacklist_check_hours = blacklist_config.get("check_hours", 24)
        self.reverse_surge_percent = blacklist_config.get("reverse_surge_percent", 0.05)

        # P2-6: 极端行情熔断配置
        cb_config = config.get("circuit_breaker", {})
        self.cb_enabled = cb_config.get("enabled", True)
        self.cb_price_change_threshold = cb_config.get("price_change_threshold", 0.30)
        self.cb_duration_minutes = cb_config.get("duration_minutes", 60)

        # 状态
        self.consecutive_losses: int = 0
        self.pause_until: Optional[datetime] = None
        self.drawdown_pause_until: Optional[datetime] = None
        self.blacklist: Set[str] = set()
        self.daily_open_count: Dict[str, int] = {"short": 0, "long": 0}
        self._daily_reset_date: Optional[datetime] = None

        # 止损监控列表（用于黑名单检测）
        self._stop_loss_monitor: Dict[str, Dict[str, Any]] = {}

        # 止损监控定时器 {symbol: threading.Timer}
        self._monitor_timers: Dict[str, threading.Timer] = {}

        # P2-6: 极端行情熔断状态 {symbol: {"triggered_at": datetime, "expires_at": datetime}}
        self._circuit_breakers: Dict[str, Dict[str, Any]] = {}

        # 线程锁：保护 _stop_loss_monitor 和 _monitor_timers 的并发访问
        self._monitor_lock = threading.Lock()

        logger.info(
            "风控管理器初始化完成",
            max_daily_positions=self.max_daily_positions,
            max_daily_same_direction=self.max_daily_same_direction,
            max_consecutive_losses=self.max_consecutive_losses,
            drawdown_threshold=self.drawdown_threshold,
        )

    def _reset_daily_counters(self) -> None:
        """重置每日计数器"""
        now = datetime.now(timezone.utc)
        if self._daily_reset_date is None or now.date() != self._daily_reset_date.date():
            self.daily_open_count = {"short": 0, "long": 0}
            self._daily_reset_date = now
            logger.info("每日计数器已重置")

    def can_open_position(self, direction: str) -> bool:
        """
        检查是否可以开仓

        Args:
            direction: 方向 ('short' 或 'long')

        Returns:
            是否可以开仓
        """
        self._reset_daily_counters()

        # 检查暂停状态
        if self._check_pause():
            return False

        # 检查每日总量
        total_opened = self.daily_open_count.get("short", 0) + self.daily_open_count.get("long", 0)
        if total_opened >= self.max_daily_positions:
            logger.info("单日开仓数量已达上限", total=total_opened, max=self.max_daily_positions)
            return False

        # 检查同方向限制
        direction_count = self.daily_open_count.get(direction, 0)
        if direction_count >= self.max_daily_same_direction:
            logger.info(
                "单日同方向开仓数量已达上限",
                direction=direction,
                count=direction_count,
                max=self.max_daily_same_direction,
            )
            return False

        return True

    def record_open(self, direction: str) -> None:
        """
        记录开仓

        Args:
            direction: 方向
        """
        self.daily_open_count[direction] = self.daily_open_count.get(direction, 0) + 1
        logger.info("记录开仓", direction=direction, count=self.daily_open_count[direction])

    async def record_loss(self, symbol: str, entry_price: float, current_price: float) -> None:
        """
        记录亏损

        Args:
            symbol: 交易对
            entry_price: 入场价格
            current_price: 当前价格
        """
        self.consecutive_losses += 1
        logger.warning(
            "记录亏损",
            symbol=symbol,
            consecutive_losses=self.consecutive_losses,
            max_consecutive=self.max_consecutive_losses,
        )

        # 检查是否触发连续亏损暂停
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.pause_until = datetime.now(timezone.utc) + timedelta(days=self.pause_days)
            pause_duration = timedelta(days=self.pause_days)
            logger.warning(
                "触发连续亏损暂停",
                consecutive_losses=self.consecutive_losses,
                pause_until=self.pause_until.isoformat(),
                pause_duration_days=self.pause_days,
            )
            # P1-8: 发送暂停通知
            await self._send_pause_notification(
                reason=f"连续亏损{self.consecutive_losses}笔",
                pause_duration=pause_duration,
            )

        # 添加到止损监控
        with self._monitor_lock:
            self._stop_loss_monitor[symbol] = {
                "entry_price": entry_price,
                "monitor_until": (datetime.now(timezone.utc) + timedelta(hours=self.blacklist_check_hours)).isoformat(),
            }

    def record_profit(self) -> None:
        """记录盈利，重置连续亏损计数"""
        self.consecutive_losses = 0
        logger.info("盈利，重置连续亏损计数")

    async def check_drawdown(self, total_pnl: float, account_balance: float) -> bool:
        """
        检查最大回撤熔断

        Args:
            total_pnl: 累计盈亏
            account_balance: 账户总资金

        Returns:
            True: 正常，False: 触发熔断
        """
        if account_balance <= 0:
            return True

        drawdown = abs(total_pnl) / account_balance if total_pnl < 0 else 0
        if drawdown >= self.drawdown_threshold:
            self.drawdown_pause_until = datetime.now(timezone.utc) + timedelta(days=self.drawdown_pause_days)
            pause_duration = timedelta(days=self.drawdown_pause_days)
            logger.warning(
                "触发最大回撤熔断",
                drawdown=drawdown,
                threshold=self.drawdown_threshold,
                pause_until=self.drawdown_pause_until.isoformat(),
                pause_duration_days=self.drawdown_pause_days,
            )
            # P1-8: 发送熔断通知
            await self._send_pause_notification(
                reason=f"最大回撤熔断（累计亏损{drawdown:.1%}）",
                pause_duration=pause_duration,
            )
            return False

        return True

    def is_blacklisted(self, symbol: str) -> bool:
        """
        检查是否在黑名单中

        Args:
            symbol: 交易对

        Returns:
            是否在黑名单中
        """
        return symbol in self.blacklist

    def is_paused(self) -> bool:
        """
        P0-6: 检查策略是否处于暂停/熔断状态

        Returns:
            True: 暂停中（连续亏损暂停或回撤熔断）
        """
        now = datetime.now(timezone.utc)
        if self.pause_until and now < self.pause_until:
            return True
        if self.drawdown_pause_until and now < self.drawdown_pause_until:
            return True
        return False

    def add_to_blacklist(self, symbol: str, reason: str) -> None:
        """
        添加永久黑名单

        Args:
            symbol: 交易对
            reason: 原因
        """
        self.blacklist.add(symbol)
        logger.warning("添加永久黑名单", symbol=symbol, reason=reason)

    def check_blacklist_monitor(
        self,
        symbol: str,
        current_price: float
    ) -> bool:
        """
        检查止损监控列表，检测反向波动

        Args:
            symbol: 交易对
            current_price: 当前价格

        Returns:
            True: 正常，False: 触发黑名单
        """
        if symbol not in self._stop_loss_monitor:
            return True

        with self._monitor_lock:
            if symbol not in self._stop_loss_monitor:
                return True

            monitor = self._stop_loss_monitor[symbol]
            entry_price = monitor.get("entry_price", 0)

            if entry_price <= 0:
                return True

            price_change = (current_price - entry_price) / entry_price
            if abs(price_change) >= self.reverse_surge_percent:
                self.add_to_blacklist(symbol, f"止损后反向波动 {price_change * 100:.2f}%")
                del self._stop_loss_monitor[symbol]
                return False

            # 检查监控是否过期
            monitor_until_str = monitor.get("monitor_until", "")
            if monitor_until_str:
                monitor_until = datetime.fromisoformat(monitor_until_str)
                if datetime.now(timezone.utc) > monitor_until:
                    del self._stop_loss_monitor[symbol]
                    logger.info("止损监控过期，移除", symbol=symbol)

        return True

    def _check_pause(self) -> bool:
        """
        检查是否处于暂停状态

        Returns:
            True: 暂停中，False: 正常
        """
        now = datetime.now(timezone.utc)

        # 检查连续亏损暂停
        if self.pause_until:
            if now < self.pause_until:
                remaining = (self.pause_until - now).total_seconds() / 3600
                logger.info("连续亏损暂停中", remaining_hours=remaining)
                return True
            self.pause_until = None
            self.consecutive_losses = 0
            logger.info("连续亏损暂停已结束")

        # 检查回撤熔断
        if self.drawdown_pause_until:
            if now < self.drawdown_pause_until:
                remaining = (self.drawdown_pause_until - now).total_seconds() / 3600
                logger.info("回撤熔断暂停中", remaining_hours=remaining)
                return True
            self.drawdown_pause_until = None
            logger.info("回撤熔断已结束")

        return False

    def calculate_position_size(
        self,
        account_balance: float,
        stop_loss_percent: float,
        leverage: int,
        current_price: float
    ) -> float:
        """
        计算仓位大小

        公式：开仓价值 = 账户总资金 × 2% / 止损幅度

        Args:
            account_balance: 账户总资金
            stop_loss_percent: 止损幅度（小数）
            leverage: 杠杆倍数
            current_price: 当前价格

        Returns:
            仓位数量
        """
        max_loss = account_balance * self.max_loss_percent
        if stop_loss_percent <= 0:
            logger.warning("止损幅度无效", stop_loss_percent=stop_loss_percent)
            return 0.0
        position_value = max_loss / stop_loss_percent
        margin = position_value / leverage
        quantity = position_value / current_price if current_price > 0 else 0

        logger.info(
            "仓位计算",
            account_balance=account_balance,
            max_loss=max_loss,
            position_value=position_value,
            margin=margin,
            quantity=quantity,
        )

        return quantity

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于持久化）"""
        return {
            "consecutive_losses": self.consecutive_losses,
            "pause_until": self.pause_until.isoformat() if self.pause_until else None,
            "drawdown_pause_until": self.drawdown_pause_until.isoformat() if self.drawdown_pause_until else None,
            "blacklist": list(self.blacklist),
            "daily_open_count": self.daily_open_count,
            "stop_loss_monitor": self._stop_loss_monitor,
            # P2-6: 持久化熔断状态
            "circuit_breakers": {
                symbol: {
                    "triggered_at": cb["triggered_at"].isoformat(),
                    "expires_at": cb["expires_at"].isoformat(),
                    "price_change": cb["price_change"],
                }
                for symbol, cb in self._circuit_breakers.items()
            },
        }

    def from_dict(self, data: Dict[str, Any]) -> None:
        """
        从字典恢复状态

        P2-7: 恢复后重新计算 _stop_loss_monitor 中各币种的 monitor_until 时间：
        - 若已过期，立即从黑名单和监控列表中移除
        - 若未过期，计算剩余时间并设置定时器自动清理
        """
        # 先取消所有已有的定时器
        self._cancel_monitor_timers()

        self.consecutive_losses = data.get("consecutive_losses", 0)
        pu = data.get("pause_until")
        self.pause_until = datetime.fromisoformat(pu) if pu else None
        dpu = data.get("drawdown_pause_until")
        self.drawdown_pause_until = datetime.fromisoformat(dpu) if dpu else None
        self.blacklist = set(data.get("blacklist", []))
        self.daily_open_count = data.get("daily_open_count", {"short": 0, "long": 0})

        # P2-7: 恢复止损监控列表，重新计算过期时间并设置定时器
        raw_monitor = data.get("stop_loss_monitor", {})
        now = datetime.now(timezone.utc)
        with self._monitor_lock:
            self._stop_loss_monitor = {}
        expired_count = 0
        removed_from_blacklist = 0

        for symbol, monitor_data in raw_monitor.items():
            monitor_until_str = monitor_data.get("monitor_until", "")
            if monitor_until_str:
                try:
                    monitor_until = datetime.fromisoformat(monitor_until_str)
                    if now >= monitor_until:
                        # 已过期，从监控列表和黑名单中移除
                        expired_count += 1
                        if symbol in self.blacklist:
                            self.blacklist.discard(symbol)
                            removed_from_blacklist += 1
                            logger.info(
                                "止损监控已过期，从黑名单中移除",
                                symbol=symbol,
                                monitor_until=monitor_until_str,
                            )
                        else:
                            logger.info(
                                "止损监控已过期，移除",
                                symbol=symbol,
                                monitor_until=monitor_until_str,
                            )
                        continue
                    else:
                        # 未过期，计算剩余时间并恢复监控
                        remaining = monitor_until - now
                        remaining_seconds = remaining.total_seconds()
                        with self._monitor_lock:
                            self._stop_loss_monitor[symbol] = {
                                "entry_price": monitor_data.get("entry_price", 0),
                                "monitor_until": monitor_until.isoformat(),
                            }
                        # P2-7: 设置定时器，到期后自动清理
                        self._schedule_monitor_expiry(symbol, remaining_seconds)
                        logger.info(
                            "止损监控恢复，已设置过期定时器",
                            symbol=symbol,
                            remaining_hours=round(remaining_seconds / 3600, 1),
                            monitor_until=monitor_until_str,
                        )
                except (ValueError, TypeError):
                    # 无法解析时间，跳过
                    expired_count += 1
                    logger.warning("止损监控时间解析失败，移除", symbol=symbol)
            else:
                # 无 monitor_until 字段，保留但设置24小时默认过期
                with self._monitor_lock:
                    self._stop_loss_monitor[symbol] = monitor_data
                default_seconds = self.blacklist_check_hours * 3600
                self._schedule_monitor_expiry(symbol, default_seconds)
                logger.info(
                    "止损监控恢复（无过期时间，使用默认值）",
                    symbol=symbol,
                    default_hours=self.blacklist_check_hours,
                )

        if expired_count > 0:
            logger.info(
                "止损监控过期清理完成",
                expired_count=expired_count,
                removed_from_blacklist=removed_from_blacklist,
            )

        # P2-6: 恢复熔断状态
        raw_cb = data.get("circuit_breakers", {})
        self._circuit_breakers = {}
        now = datetime.now(timezone.utc)
        cb_restored = 0
        cb_expired = 0
        for symbol, cb_data in raw_cb.items():
            try:
                triggered_at = datetime.fromisoformat(cb_data["triggered_at"])
                expires_at = datetime.fromisoformat(cb_data["expires_at"])
                if now >= expires_at:
                    cb_expired += 1
                    logger.info("熔断已过期，清理", symbol=symbol)
                    continue
                self._circuit_breakers[symbol] = {
                    "triggered_at": triggered_at,
                    "expires_at": expires_at,
                    "price_change": cb_data.get("price_change", 0),
                }
                cb_restored += 1
                remaining = (expires_at - now).total_seconds() / 60
                logger.info(
                    "熔断状态恢复",
                    symbol=symbol,
                    remaining_minutes=round(remaining, 1),
                )
            except (ValueError, TypeError, KeyError) as e:
                logger.warning("熔断状态恢复失败", symbol=symbol, error=str(e))
        if cb_restored > 0 or cb_expired > 0:
            logger.info(
                "熔断状态恢复完成",
                restored=cb_restored,
                expired=cb_expired,
            )

    def _schedule_monitor_expiry(self, symbol: str, delay_seconds: float) -> None:
        """
        P2-7: 设置止损监控过期定时器

        到期后自动从 _stop_loss_monitor 中移除该币种。

        Args:
            symbol: 交易对
            delay_seconds: 延迟秒数
        """
        # 取消已有定时器
        if symbol in self._monitor_timers:
            self._monitor_timers[symbol].cancel()

        if delay_seconds <= 0:
            return

        def _on_expiry():
            """定时器回调：过期清理"""
            with self._monitor_lock:
                if symbol in self._stop_loss_monitor:
                    del self._stop_loss_monitor[symbol]
                    logger.info("止损监控定时器到期，自动移除", symbol=symbol)
                if symbol in self._monitor_timers:
                    del self._monitor_timers[symbol]

        timer = threading.Timer(delay_seconds, _on_expiry)
        timer.daemon = True
        timer.start()
        self._monitor_timers[symbol] = timer

    def _cancel_monitor_timers(self) -> None:
        """
        P2-7: 取消所有止损监控定时器

        在策略停止或状态重置时调用。
        """
        with self._monitor_lock:
            for symbol, timer in list(self._monitor_timers.items()):
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._monitor_timers.clear()
        logger.debug("所有止损监控定时器已取消")

    # ==================== P2-6: 极端行情熔断 ====================

    def check_price_change(
        self,
        symbol: str,
        price_change_1h: float,
    ) -> bool:
        """
        P2-6: 检查单币种价格变化是否触发熔断

        若1小时内涨跌幅超过阈值，触发熔断并记录。

        Args:
            symbol: 交易对
            price_change_1h: 1小时涨跌幅（小数，如 0.35 表示 35%）

        Returns:
            True: 正常；False: 触发熔断
        """
        if not self.cb_enabled:
            return True

        # 检查是否已在熔断中
        if symbol in self._circuit_breakers:
            cb = self._circuit_breakers[symbol]
            now = datetime.now(timezone.utc)
            if now < cb["expires_at"]:
                return False
            # 熔断已过期，清理
            del self._circuit_breakers[symbol]
            logger.info("熔断已到期，自动恢复", symbol=symbol)

        if abs(price_change_1h) >= self.cb_price_change_threshold:
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=self.cb_duration_minutes)
            self._circuit_breakers[symbol] = {
                "triggered_at": now,
                "expires_at": expires_at,
                "price_change": price_change_1h,
            }
            logger.warning(
                "触发极端行情熔断",
                symbol=symbol,
                price_change_1h=f"{price_change_1h:.1%}",
                threshold=f"{self.cb_price_change_threshold:.0%}",
                expires_at=expires_at.isoformat(),
                duration_minutes=self.cb_duration_minutes,
            )
            return False

        return True

    def is_circuit_breaker_active(self, symbol: str) -> bool:
        """
        P2-6: 检查指定币种是否处于熔断状态

        自动清理已过期的熔断记录。

        Args:
            symbol: 交易对

        Returns:
            True: 熔断中；False: 正常
        """
        if not self.cb_enabled:
            return False

        if symbol not in self._circuit_breakers:
            return False

        cb = self._circuit_breakers[symbol]
        now = datetime.now(timezone.utc)
        if now >= cb["expires_at"]:
            # 熔断已过期，自动清理
            del self._circuit_breakers[symbol]
            logger.info("熔断已到期，自动恢复", symbol=symbol)
            return False

        return True

    def get_circuit_breaker_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        P2-6: 获取熔断详情

        Args:
            symbol: 交易对

        Returns:
            熔断信息字典，无熔断返回 None
        """
        if symbol not in self._circuit_breakers:
            return None
        cb = self._circuit_breakers[symbol]
        now = datetime.now(timezone.utc)
        if now >= cb["expires_at"]:
            del self._circuit_breakers[symbol]
            return None
        return {
            "symbol": symbol,
            "triggered_at": cb["triggered_at"].isoformat(),
            "expires_at": cb["expires_at"].isoformat(),
            "price_change": cb["price_change"],
            "remaining_minutes": (cb["expires_at"] - now).total_seconds() / 60,
        }

    def get_all_circuit_breaker_symbols(self) -> Set[str]:
        """
        P2-6: 获取所有熔断中的币种

        Returns:
            熔断币种集合
        """
        now = datetime.now(timezone.utc)
        expired = []
        for symbol, cb in list(self._circuit_breakers.items()):
            if now >= cb["expires_at"]:
                expired.append(symbol)
        for symbol in expired:
            del self._circuit_breakers[symbol]
        return set(self._circuit_breakers.keys())

    async def _send_pause_notification(self, reason: str, pause_duration: timedelta) -> None:
        """
        P1-8: 发送暂停/熔断通知

        Args:
            reason: 暂停原因
            pause_duration: 暂停时长
        """
        if not self._notification_callback:
            return

        # 检查通知事件开关
        if self._should_notify and not self._should_notify("pause_notification"):
            return

        try:
            pause_hours = pause_duration.total_seconds() / 3600
            message = (
                f"【HRS策略暂停通知】\n"
                f"暂停原因: {reason}\n"
                f"暂停时长: {pause_duration.days}天{pause_duration.seconds // 3600}小时\n"
                f"当前连续亏损次数: {self.consecutive_losses}\n"
                f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            await self._notification_callback(
                message=message,
                level="warning",
                project="hrs",
            )
            logger.info("暂停通知已发送", reason=reason, pause_hours=pause_hours)
        except Exception as e:
            logger.warning("发送暂停通知失败", error=str(e))