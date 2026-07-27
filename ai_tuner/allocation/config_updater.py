"""
配置更新器

负责将月度资金分配结果写入数据库和配置文件。

写入目标：
    1. public.capital_allocation 表：持久化分配记录
    2. ai_tuner/config.yaml：更新 capital_limits 字段
    3. 各策略 config.yaml：更新 capital_limits 字段
"""

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import structlog

from ai_tuner.allocation.allocation_calculator import AllocationResult

logger = structlog.get_logger()

# 中国标准时间时区 (UTC+8)
CST = timezone(timedelta(hours=8))


class AllocationConfigUpdater:
    """
    配置更新器

    将分配结果原子化写入数据库和各配置文件。
    """

    def __init__(self):
        """初始化配置更新器"""

    async def update_all(
        self,
        result: AllocationResult,
        config: Dict[str, Any],
        db_manager,
        config_operator,
        rollback_manager,
    ) -> bool:
        """
        更新所有存储目标

        流程：
        1. 写入数据库（public.capital_allocation 表）
        2. 更新 ai_tuner/config.yaml 的 capital_limits
        3. 更新各策略 config.yaml 的 capital_limits

        Args:
            result: 分配计算结果
            config: 完整系统配置字典
            db_manager: DatabaseManager 实例
            config_operator: ConfigOperator 实例
            rollback_manager: RollbackManager 实例

        Returns:
            是否全部更新成功
        """
        try:
            # 1. 写入数据库
            db_success = await self._save_to_db(result, db_manager)
            if not db_success:
                logger.error("数据库写入失败，中断配置更新")
                return False

            # 2. 更新 ai_tuner/config.yaml
            tuner_config_success = await self._update_tuner_config(
                result, config, config_operator
            )
            if not tuner_config_success:
                logger.error("tuner 配置更新失败")
                return False

            # 3. 更新各策略 config.yaml
            strategy_success = await self._update_strategy_configs(
                result, config, config_operator
            )
            if not strategy_success:
                logger.error("策略配置更新失败")
                return False

            logger.info("所有配置更新完成", month=result.month)
            return True

        except Exception as e:
            logger.error("配置更新异常", error=str(e), exc_info=True)
            return False

    async def _save_to_db(
        self,
        result: AllocationResult,
        db_manager,
    ) -> bool:
        """
        将分配结果写入 public.capital_allocation 表

        使用 INSERT ... ON CONFLICT (month) DO NOTHING 保证幂等性。

        Args:
            result: 分配计算结果
            db_manager: DatabaseManager 实例

        Returns:
            是否写入成功
        """
        try:
            # 将 entries 序列化为 JSON
            entries_json = json.dumps(
                [
                    {
                        "strategy_id": e.strategy_id,
                        "strategy_name": e.strategy_name,
                        "realized_pnl": e.realized_pnl,
                        "initial_capital": e.initial_capital,
                        "return_rate": e.return_rate,
                        "rank": e.rank,
                        "allocated_ratio": e.allocated_ratio,
                        "allocated_amount": e.allocated_amount,
                    }
                    for e in result.entries
                ],
                ensure_ascii=False,
            )

            created_at = datetime.now(CST).isoformat()

            query = """
                INSERT INTO public.capital_allocation
                    (month, total_capital, strategy_count, is_first_month, entries, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (month) DO NOTHING
            """

            await db_manager.execute(
                query,
                result.month,
                result.total_capital,
                len(result.entries),
                result.is_first_month,
                entries_json,
                "active",
                created_at,
            )

            logger.info(
                "分配记录已写入数据库",
                month=result.month,
                total_capital=result.total_capital,
                strategy_count=len(result.entries),
            )
            return True

        except Exception as e:
            logger.error("数据库写入异常", error=str(e), exc_info=True)
            return False

    async def _update_tuner_config(
        self,
        result: AllocationResult,
        config: Dict[str, Any],
        config_operator,
    ) -> bool:
        """
        更新 ai_tuner/config.yaml 的 capital_limits 字段

        使用 ConfigOperator.apply_changes 原子写入。

        格式：
        ```yaml
        capital_limits:
          btc_eth:
            ratio: 0.36
            amount_usdt: 360.0
          ...
          risk_reserve:
            ratio: 0.10
            amount_usdt: 100.0
          total:
            ratio: 1.0
            amount_usdt: 1000.0
          allocation_month: "2026-07"
          updated_at: "2026-07-31T23:55:00+08:00"
        ```

        Args:
            result: 分配计算结果
            config: 完整系统配置字典
            config_operator: ConfigOperator 实例

        Returns:
            是否更新成功
        """
        try:
            # 构建 capital_limits 配置
            capital_limits = {}
            for entry in result.entries:
                capital_limits[entry.strategy_id] = {
                    "ratio": round(entry.allocated_ratio, 4),
                    "amount_usdt": entry.allocated_amount,
                }

            # 风险备用金
            reserve_ratio = round(result.reserve_amount / result.total_capital, 4) if result.total_capital > 0 else 0.0
            capital_limits["risk_reserve"] = {
                "ratio": reserve_ratio,
                "amount_usdt": result.reserve_amount,
            }

            # 汇总
            capital_limits["total"] = {
                "ratio": 1.0,
                "amount_usdt": result.total_capital,
            }

            # 元信息
            capital_limits["allocation_month"] = result.month
            capital_limits["updated_at"] = datetime.now(CST).isoformat()

            # 定位 tuner 配置文件路径
            tuner_config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml",
            )

            # 使用 ConfigOperator.apply_changes 原子写入整个 capital_limits 节点
            # apply_changes 内部会：备份 → 读取 → 更新 → 原子写入
            success = config_operator.apply_changes(
                config_path=tuner_config_path,
                adjustments={"capital_limits": capital_limits},
            )

            if success:
                logger.info(
                    "tuner 配置 capital_limits 已更新",
                    config_path=tuner_config_path,
                    strategy_count=len(result.entries),
                )
            else:
                logger.error("tuner 配置 capital_limits 更新失败")

            return success

        except Exception as e:
            logger.error("更新 tuner 配置异常", error=str(e), exc_info=True)
            return False

    async def _update_strategy_configs(
        self,
        result: AllocationResult,
        config: Dict[str, Any],
        config_operator,
    ) -> bool:
        """
        更新各策略 config.yaml 的 capital_limits 字段

        使用 ConfigOperator.apply_changes 原子写入。

        格式：
        ```yaml
        capital_limits:
          monthly_limit: 360.0
          allocated_ratio: 0.36
          allocation_month: "2026-07"
          updated_at: "2026-07-31T23:55:00+08:00"
        ```

        Args:
            result: 分配计算结果
            config: 完整系统配置字典
            config_operator: ConfigOperator 实例

        Returns:
            是否全部更新成功
        """
        try:
            # 构建 strategy_id -> config_path 的映射
            strategies_cfg = config.get("strategies", [])
            strategy_paths: Dict[str, str] = {}
            for s in strategies_cfg:
                sid = s.get("strategy_id", "")
                if sid:
                    strategy_paths[sid] = s.get("config_path", "")

            all_success = True
            updated_at = datetime.now(CST).isoformat()

            for entry in result.entries:
                config_path = strategy_paths.get(entry.strategy_id, "")
                if not config_path:
                    logger.warning(
                        "策略配置文件路径未找到，跳过",
                        strategy_id=entry.strategy_id,
                    )
                    continue

                if not os.path.exists(config_path):
                    logger.warning(
                        "策略配置文件不存在，跳过",
                        strategy_id=entry.strategy_id,
                        config_path=config_path,
                    )
                    continue

                try:
                    # 构建 capital_limits 配置
                    capital_limits = {
                        "monthly_limit": entry.allocated_amount,
                        "allocated_ratio": round(entry.allocated_ratio, 4),
                        "allocation_month": result.month,
                        "updated_at": updated_at,
                    }

                    # 使用 ConfigOperator.apply_changes 原子写入
                    # apply_changes 内部会：备份 → 读取 → 更新 → 原子写入
                    success = config_operator.apply_changes(
                        config_path=config_path,
                        adjustments={"capital_limits": capital_limits},
                    )

                    if success:
                        logger.info(
                            "策略配置 capital_limits 已更新",
                            strategy_id=entry.strategy_id,
                            config_path=config_path,
                            monthly_limit=entry.allocated_amount,
                            allocated_ratio=round(entry.allocated_ratio, 4),
                        )
                    else:
                        logger.error(
                            "策略配置 capital_limits 更新失败",
                            strategy_id=entry.strategy_id,
                            config_path=config_path,
                        )
                        all_success = False

                except Exception as e:
                    logger.error(
                        "更新策略配置异常",
                        strategy_id=entry.strategy_id,
                        config_path=config_path,
                        error=str(e),
                    )
                    all_success = False

            return all_success

        except Exception as e:
            logger.error("更新策略配置异常", error=str(e), exc_info=True)
            return False