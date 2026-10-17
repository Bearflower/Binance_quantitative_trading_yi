"""
D1｜execution_facts PostgreSQL sink 实现。

由 export_execution_facts.py 在 EXECUTION_FACTS_DB_WRITE 开关开启时注入。
负责把采集到的成交明细 / 收入流水 / 原始响应 / 游标落库到 trading schema。

错误策略：四个方法各自 try/except 全吞，structlog 打中文 warning，
绝不向上抛——上游脚本已在 sink 调用点包 try/except，这里形成双保险。

SQL 安全：串不含 -- 注释、/* 块注释、; 分号、CREATE/GRANT/ALTER 等危险词，
与 shared/database.py::_validate_sql 口径一致。

CursorRecord 单一定义在 scripts/export_execution_facts.py，
本模块通过鸭子类型字段访问（getattr），不重复声明 dataclass。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, List, Optional

import structlog

logger = structlog.get_logger()


# ── 常量（魔法数集中，D-1） ──────────────────────────────────────

# 日志行截断（避免把整个异常栈/长 payload 打进字段）
_LOG_ERROR_TRUNCATE = 200
_LOG_RAW_TRUNCATE = 60

# raw_payload 的来源/entity_key 折叠常量
_RAW_SOURCE_FILL = "userTrades"
_RAW_SOURCE_INCOME = "um/income"
_RAW_KEY_FILL = "fill"
_RAW_KEY_INCOME = "income"


# ── SQL 常量（无注释、无分号、无危险词；参数用 $n） ────────────────

_SQL_FILL_UPSERT = (
    "INSERT INTO trading.execution_fact_fill "
    "(account_id, symbol, trade_id, order_id, side, qty, price, quote_qty, "
    "commission, commission_asset, realized_pnl, trade_time_utc) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
    "ON CONFLICT (account_id, symbol, trade_id) DO UPDATE SET "
    " order_id = EXCLUDED.order_id, side = EXCLUDED.side, "
    " qty = EXCLUDED.qty, price = EXCLUDED.price, quote_qty = EXCLUDED.quote_qty, "
    " commission = EXCLUDED.commission, commission_asset = EXCLUDED.commission_asset, "
    " realized_pnl = EXCLUDED.realized_pnl, trade_time_utc = EXCLUDED.trade_time_utc, "
    " fetched_at = now()"
)

_SQL_INCOME_UPSERT = (
    "INSERT INTO trading.execution_fact_income "
    "(account_id, tran_id, symbol, income_type, income, asset, info, trade_id, event_time_utc) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
    "ON CONFLICT (account_id, tran_id) DO UPDATE SET "
    " income = EXCLUDED.income, asset = EXCLUDED.asset, info = EXCLUDED.info, "
    " trade_id = EXCLUDED.trade_id, event_time_utc = EXCLUDED.event_time_utc, "
    " fetched_at = now()"
)

_SQL_RAW_UPSERT = (
    "INSERT INTO trading.execution_fact_raw "
    "(source, entity_key, entity_id, raw_payload, payload_sha256) "
    "VALUES ($1,$2,$3,$4,$5) "
    "ON CONFLICT (source, entity_key, entity_id) DO UPDATE SET "
    " raw_payload = EXCLUDED.raw_payload, payload_sha256 = EXCLUDED.payload_sha256, "
    " fetched_at = now()"
)

_SQL_CURSOR_UPSERT = (
    "INSERT INTO trading.execution_fact_cursor "
    "(source, account_id, income_type, symbol, window_start_ms, window_end_ms, "
    "last_event_id, covered_until_ms, returned_count, page_complete, request_status, "
    "error_detail, retry_count, last_fetched_at) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13, now()) "
    "ON CONFLICT (source, account_id, income_type, symbol, window_start_ms) DO UPDATE SET "
    " window_end_ms = EXCLUDED.window_end_ms, last_event_id = EXCLUDED.last_event_id, "
    " covered_until_ms = EXCLUDED.covered_until_ms, returned_count = EXCLUDED.returned_count, "
    " page_complete = EXCLUDED.page_complete, request_status = EXCLUDED.request_status, "
    " error_detail = EXCLUDED.error_detail, retry_count = EXCLUDED.retry_count, "
    " last_fetched_at = now()"
)

_SQL_CURSOR_LOAD = (
    "SELECT source, account_id, income_type, symbol, window_start_ms, window_end_ms, "
    "last_event_id, covered_until_ms, returned_count, page_complete, request_status, "
    "error_detail, retry_count "
    "FROM trading.execution_fact_cursor "
    "WHERE source = $1 AND account_id = $2 AND income_type IS NOT DISTINCT FROM $3 "
    "AND symbol IS NOT DISTINCT FROM $4 AND window_start_ms = $5"
)


# ── 类型转换辅助函数（下沉私有，避免重复散落） ──────────────────────

def _str_to_decimal(value: Optional[str]) -> Optional[Decimal]:
    """NUMERIC 列：str→Decimal；None/空串→None；Decimal() 异常→None+warning。"""
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        logger.warning("NUMERIC 字段无法解析，降级为 None", raw=str(value)[:_LOG_RAW_TRUNCATE])
        return None


def _ms_to_timestamptz(ms: Optional[int]) -> Optional[datetime]:
    """毫秒→TIMESTAMPTZ（UTC）；None→None。"""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _jsonb_param(payload: Any) -> str:
    """JSONB 列：dict→canonical JSON 字符串（sort_keys + ensure_ascii=False）。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ── Cursor → upsert args 辅助（消除 save_cursor / _row_to_cursor 的重复） ─

def _cursor_to_args(cursor: Any) -> tuple:
    """CursorRecord（鸭子类型）→ save_cursor 的 13 个参数；
    getattr 带默认值，字段缺失时安全降级。
    顺序与 _SQL_CURSOR_UPSERT 的 $1..$13 一一对应。"""
    return (
        getattr(cursor, "source", None),
        getattr(cursor, "account_id", None),
        getattr(cursor, "income_type", None),
        getattr(cursor, "symbol", None),
        getattr(cursor, "window_start_ms", None),
        getattr(cursor, "window_end_ms", None),
        getattr(cursor, "last_event_id", None),
        getattr(cursor, "covered_until_ms", None),
        int(getattr(cursor, "returned_count", 0) or 0),
        bool(getattr(cursor, "page_complete", False)),
        getattr(cursor, "request_status", "ok"),
        getattr(cursor, "error_detail", None),
        int(getattr(cursor, "retry_count", 0) or 0),
    )


def _cursor_from_row(row: dict) -> "CursorRecord":
    """把 fetch_one 返回的 dict → scripts.export_execution_facts.CursorRecord。

    延迟 import 避免脚本里 BinanceClient 等重依赖在 import 时被触发。
    （脚本的 dataclass 仅依赖标准库，纯数据类，无 IO 副作用。）
    """
    from scripts.export_execution_facts import CursorRecord  # type: ignore
    return CursorRecord(
        source=row["source"],
        account_id=row.get("account_id", ""),
        income_type=row.get("income_type"),
        symbol=row.get("symbol"),
        window_start_ms=row["window_start_ms"],
        window_end_ms=row.get("window_end_ms"),
        last_event_id=row.get("last_event_id"),
        covered_until_ms=row.get("covered_until_ms"),
        returned_count=int(row.get("returned_count") or 0),
        page_complete=bool(row.get("page_complete")),
        request_status=row.get("request_status") or "ok",
        error_detail=row.get("error_detail"),
        retry_count=int(row.get("retry_count") or 0),
    )


class PostgresExecutionFactsSink:
    """execution_facts 的 PostgreSQL sink。

    构造函数接受 db_manager（DatabaseManager 或满足 execute/fetch_one/
    execute_transaction 的鸭子类型）。不自己建连接池——调用方负责 connect
    和 disconnect，脚本侧 CLI 的 finally 会释放。
    """

    def __init__(self, db_manager: Any) -> None:
        self.db = db_manager

    # ── 批量 upsert 通用骨架（消除 fills/income 同构主循环，D-2） ────

    async def _bulk_upsert_with_raw(
        self,
        facts: List[Any],
        fact_sql: str,
        raw_sql: str,
        build_fact_args: Callable[[Any], tuple],
        build_raw_args: Callable[[Any], tuple],
        label: str,
    ) -> None:
        """批量 upsert 一组 fact，同事务内顺带写 raw。

        空列表直接返回；异常记录中文 error 日志后 raise，
        由上游导出循环捕获转换为 errors + page_complete=False。
        """
        if not facts:
            return
        try:
            queries: List[tuple] = []
            for fact in facts:
                queries.append((fact_sql, build_fact_args(fact)))
                queries.append((raw_sql, build_raw_args(fact)))
            await self.db.execute_transaction(queries)
        except Exception as exc:
            logger.error(
                f"{label} 失败，已上抛",
                error=str(exc)[:_LOG_ERROR_TRUNCATE],
                count=len(facts),
            )
            raise

    # ── 成交明细 / 收入流水 upsert（各一行，内部调批量辅助） ─────────

    async def upsert_fills(self, facts: List[Any]) -> None:
        """批量 upsert 成交明细；顺带写 execution_fact_raw（同事务）。"""
        await self._bulk_upsert_with_raw(
            facts,
            _SQL_FILL_UPSERT,
            _SQL_RAW_UPSERT,
            self._build_fill_args,
            lambda f: self._build_raw_args(
                f,
                source=_RAW_SOURCE_FILL,
                entity_key=_RAW_KEY_FILL,
                entity_id=f"{getattr(f,'account_id','')}:{getattr(f,'symbol','')}:{getattr(f,'trade_id','')}",
            ),
            label="upsert_fills",
        )

    async def upsert_income(self, facts: List[Any]) -> None:
        """批量 upsert 收入流水；顺带写 execution_fact_raw（同事务）。"""
        await self._bulk_upsert_with_raw(
            facts,
            _SQL_INCOME_UPSERT,
            _SQL_RAW_UPSERT,
            self._build_income_args,
            lambda f: self._build_raw_args(
                f,
                source=_RAW_SOURCE_INCOME,
                entity_key=_RAW_KEY_INCOME,
                entity_id=f"{getattr(f,'account_id','')}:{getattr(f,'tran_id','')}",
            ),
            label="upsert_income",
        )

    # ── args 构造器（鸭子类型字段访问；getattr 带默认值） ───────────

    def _build_fill_args(self, fact: Any) -> tuple:
        """从 FillFact 提取 fill upsert 的 12 个参数。"""
        return (
            getattr(fact, "account_id"),
            getattr(fact, "symbol"),
            getattr(fact, "trade_id"),
            getattr(fact, "order_id"),
            getattr(fact, "side"),
            _str_to_decimal(getattr(fact, "qty")),
            _str_to_decimal(getattr(fact, "price")),
            _str_to_decimal(getattr(fact, "quote_qty")),
            _str_to_decimal(getattr(fact, "commission")),
            getattr(fact, "commission_asset"),
            _str_to_decimal(getattr(fact, "realized_pnl")),
            _ms_to_timestamptz(getattr(fact, "trade_time_ms")),
        )

    def _build_income_args(self, fact: Any) -> tuple:
        """从 IncomeFact 提取 income upsert 的 9 个参数。"""
        return (
            getattr(fact, "account_id"),
            getattr(fact, "tran_id"),
            getattr(fact, "symbol"),
            getattr(fact, "income_type"),
            _str_to_decimal(getattr(fact, "income")),
            getattr(fact, "asset"),
            getattr(fact, "info"),
            getattr(fact, "trade_id"),
            _ms_to_timestamptz(getattr(fact, "event_time_ms")),
        )

    def _build_raw_args(
        self,
        fact: Any,
        *,
        source: str,
        entity_key: str,
        entity_id: str,
    ) -> tuple:
        """raw upsert 参数（source/entity_key/entity_id 由调用方传入，
        消除 fills/income 两份同构代码）。"""
        return (
            source,
            entity_key,
            entity_id,
            _jsonb_param(getattr(fact, "raw_payload", {})),
            getattr(fact, "payload_sha256", ""),
        )

    # ── 游标 load / save ────────────────────────────────────────────

    async def load_cursor(
        self,
        source: str,
        account_id: str,
        income_type: Optional[str],
        symbol: Optional[str],
        window_start_ms: int,
    ) -> Optional[Any]:
        """按完整身份键读取已有游标（断点恢复）。

        身份键 = source + account_id + income_type + symbol + window_start_ms。
        income_type 或 symbol 为 NULL 时用 IS NOT DISTINCT FROM 匹配。
        无记录返回 None；查询异常降级返回 None（让调用方走窗口起点）。
        """
        try:
            row = await self.db.fetch_one(
                _SQL_CURSOR_LOAD, source, account_id, income_type, symbol, window_start_ms,
            )
            return _cursor_from_row(row) if row is not None else None
        except Exception as exc:
            logger.warning(
                "load_cursor 失败，返回 None 让调用方走窗口起点",
                source=source, account_id=account_id, income_type=income_type,
                symbol=symbol, window_start_ms=window_start_ms,
                error=str(exc)[:_LOG_ERROR_TRUNCATE],
            )
            return None

    async def save_cursor(self, cursor: Any) -> None:
        """upsert 采集游标。异常记录中文 error 日志后上抛，
        由上游导出循环捕获转换为 errors + page_complete=False。"""
        try:
            args = _cursor_to_args(cursor)
            await self.db.execute(_SQL_CURSOR_UPSERT, *args)
        except Exception as exc:
            src = getattr(cursor, "source", "<unknown>") if cursor is not None else "<unknown>"
            logger.error(
                "save_cursor 失败，已上抛",
                source=src,
                error=str(exc)[:_LOG_ERROR_TRUNCATE],
            )
            raise
