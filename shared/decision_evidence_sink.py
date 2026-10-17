"""
C1｜decision_evidence PostgreSQL sink 实现。

供策略生产者（btc_eth）在 analyze() 之后旁路写入决策证据。
payload 来自 shared/decision_evidence.py::serialize_decision(...).payload，
是一个包含决策级字段 + features 列表的 dict。

SQL 安全：串不含注释、分号、CREATE/GRANT/ALTER 等危险词，
与 shared/database.py::_validate_sql 口径一致。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


# ── 常量（D-1：日志截断魔法数集中） ───────────────────────────────

_LOG_ERROR_TRUNCATE = 200


# ── SQL 常量（无注释、无分号、无危险词；参数用 $n） ────────────────

_SQL_DECISION_UPSERT = (
    "INSERT INTO trading.decision_evidence "
    "(account_id, strategy_id, decision_id, cycle_id, decision_time_utc, "
    "code_hash, config_hash, entry_mode, universe_snapshot_id, symbol, "
    "direction, score_components, thresholds, planned_qty, planned_notional, "
    "margin_limit, margin_limit_source, accepted, rejection_reason, updated_at) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19, now()) "
    "ON CONFLICT (decision_id) DO UPDATE SET "
    " account_id = EXCLUDED.account_id, strategy_id = EXCLUDED.strategy_id, "
    " cycle_id = EXCLUDED.cycle_id, decision_time_utc = EXCLUDED.decision_time_utc, "
    " code_hash = EXCLUDED.code_hash, config_hash = EXCLUDED.config_hash, "
    " entry_mode = EXCLUDED.entry_mode, universe_snapshot_id = EXCLUDED.universe_snapshot_id, "
    " symbol = EXCLUDED.symbol, direction = EXCLUDED.direction, "
    " score_components = EXCLUDED.score_components, thresholds = EXCLUDED.thresholds, "
    " planned_qty = EXCLUDED.planned_qty, planned_notional = EXCLUDED.planned_notional, "
    " margin_limit = EXCLUDED.margin_limit, margin_limit_source = EXCLUDED.margin_limit_source, "
    " accepted = EXCLUDED.accepted, rejection_reason = EXCLUDED.rejection_reason, "
    " updated_at = now()"
)

_SQL_FEATURE_UPSERT = (
    "INSERT INTO trading.decision_feature_evidence "
    "(decision_id, name, value, unit, event_time, available_at, availability, "
    "fetched_at, source, quality_flag, fallback_reason, lookback_actual) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
    "ON CONFLICT (decision_id, name, source) DO UPDATE SET "
    " value = EXCLUDED.value, unit = EXCLUDED.unit, "
    " event_time = EXCLUDED.event_time, available_at = EXCLUDED.available_at, "
    " availability = EXCLUDED.availability, fetched_at = EXCLUDED.fetched_at, "
    " quality_flag = EXCLUDED.quality_flag, fallback_reason = EXCLUDED.fallback_reason, "
    " lookback_actual = EXCLUDED.lookback_actual"
)


def _to_text(v: Any) -> str:
    """特征 value 列是 TEXT；dict/list 需 json.dumps，其他 str()。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _jsonb_or_none(v: Any) -> Optional[str]:
    """决策级 JSONB 列：dict/list→canonical JSON str，None→None。"""
    if v is None:
        return None
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def _iso_to_timestamptz(v: Any) -> Optional[datetime]:
    """TIMESTAMPTZ 列参数归一：ISO 字符串/datetime → datetime；None → None。

    serialize_decision 产出的时间字段是 ISO 字符串（含 +00:00 时区），
    asyncpg 的 timestamptz 编码器只接受 datetime 实例，直接绑字符串会抛
    TypeError（被上层静默吞掉）。此函数把它解析回 datetime。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        # aware 直接保留；naive 补 UTC（列名以 utc 结尾；生产者侧已保证 aware UTC）
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
    # str（含 ISO 格式）或其他可转 str 的对象
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class PostgresDecisionEvidenceSink:
    """decision_evidence 的 PostgreSQL sink。

    构造函数接受 db_manager；方法全吞异常，绝不向上抛。
    """

    def __init__(self, db_manager: Any) -> None:
        self.db = db_manager

    async def upsert_evidence(self, payload: Dict[str, Any]) -> None:
        """写一条决策证据（决策级 + 所有特征，单事务）。

        payload 来自 serialize_decision(...).payload。空或异常静默返回。
        """
        try:
            if not payload:
                return
            queries: List[tuple] = []
            queries.append((_SQL_DECISION_UPSERT, self._build_decision_args(payload)))
            # 特征级
            features = payload.get("features") or []
            for feat in features:
                queries.append((_SQL_FEATURE_UPSERT, self._build_feature_args(payload, feat)))
            await self.db.execute_transaction(queries)
        except Exception as exc:
            logger.warning(
                "upsert_evidence 失败，已吞异常",
                error=str(exc)[:_LOG_ERROR_TRUNCATE],
                decision_id=payload.get("decision_id") if payload else None,
            )

    def _build_decision_args(self, payload: Dict[str, Any]) -> tuple:
        """从 payload 提取决策级 upsert 参数（按 SQL 列顺序）。"""
        return (
            payload.get("account_id"),
            payload.get("strategy_id"),
            payload.get("decision_id"),
            payload.get("cycle_id"),
            _iso_to_timestamptz(payload.get("decision_time_utc")),
            payload.get("code_hash"),
            payload.get("config_hash"),
            payload.get("entry_mode"),
            payload.get("universe_snapshot_id"),
            payload.get("symbol"),
            payload.get("direction"),
            _jsonb_or_none(payload.get("score_components")),
            _jsonb_or_none(payload.get("thresholds")),
            payload.get("planned_qty"),
            payload.get("planned_notional"),
            payload.get("margin_limit"),
            payload.get("margin_limit_source"),
            bool(payload.get("accepted", False)),
            payload.get("rejection_reason"),
        )

    def _build_feature_args(self, payload: Dict[str, Any], feat: Dict[str, Any]) -> tuple:
        """从 payload + 单条特征 dict 提取特征级 upsert 参数。"""
        return (
            payload.get("decision_id"),
            feat.get("name"),
            _to_text(feat.get("value")),
            feat.get("unit"),
            _iso_to_timestamptz(feat.get("event_time")),
            _iso_to_timestamptz(feat.get("available_at")),
            feat.get("availability") or "unknown",
            _iso_to_timestamptz(feat.get("fetched_at")),
            feat.get("source"),
            feat.get("quality_flag"),
            feat.get("fallback_reason"),
            feat.get("lookback_actual"),
        )
