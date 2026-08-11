"""
策略记忆库数据库操作
提供 strategy_memory 表的 CRUD 操作

所有操作通过 DatabaseManager 的参数化查询执行，防止 SQL 注入。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class MemoryDBHandler:
    """
    策略记忆库数据库处理器

    管理 strategy_memory 表的增删改查，支持：
    - 保存新的调优记忆
    - 查询历史记忆（滑动窗口）
    - 审批状态管理
    - 效果追踪
    """

    def __init__(self, db_manager):
        """
        初始化记忆库处理器

        Args:
            db_manager: DatabaseManager 实例
        """
        self.db_manager = db_manager
        self._schema = None  # 在 ensure_table_exists 时设置

    async def ensure_table_exists(self, schema: str = "trading") -> None:
        """
        确保 strategy_memory 表存在，如不存在则创建

        Args:
            schema: 数据库 schema 名称
        """
        self._schema = schema  # 缓存 schema 供后续方法使用

        ddl = f"""
            CREATE TABLE IF NOT EXISTS {self._schema}.strategy_memory (
                id SERIAL PRIMARY KEY,
                strategy_id VARCHAR(32) NOT NULL,
                strategy_name VARCHAR(64),
                version VARCHAR(20),
                week_start VARCHAR(10),
                week_end VARCHAR(10),
                summary TEXT,
                full_report JSONB,
                ai_suggestions JSONB,
                approved_by VARCHAR(64),
                approved_at TIMESTAMP,
                is_applied BOOLEAN DEFAULT FALSE,
                is_rejected BOOLEAN DEFAULT FALSE,
                is_expired BOOLEAN DEFAULT FALSE,
                is_rolled_back BOOLEAN DEFAULT FALSE,
                rollback_reason TEXT,
                post_win_rate FLOAT,
                post_total_pnl FLOAT,
                effect_notes TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """
        await self.db_manager.execute_ddl(ddl)

        # 创建索引
        index_ddl = f"""
            CREATE INDEX IF NOT EXISTS idx_memory_strategy_date
                ON {self._schema}.strategy_memory (strategy_id, created_at DESC)
        """
        try:
            await self.db_manager.execute_ddl(index_ddl)
        except Exception as e:
            logger.debug("索引创建跳过", error=str(e))

        # 迁移：新增 active_version 字段（兼容已有表）
        migrate_ddl = f"""
            ALTER TABLE {self._schema}.strategy_memory
            ADD COLUMN IF NOT EXISTS active_version VARCHAR(20) DEFAULT ''
        """
        try:
            await self.db_manager.execute_ddl(migrate_ddl)
        except Exception as e:
            logger.debug("active_version 字段迁移跳过", error=str(e))

        # 创建 active_version 索引（加速 EffectTracker 按版本号查找）
        index_active_ddl = f"""
            CREATE INDEX IF NOT EXISTS idx_memory_active_version
                ON {self._schema}.strategy_memory (strategy_id, active_version)
        """
        try:
            await self.db_manager.execute_ddl(index_active_ddl)
        except Exception as e:
            logger.debug("active_version 索引创建跳过", error=str(e))

        logger.info("策略记忆表已就绪", schema=self._schema)

    async def save_memory(
        self,
        strategy_id: str,
        strategy_name: str,
        report: Dict[str, Any],
        ai_suggestions: Dict[str, Any],
        summary: str = "",
        active_version: str = "",
    ) -> int:
        """
        保存一条新的调优记忆记录

        Args:
            strategy_id: 策略唯一标识
            strategy_name: 策略显示名称
            report: 完整的 StrategyReport 字典
            ai_suggestions: AI 输出的调优建议字典
            summary: AI 生成的摘要
            active_version: 生效的覆盖层版本号（如 "V20260804"）

        Returns:
            新记录的 ID
        """
        import json

        meta = report.get("meta", {})
        query = f"""
            INSERT INTO {self._schema}.strategy_memory
                (strategy_id, strategy_name, version, week_start, week_end,
                 summary, full_report, ai_suggestions, active_version,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, NOW(), NOW())
            RETURNING id
        """
        row = await self.db_manager.fetch_one(
            query,
            strategy_id,
            strategy_name,
            meta.get("version", ""),
            meta.get("week_start", ""),
            meta.get("week_end", ""),
            summary,
            json.dumps(report, ensure_ascii=False, default=str),
            json.dumps(ai_suggestions, ensure_ascii=False, default=str),
            active_version,
        )

        memory_id = row["id"] if row else 0
        logger.info("保存策略记忆", strategy_id=strategy_id, memory_id=memory_id, active_version=active_version)
        return memory_id

    async def get_recent_memories(
        self, strategy_id: str, limit: int
    ) -> List[Dict[str, Any]]:
        """
        获取最近 N 条已生效的记忆

        Args:
            strategy_id: 策略唯一标识
            limit: 返回条数

        Returns:
            记忆记录列表
        """
        query = f"""
            SELECT id, strategy_id, summary, ai_suggestions, created_at,
                   is_applied, post_win_rate, post_total_pnl
            FROM {self._schema}.strategy_memory
            WHERE strategy_id = $1
              AND is_applied = TRUE
            ORDER BY created_at DESC
            LIMIT $2
        """
        return await self.db_manager.fetch_all(query, strategy_id, limit)

    async def find_memory_by_version(
        self,
        strategy_id: str,
        active_version: str,
    ) -> Optional[Dict[str, Any]]:
        """
        根据策略 ID 和版本号查找已生效的记忆记录

        EffectTracker 使用此方法查找"上上周已生效"的记录。

        Args:
            strategy_id: 策略唯一标识
            active_version: 版本号（如 "V20260804"）

        Returns:
            匹配的记忆记录字典，未找到返回 None
        """
        query = f"""
            SELECT id, strategy_id, active_version, post_win_rate, post_total_pnl,
                   effect_notes, full_report, ai_suggestions, created_at
            FROM {self._schema}.strategy_memory
            WHERE strategy_id = $1
              AND is_applied = TRUE
              AND active_version = $2
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            return await self.db_manager.fetch_one(query, strategy_id, active_version)
        except Exception as e:
            logger.error(
                "按版本查找记忆记录异常",
                strategy_id=strategy_id,
                active_version=active_version,
                error=str(e),
            )
            return None

    async def get_recent_applied_memories(
        self,
        strategy_id: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """
        获取最近 N 条已生效的记忆（用于学习信号规则引擎）

        LearningSignalGenerator 使用此方法实现 L2/L4 规则。

        Args:
            strategy_id: 策略唯一标识
            limit: 返回条数

        Returns:
            记忆记录列表（含 ai_suggestions 和 created_at）
        """
        query = f"""
            SELECT id, strategy_id, ai_suggestions, created_at
            FROM {self._schema}.strategy_memory
            WHERE strategy_id = $1
              AND is_applied = TRUE
            ORDER BY created_at DESC
            LIMIT $2
        """
        try:
            return await self.db_manager.fetch_all(query, strategy_id, limit)
        except Exception as e:
            logger.error(
                "获取最近已生效记忆异常",
                strategy_id=strategy_id,
                limit=limit,
                error=str(e),
            )
            return []

    async def mark_applied(self, memory_id: int, approved_by: str = "") -> bool:
        """
        标记记忆为已应用

        Args:
            memory_id: 记忆记录 ID
            approved_by: 审批人标识

        Returns:
            是否成功
        """
        query = f"""
            UPDATE {self._schema}.strategy_memory
            SET is_applied = TRUE,
                approved_by = $2,
                approved_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
        """
        await self.db_manager.execute(query, memory_id, approved_by)
        logger.info("记忆已标记为应用", memory_id=memory_id, approved_by=approved_by)
        return True

    async def mark_rejected(self, memory_id: int) -> bool:
        """
        标记记忆为已拒绝

        Args:
            memory_id: 记忆记录 ID

        Returns:
            是否成功
        """
        query = f"""
            UPDATE {self._schema}.strategy_memory
            SET is_rejected = TRUE,
                updated_at = NOW()
            WHERE id = $1
        """
        await self.db_manager.execute(query, memory_id)
        logger.info("记忆已标记为拒绝", memory_id=memory_id)
        return True

    async def mark_expired(self, memory_id: int) -> bool:
        """
        标记记忆为已过期

        Args:
            memory_id: 记忆记录 ID

        Returns:
            是否成功
        """
        query = f"""
            UPDATE {self._schema}.strategy_memory
            SET is_expired = TRUE,
                updated_at = NOW()
            WHERE id = $1
        """
        await self.db_manager.execute(query, memory_id)
        logger.info("记忆已标记为过期", memory_id=memory_id)
        return True

    async def mark_rolled_back(self, memory_id: int, reason: str = "") -> bool:
        """
        标记记忆为已回滚

        Args:
            memory_id: 记忆记录 ID
            reason: 回滚原因

        Returns:
            是否成功
        """
        query = f"""
            UPDATE {self._schema}.strategy_memory
            SET is_rolled_back = TRUE,
                rollback_reason = $2,
                updated_at = NOW()
            WHERE id = $1
        """
        await self.db_manager.execute(query, memory_id, reason)
        logger.info("记忆已标记为回滚", memory_id=memory_id, reason=reason)
        return True

    async def update_effect_tracking(
        self,
        memory_id: int,
        post_win_rate: float,
        post_total_pnl: float,
        notes: str = "",
    ) -> bool:
        """
        更新效果追踪数据

        Args:
            memory_id: 记忆记录 ID
            post_win_rate: 应用后的胜率
            post_total_pnl: 应用后的总盈亏
            notes: 备注

        Returns:
            是否成功
        """
        query = f"""
            UPDATE {self._schema}.strategy_memory
            SET post_win_rate = $2,
                post_total_pnl = $3,
                effect_notes = $4,
                updated_at = NOW()
            WHERE id = $1
        """
        try:
            await self.db_manager.execute(query, memory_id, post_win_rate, post_total_pnl, notes)
            logger.info("效果追踪已更新", memory_id=memory_id)
            return True
        except Exception as e:
            logger.error("效果追踪更新异常", memory_id=memory_id, error=str(e))
            return False

    async def get_pending_approvals(self) -> List[Dict[str, Any]]:
        """
        获取所有待审批记录

        Returns:
            待审批记录列表
        """
        query = f"""
            SELECT id, strategy_id, strategy_name, summary, ai_suggestions,
                   created_at
            FROM {self._schema}.strategy_memory
            WHERE is_applied = FALSE
              AND is_rejected = FALSE
              AND is_expired = FALSE
            ORDER BY created_at DESC
        """
        return await self.db_manager.fetch_all(query)

    async def expire_stale_approvals(self, timeout_hours: int) -> int:
        """
        使超时审批过期

        Args:
            timeout_hours: 超时时间（小时）

        Returns:
            过期的记录数
        """
        cutoff = datetime.now() - timedelta(hours=timeout_hours)
        query = f"""
            WITH updated AS (
                UPDATE {self._schema}.strategy_memory
                SET is_expired = TRUE,
                    updated_at = NOW()
                WHERE is_applied = FALSE
                  AND is_rejected = FALSE
                  AND is_expired = FALSE
                  AND created_at < $1
                RETURNING id
            )
            SELECT COUNT(*) AS cnt FROM updated
        """
        row = await self.db_manager.fetch_one(query, cutoff)
        count = row["cnt"] if row else 0
        logger.info("过期审批已清理", count=count, timeout_hours=timeout_hours)
        return count