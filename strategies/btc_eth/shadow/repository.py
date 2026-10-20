"""research schema 读写封装

所有 SQL 硬性 research. 前缀（架构方案 §4.4），禁止出现 trading. 的 INSERT/UPDATE。
幂等写入：ON CONFLICT DO NOTHING 保证调度重试不产生重复事件。
写失败只记日志 + 告警，不 throw 回主循环（§4.4、spec §4.4）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from datetime import datetime

import structlog

from shared.database import DatabaseManager


logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 常量：所有表名 + schema 前缀（单点，禁止散落在各文件硬编码）
# ---------------------------------------------------------------------------
_SCHEMA = "research"
_TABLE_SHADOW_RUNS = f"{_SCHEMA}.shadow_runs"
_TABLE_SHADOW_EVENTS = f"{_SCHEMA}.shadow_decision_events"


# ---------------------------------------------------------------------------
# research schema SQL（硬性前缀，供测试断言匹配）
# ---------------------------------------------------------------------------
_INSERT_RUN_SQL = (
    f"INSERT INTO {_TABLE_SHADOW_RUNS} "
    f"(run_id, instance_id, symbol, started_at, ended_at, status, "
    f"initial_virtual_equity_usdt, git_commit, git_sha, config_hashes, "
    f"data_cutoff_at) "
    f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
    f"ON CONFLICT (run_id) DO NOTHING"
)

_INSERT_EVENT_SQL = (
    f"INSERT INTO {_TABLE_SHADOW_EVENTS} "
    f"(run_id, profile_id, symbol, scheduled_at, evaluated_at, decision, "
    f"direction, grade, score, reference_price, intended_order_type, "
    f"intended_entry_price, stop_loss_rule, tp1_rule, tp2_rule, trailing_rule, "
    f"time_stop_rule, feature_snapshot, reason_codes, data_quality_reason, "
    f"config_hash, git_commit, source_kline_close_times) "
    f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, "
    f"$16, $17, $18, $19, $20, $21, $22, $23) "
    f"ON CONFLICT (run_id, profile_id, symbol, scheduled_at) DO NOTHING"
)

_CLOSE_RUN_SQL = (
    f"UPDATE {_TABLE_SHADOW_RUNS} "
    f"SET status = $1, ended_at = $2, data_cutoff_at = $3 "
    f"WHERE run_id = $4"
)

_FIND_ACTIVE_RUN_SQL = (
    f"SELECT run_id, instance_id, symbol, started_at, status, "
    f"config_hashes, git_commit, git_sha "
    f"FROM {_TABLE_SHADOW_RUNS} "
    f"WHERE instance_id = $1 AND status = 'active' "
    f"ORDER BY started_at DESC LIMIT 1"
)

_FIND_RUN_BY_ID_SQL = (
    f"SELECT * FROM {_TABLE_SHADOW_RUNS} WHERE run_id = $1"
)

_FIND_EVENTS_BY_RUN_SQL = (
    f"SELECT * FROM {_TABLE_SHADOW_EVENTS} "
    f"WHERE run_id = $1 ORDER BY scheduled_at, profile_id"
)


class ShadowRepository:
    """research schema 影子数据仓储

    只负责读写 research.* 表，不触碰 trading.*。
    契约（v2 —— 2026-10-19 修正）：
      - 写操作：捕获异常降级为日志 + 告警，不向上抛（采集主循环不能因 DB 抖动崩溃）。
        方法：upsert_shadow_run / close_shadow_run / insert_decision_event /
              insert_decision_events_batch。
      - 读操作：异常必须向上抛出（阻断项 2：导出 CLI 发现 DB 故障时必须报错退出，
        绝不能静默返回空数据导致误报"零事件"）。
        方法：find_active_run / find_run_by_id / find_events_by_run。
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    # ------------------------------------------------------------------ run
    async def upsert_shadow_run(
        self,
        *,
        run_id: str,
        instance_id: str,
        symbol: str,
        started_at: datetime,
        initial_virtual_equity_usdt: float,
        git_commit: str = "",
        git_sha: str = "",
        config_hashes: Optional[Dict[str, str]] = None,
    ) -> bool:
        """创建/幂等插入一条 shadow_run（ON CONFLICT DO NOTHING）

        返回 True 表示成功（含被冲突跳过），False 表示 DB 异常降级。
        """
        try:
            config_hashes_json = json.dumps(
                config_hashes or {}, ensure_ascii=False, sort_keys=True
            )
            await self.db.execute(
                _INSERT_RUN_SQL,
                run_id, instance_id, symbol,
                started_at, None, "active",
                initial_virtual_equity_usdt,
                git_commit or None, git_sha or None,
                config_hashes_json, None,
            )
            return True
        except Exception as exc:
            logger.error(
                "shadow_runs 写入失败（已降级，不阻断采集）",
                run_id=run_id, error=str(exc), exc_info=True,
            )
            return False

    async def close_shadow_run(
        self,
        *,
        run_id: str,
        status: str = "closed",
        ended_at: Optional[datetime] = None,
    ) -> bool:
        """关闭 shadow_run（active → closed/aborted）"""
        try:
            now = ended_at or datetime.utcnow()
            await self.db.execute(
                _CLOSE_RUN_SQL, status, now, now, run_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "shadow_runs 关闭失败",
                run_id=run_id, error=str(exc), exc_info=True,
            )
            return False

    async def find_active_run(
        self, *, instance_id: str
    ) -> Optional[Dict[str, Any]]:
        """查找该 instance_id 下最新的 active run

        读契约：查询异常必须向上抛出（阻断项 2：导出 CLI 发现 DB 故障时必须报错退出，
        绝不能静默返回空数据）。
        """
        return await self.db.fetch_one(_FIND_ACTIVE_RUN_SQL, instance_id)

    async def find_run_by_id(self, *, run_id: str) -> Optional[Dict[str, Any]]:
        """按 run_id 查 run

        读契约：查询异常必须向上抛出（阻断项 2：导出 CLI 发现 DB 故障时必须报错退出，
        绝不能静默返回 None 然后误报 run_id 不存在）。
        """
        return await self.db.fetch_one(_FIND_RUN_BY_ID_SQL, run_id)

    # ---------------------------------------------------------------- events
    async def insert_decision_event(
        self,
        event: Dict[str, Any],
    ) -> bool:
        """写入一条决策事件（ON CONFLICT DO NOTHING）

        event 字典需含以下键（与 _INSERT_EVENT_SQL 列顺序对齐）：
            run_id, profile_id, symbol, scheduled_at, evaluated_at,
            decision, direction, grade, score, reference_price,
            intended_order_type, intended_entry_price,
            stop_loss_rule, tp1_rule, tp2_rule, trailing_rule, time_stop_rule,
            feature_snapshot, reason_codes, data_quality_reason,
            config_hash, git_commit, source_kline_close_times

        feature_snapshot / reason_codes / source_kline_close_times 为 dict/list，
        会被序列化为 JSONB。所有值允许 None（除带 NOT NULL 的列外由调用方保证）。
        """
        try:
            await self.db.execute(
                _INSERT_EVENT_SQL,
                event.get("run_id"),
                event.get("profile_id"),
                event.get("symbol"),
                event.get("scheduled_at"),
                event.get("evaluated_at"),
                event.get("decision"),
                event.get("direction"),
                event.get("grade"),
                event.get("score"),
                event.get("reference_price"),
                event.get("intended_order_type"),
                event.get("intended_entry_price"),
                _json_or_none(event.get("stop_loss_rule")),
                _json_or_none(event.get("tp1_rule")),
                _json_or_none(event.get("tp2_rule")),
                _json_or_none(event.get("trailing_rule")),
                _json_or_none(event.get("time_stop_rule")),
                _json_or_none(event.get("feature_snapshot")),
                _json_or_none(event.get("reason_codes")),
                event.get("data_quality_reason"),
                event.get("config_hash"),
                event.get("git_commit"),
                _json_or_none(event.get("source_kline_close_times")),
            )
            return True
        except Exception as exc:
            logger.error(
                "shadow_decision_events 写入失败（已降级，不阻断采集）",
                run_id=event.get("run_id"),
                profile_id=event.get("profile_id"),
                error=str(exc),
                exc_info=True,
            )
            return False

    async def insert_decision_events_batch(
        self, events: List[Dict[str, Any]]
    ) -> int:
        """批量写入决策事件（逐条调用 insert_decision_event）

        返回成功写入的条数。写失败只记日志，不中断后续。
        """
        success = 0
        for event in events:
            if await self.insert_decision_event(event):
                success += 1
        return success

    async def find_events_by_run(self, *, run_id: str) -> List[Dict[str, Any]]:
        """按 run_id 查全部决策事件（导出用）

        读契约：查询异常必须向上抛出（阻断项 2：导出 CLI 发现 DB 故障时必须报错退出，
        绝不能静默返回 [] 然后导出"零事件"假结果）。
        """
        return await self.db.fetch_all(_FIND_EVENTS_BY_RUN_SQL, run_id)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _json_or_none(value: Any) -> Optional[str]:
    """将 dict/list 序列化为 JSON 字符串；其他值原样返回（None 即 None）"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


# ---------------------------------------------------------------------------
# SQL 暴露（供 test_shadow_no_trading_write 静态核对）
# ---------------------------------------------------------------------------
def get_all_repository_sql() -> List[str]:
    """返回本模块全部 SQL 字符串（供测试断言含 research. 前缀、不含 trading.）"""
    return [
        _INSERT_RUN_SQL,
        _INSERT_EVENT_SQL,
        _CLOSE_RUN_SQL,
        _FIND_ACTIVE_RUN_SQL,
        _FIND_RUN_BY_ID_SQL,
        _FIND_EVENTS_BY_RUN_SQL,
    ]
