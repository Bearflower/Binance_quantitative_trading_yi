"""
利润提取提醒任务

每天 07:35（北京时间）检查账户总权益是否创新高，
若创新高则计算应提取盈利，通过飞书推送提醒。

逻辑：
    - 获取币安账户总权益（totalWalletBalance）
    - 与数据库持久化的历史最高值（ATH）比较
    - 创新高则更新 ATH，计算盈利 = 当前权益 - 初始资金
    - 盈利 > 0 且建议提取额 >= 最小提取额 → 发送通知
    - 每周最多推送一次，避免重复骚扰
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

import structlog
from apscheduler.triggers.cron import CronTrigger

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.notification import NotificationClient

logger = structlog.get_logger()

# 系统状态表，存储所有系统级全局键值
_SYSTEM_STATE_TABLE = "system_state"


class ProfitExtractionJob:
    """
    利润提取提醒任务

    每天定时检查账户权益，创新高时发送提取建议通知。
    数据持久化到 system_state 表，重启后 ATH 值不丢失。
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        notification_client: NotificationClient,
        binance: BinanceClient,
        config: Dict[str, Any],
    ):
        """
        Args:
            db_manager: 数据库管理器
            notification_client: 飞书通知客户端
            binance: 币安 API 客户端
            config: 完整系统配置 dict
        """
        self.db_manager = db_manager
        self.notification_client = notification_client
        self.binance = binance
        self.extraction_cfg = config.get("profit_extraction", {})
        self._ath_balance: Decimal = Decimal("0")
        self._last_notified_week: Optional[str] = None

    # ──────────────────────────────────────
    # 对外接口
    # ──────────────────────────────────────

    async def run_daily_check(self) -> Dict[str, Any]:
        """
        执行每日利润提取检查

        Returns:
            包含检查结果的字典，如 {"action": "skip", "reason": "..."}
        """
        if not self.extraction_cfg.get("enabled", True):
            logger.info("利润提取提醒未启用，跳过")
            return {"action": "skip", "reason": "disabled"}

        try:
            # 1. 获取账户总权益
            account_info = await self.binance.get_account_info()
            total_equity = Decimal(str(account_info.get("totalWalletBalance", 0)))
            if total_equity <= Decimal("0"):
                return {"action": "skip", "reason": "zero_or_negative_equity"}

            # 2. 加载持久化 ATH
            await self._load_ath_balance()

            # 3. 检查是否创新高
            if total_equity <= self._ath_balance:
                logger.debug("账户权益未创新高", equity=float(total_equity), ath=float(self._ath_balance))
                return {"action": "skip", "reason": "not_new_high"}

            # 4. 创新高，更新持久化 ATH
            old_ath = self._ath_balance
            self._ath_balance = total_equity
            await self._save_ath_balance(total_equity)

            # 5. 计算盈利
            initial_capital = Decimal(str(self.extraction_cfg.get("initial_capital_usdt", 500)))
            profit = total_equity - initial_capital
            if profit <= Decimal("0"):
                return {"action": "skip", "reason": "not_profitable"}

            # 6. 计算建议提取额
            extract_ratio = Decimal(str(self.extraction_cfg.get("extract_ratio", 0.50)))
            min_extract = Decimal(str(self.extraction_cfg.get("min_extract_usdt", 10)))
            extract_amount = profit * extract_ratio

            if extract_amount < min_extract:
                logger.debug(
                    "建议提取额低于最小提取额，跳过",
                    extract_amount=float(extract_amount),
                    min_extract=float(min_extract),
                )
                return {"action": "skip", "reason": "below_min_extract"}

            # 7. 每周最多推送一次
            current_week = datetime.now().strftime("%Y-W%W")
            if self._last_notified_week == current_week:
                return {"action": "skip", "reason": "already_notified_this_week"}

            self._last_notified_week = current_week

            # 8. 发送飞书通知
            await self._send_notification(
                total_equity=total_equity,
                initial_capital=initial_capital,
                profit=profit,
                extract_amount=extract_amount,
                extract_ratio=extract_ratio,
                old_ath=old_ath,
            )

            logger.info(
                "利润提取提醒已发送",
                equity=float(total_equity),
                profit=float(profit),
                extract_amount=float(extract_amount),
            )
            return {
                "action": "notified",
                "equity": float(total_equity),
                "profit": float(profit),
                "extract_amount": float(extract_amount),
            }

        except Exception as e:
            logger.error("利润提取检查异常", error=str(e), exc_info=True)
            return {"action": "error", "reason": str(e)}

    def get_cron_trigger(self) -> CronTrigger:
        """返回每日 07:35 CST 的 cron trigger"""
        return CronTrigger(
            hour=7,
            minute=35,
            timezone="Asia/Shanghai",
        )

    # ──────────────────────────────────────
    # 数据库操作
    # ──────────────────────────────────────

    async def _ensure_system_state_table(self) -> None:
        """创建系统状态表（如果不存在）"""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {_SYSTEM_STATE_TABLE} (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
        try:
            await self.db_manager.execute_ddl(create_sql)
        except Exception as e:
            logger.warning("创建系统状态表失败", error=str(e))

    async def _load_ath_balance(self) -> None:
        """从数据库加载 ATH 余额"""
        try:
            await self._ensure_system_state_table()
            row = await self.db_manager.fetch_one(
                f"SELECT value FROM {_SYSTEM_STATE_TABLE} WHERE key = $1",
                "ath_balance",
            )
            if row and row.get("value") is not None:
                self._ath_balance = Decimal(row["value"])
                logger.info("ATH余额已从数据库恢复", ath_balance=float(self._ath_balance))
        except Exception as e:
            logger.warning("加载ATH余额失败", error=str(e))

    async def _save_ath_balance(self, new_ath: Decimal) -> None:
        """保存 ATH 余额到数据库"""
        try:
            await self._ensure_system_state_table()
            await self.db_manager.execute(
                f"""INSERT INTO {_SYSTEM_STATE_TABLE} (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW()
                """,
                "ath_balance",
                str(new_ath),
            )
        except Exception as e:
            logger.warning("保存ATH余额失败", error=str(e))

    # ──────────────────────────────────────
    # 通知
    # ──────────────────────────────────────

    async def _send_notification(
        self,
        total_equity: Decimal,
        initial_capital: Decimal,
        profit: Decimal,
        extract_amount: Decimal,
        extract_ratio: Decimal,
        old_ath: Decimal,
    ) -> None:
        """发送利润提取通知"""
        message = (
            f"💰 利润提取提醒\n"
            f"账户权益创新高！\n\n"
            f"当前权益：{float(total_equity):.2f} U\n"
            f"初始资金：{float(initial_capital):.2f} U\n"
            f"累计盈利：{float(profit):.2f} U\n"
            f"建议提取：{float(extract_amount):.2f} U（盈利的 {extract_ratio * 100:.0f}%）\n"
            f"上次最高：{float(old_ath):.2f} U → 当前：{float(total_equity):.2f} U"
        )
        try:
            await self.notification_client.send(
                message=message,
                level="info",
                title="利润提取提醒",
            )
        except Exception as e:
            logger.error("发送利润提取通知失败", error=str(e))