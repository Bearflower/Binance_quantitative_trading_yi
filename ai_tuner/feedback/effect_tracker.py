"""
效果追踪器

在每周调优前，计算"上周实际表现"，回填到"上上周AI建议记录"的 post_* 字段。
通过复用 adapter.collect(week_offset=-1) 计算绩效，避免重复实现绩效计算逻辑。
"""

import os
from typing import Any, Dict, Optional

import structlog
from pydantic import BaseModel, Field

from ai_tuner.adapters.base_adapter import BaseAdapter
from ai_tuner.deploy.version_manager import VersionManager
from ai_tuner.memory.db_handler import MemoryDBHandler

logger = structlog.get_logger()


class EffectSummary(BaseModel):
    """效果摘要数据模型"""

    has_data: bool = Field(default=False, description="是否有历史数据")
    memory_id: int = Field(default=0, description="对应的记忆记录 ID")
    pre_win_rate: float = Field(default=0.0, description="调优前胜率（全量历史）")
    pre_total_pnl: float = Field(default=0.0, description="调优前总盈亏（全量历史）")
    post_win_rate: float = Field(default=0.0, description="调优后胜率（上周实际）")
    post_total_pnl: float = Field(default=0.0, description="调优后盈亏（上周实际）")
    win_rate_change: float = Field(default=0.0, description="胜率变化（百分点，正值=提升）")
    pnl_change: float = Field(default=0.0, description="盈亏变化（USDT，正值=提升）")
    max_drawdown_pct: float = Field(default=0.0, description="上周最大回撤百分比")
    total_trades: int = Field(default=0, description="上周总交易笔数")
    rating: str = Field(default="数据不足", description="评级：良好/一般/较差/数据不足")
    original_version: str = Field(default="", description="原始版本号（从 .active 读取）")
    notes: str = Field(default="", description="备注")


class EffectTracker:
    """
    效果追踪器

    在每周调优前，计算"上周实际表现"，回填到"上上周AI建议记录"的 post_* 字段。
    通过复用 adapter.collect(week_offset=-1) 计算绩效，避免重复实现。

    异常降级：
    - 所有异常在 track_and_fill() 内部捕获，返回 EffectSummary(has_data=False)
    - 不阻断主流程
    """

    def __init__(self, config: Dict[str, Any], version_manager: Optional[VersionManager] = None):
        """
        初始化效果追踪器

        Args:
            config: 系统配置字典
            version_manager: 版本管理器实例，如未提供则自动创建
        """
        self.config = config
        # 从配置读取反馈闭环配置段，使用 .get() 提供默认值确保向后兼容
        feedback_cfg = config.get("feedback", {})
        rating_cfg = feedback_cfg.get("rating", {})

        self.good_win_rate_increase = rating_cfg.get("good_win_rate_increase", 0.03)
        self.good_pnl_increase_usdt = rating_cfg.get("good_pnl_increase_usdt", 5.0)
        self.bad_win_rate_decrease = rating_cfg.get("bad_win_rate_decrease", 0.03)
        self.bad_pnl_decrease_usdt = rating_cfg.get("bad_pnl_decrease_usdt", 5.0)
        self.min_trades_for_valid = rating_cfg.get("min_trades_for_valid", 3)

        # 版本管理器：复用 VersionManager，避免重复实现版本读取逻辑
        self.version_manager = version_manager or VersionManager(config)

        # 项目根目录：从 ai_tuner/feedback/ 上溯 3 层到项目根目录
        self.project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )

        logger.debug("效果追踪器初始化完成", min_trades_for_valid=self.min_trades_for_valid)

    async def track_and_fill(
        self,
        strategy_id: str,
        adapter: BaseAdapter,
        db_handler: MemoryDBHandler,
    ) -> EffectSummary:
        """
        执行效果追踪和回填

        流程：
        1. 读取 tuning_overrides/.active 获取版本号
        2. 在 strategy_memory 表中查找匹配记录
        3. 幂等性检查：post_* 字段已填充则跳过
        4. 调用 adapter.collect(week_offset=-1) 计算上周绩效
        5. 回填 post_win_rate, post_total_pnl, effect_notes
        6. 计算评级并返回 EffectSummary

        Args:
            strategy_id: 策略唯一标识
            adapter: 策略适配器实例（复用其 collect() 方法）
            db_handler: 记忆库数据库处理器

        Returns:
            EffectSummary: 效果摘要（包含评级、指标对比、备注）
        """
        try:
            # 步骤1：读取当前生效版本号（复用 VersionManager）
            config_path = adapter.config_path
            if not config_path:
                logger.warning("适配器未配置 config_path，跳过效果追踪", strategy_id=strategy_id)
                return EffectSummary(has_data=False)

            active_version = self.version_manager.get_active_version(config_path)
            if not active_version:
                logger.info(
                    "无生效版本号，跳过效果追踪",
                    strategy_id=strategy_id,
                )
                return EffectSummary(has_data=False)

            # 步骤2：在 strategy_memory 表中查找匹配记录
            memory_record = await self._find_memory_record(
                db_handler=db_handler,
                strategy_id=strategy_id,
                active_version=active_version,
            )
            if not memory_record:
                logger.info(
                    "未找到匹配的调优记忆记录",
                    strategy_id=strategy_id,
                    active_version=active_version,
                )
                return EffectSummary(has_data=False)

            # 步骤3：幂等性检查
            if self._is_already_filled(memory_record):
                logger.info(
                    "效果数据已回填，跳过重复计算",
                    strategy_id=strategy_id,
                    memory_id=memory_record.get("id"),
                )
                return self._build_summary_from_record(memory_record, active_version)

            # 步骤4：调用 adapter.collect(week_offset=-1) 计算上周绩效
            post_report = await adapter.collect(week_offset=-1)
            post_perf = post_report.performance
            post_risk = post_report.risk

            # 提取 pre_* 基线数据
            pre_win_rate = float(memory_record.get("pre_win_rate", 0) or 0)
            pre_total_pnl = float(memory_record.get("pre_total_pnl", 0) or 0)

            # 如果 memory_record 本身没有 pre_* 字段，从 full_report 中提取
            if pre_win_rate == 0 and pre_total_pnl == 0:
                full_report = memory_record.get("full_report", {})
                if isinstance(full_report, str):
                    import json
                    try:
                        full_report = json.loads(full_report)
                    except (json.JSONDecodeError, TypeError):
                        full_report = {}
                perf = full_report.get("performance", {}) if isinstance(full_report, dict) else {}
                pre_win_rate = float(perf.get("win_rate", 0) or 0)
                pre_total_pnl = float(perf.get("total_pnl", 0) or 0)

            post_win_rate = post_perf.win_rate
            post_total_pnl = post_perf.total_pnl
            win_rate_change = post_win_rate - pre_win_rate
            pnl_change = post_total_pnl - pre_total_pnl
            max_drawdown_pct = post_risk.max_drawdown_pct
            total_trades = post_perf.total_trades

            # 步骤5：计算评级
            summary_dict = {
                "win_rate_change": win_rate_change,
                "pnl_change": pnl_change,
                "total_trades": total_trades,
            }
            rating = self._calc_rating(summary_dict)
            notes = self._build_effect_notes(rating, win_rate_change, pnl_change, total_trades)

            # 步骤6：回填 strategy_memory 记录
            memory_id = memory_record.get("id", 0)
            await db_handler.update_effect_tracking(
                memory_id=memory_id,
                post_win_rate=post_win_rate,
                post_total_pnl=post_total_pnl,
                notes=notes,
            )

            logger.info(
                "效果追踪回填完成",
                strategy_id=strategy_id,
                memory_id=memory_id,
                rating=rating,
                win_rate_change=round(win_rate_change, 4),
                pnl_change=round(pnl_change, 2),
            )

            return EffectSummary(
                has_data=True,
                memory_id=memory_id,
                pre_win_rate=pre_win_rate,
                pre_total_pnl=pre_total_pnl,
                post_win_rate=post_win_rate,
                post_total_pnl=post_total_pnl,
                win_rate_change=win_rate_change,
                pnl_change=pnl_change,
                max_drawdown_pct=max_drawdown_pct,
                total_trades=total_trades,
                rating=rating,
                original_version=active_version,
                notes=notes,
            )

        except Exception as e:
            logger.error(
                "效果追踪异常",
                strategy_id=strategy_id,
                error=str(e),
                exc_info=True,
            )
            return EffectSummary(has_data=False)

    async def _find_memory_record(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
        active_version: str,
    ) -> Optional[Dict[str, Any]]:
        """
        在 strategy_memory 表中查找匹配的记录

        Args:
            db_handler: 记忆库数据库处理器
            strategy_id: 策略唯一标识
            active_version: 版本号

        Returns:
            匹配的记录字典，未找到返回 None
        """
        try:
            return await db_handler.find_memory_by_version(
                strategy_id=strategy_id,
                active_version=active_version,
            )
        except Exception as e:
            logger.error(
                "查找记忆记录异常",
                strategy_id=strategy_id,
                active_version=active_version,
                error=str(e),
            )
            return None

    @staticmethod
    def _is_already_filled(memory_record: Dict[str, Any]) -> bool:
        """
        检查 post_* 字段是否已填充（幂等性校验）

        Args:
            memory_record: 记忆记录字典

        Returns:
            True 如果已填充，False 否则
        """
        post_win = memory_record.get("post_win_rate")
        post_pnl = memory_record.get("post_total_pnl")
        return post_win is not None and post_pnl is not None

    def _calc_rating(self, summary: Dict[str, Any]) -> str:
        """
        计算效果评级

        评级规则（从配置读取阈值）：
        - 良好: 胜率提升 >= good_win_rate_increase 或 收益提升 >= good_pnl_increase_ratio
        - 较差: 胜率下降 >= bad_win_rate_decrease 且 收益下降 >= bad_pnl_decrease_ratio
        - 数据不足: 上周交易笔数 < min_trades_for_valid
        - 一般: 不符合上述条件

        Args:
            summary: 包含 win_rate_change, pnl_change, total_trades 等字段的字典

        Returns:
            "良好" / "一般" / "较差" / "数据不足"
        """
        win_rate_change = summary.get("win_rate_change", 0)
        pnl_change = summary.get("pnl_change", 0)
        total_trades = summary.get("total_trades", 0)

        # 先判断数据是否充足
        if total_trades < self.min_trades_for_valid:
            return "数据不足"

        # 判断是否良好（或条件）
        if win_rate_change >= self.good_win_rate_increase or pnl_change >= self.good_pnl_increase_usdt:
            return "良好"

        # 判断是否较差（且条件）
        if win_rate_change <= -self.bad_win_rate_decrease and pnl_change <= -self.bad_pnl_decrease_usdt:
            return "较差"

        return "一般"

    @staticmethod
    def _build_effect_notes(
        rating: str,
        win_rate_change: float,
        pnl_change: float,
        total_trades: int,
    ) -> str:
        """
        构建效果备注文本

        Args:
            rating: 评级
            win_rate_change: 胜率变化
            pnl_change: 盈亏变化
            total_trades: 总交易笔数

        Returns:
            备注文本
        """
        parts = [
            f"评级：{rating}",
        ]

        if total_trades > 0:
            wr_sign = "+" if win_rate_change >= 0 else ""
            pnl_sign = "+" if pnl_change >= 0 else ""
            parts.append(f"胜率变化：{wr_sign}{win_rate_change:.1%}")
            parts.append(f"盈亏变化：{pnl_sign}{pnl_change:.2f} USDT")
            parts.append(f"交易笔数：{total_trades}")

        return "；".join(parts)

    def _build_summary_from_record(
        self,
        memory_record: Dict[str, Any],
        active_version: str,
    ) -> EffectSummary:
        """
        从已有记录构建 EffectSummary（幂等性跳过时使用）

        Args:
            memory_record: 记忆记录字典
            active_version: 版本号

        Returns:
            EffectSummary 实例
        """
        post_win = float(memory_record.get("post_win_rate", 0) or 0)
        post_pnl = float(memory_record.get("post_total_pnl", 0) or 0)
        notes = memory_record.get("effect_notes", "") or ""

        # 从 full_report 提取 pre_* 数据
        full_report = memory_record.get("full_report", {})
        if isinstance(full_report, str):
            import json
            try:
                full_report = json.loads(full_report)
            except (json.JSONDecodeError, TypeError):
                full_report = {}
        perf = full_report.get("performance", {}) if isinstance(full_report, dict) else {}
        pre_win = float(perf.get("win_rate", 0) or 0)
        pre_pnl = float(perf.get("total_pnl", 0) or 0)

        # 从 notes 中解析评级
        rating = "一般"
        if "评级：" in notes:
            rating_part = notes.split("评级：")[1].split("；")[0].strip()
            if rating_part in ("良好", "一般", "较差", "数据不足"):
                rating = rating_part

        return EffectSummary(
            has_data=True,
            memory_id=memory_record.get("id", 0),
            pre_win_rate=pre_win,
            pre_total_pnl=pre_pnl,
            post_win_rate=post_win,
            post_total_pnl=post_pnl,
            win_rate_change=post_win - pre_win,
            pnl_change=post_pnl - pre_pnl,
            rating=rating,
            original_version=active_version,
            notes=notes,
        )