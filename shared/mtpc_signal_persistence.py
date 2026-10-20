"""
MTPCS 系（btc_eth / btc_eth_aggressive）信号持久化模块

负责：
  1) 幂等建列 + 唯一索引（dedup_key）
  2) 将 execute_signal 之后的信号结果一次性写入 trade_signals（终态：executed / cancelled）
  3) 冲突语义：仅允许 cancelled → executed 升级覆盖，首写事实优先

设计约束（硬不变量）：
  - 整个落库流程 try/except 包裹，绝不 raise、绝不改变任何下单决策
  - 失败时 logger.warning + 尝试 notification.send_alert（告警调用本身也 try/except 兜底）
  - 时间口径：naive 北京时间（与 trade_logger.trade_records.executed_at 一致）
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import json
import structlog


logger = structlog.get_logger()

# 北京时区常量（UTC+8）。与 shared/trade_logger.BEIJING_TZ 同口径，
# 但本模块独立声明，避免共享模块间循环 import。
BEIJING_TZ = timezone(timedelta(hours=8))


# -------------------- DDL 模板 --------------------

# 为已有 trade_signals 表幂等补齐 dedup_key 列（VARCHAR(64)）
_ALTER_ADD_DEDUP_KEY = """
ALTER TABLE {schema}.trade_signals
    ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)
"""

# 基于 dedup_key 的唯一索引（用于 ON CONFLICT 冲突检测）
_CREATE_UNIQUE_INDEX_DEDUP = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_{schema}_signals_dedup
    ON {schema}.trade_signals(dedup_key)
"""

# 落库 SQL：一次性写终态
# - trade_success=True  → status='executed',   executed_at=now_beijing_naive
# - 否则                → status='cancelled',   executed_at=now_beijing_naive, metadata 记录原因
# ON CONFLICT 语义：仅允许 cancelled→executed 升级覆盖；executed 首写事实不被后续 cancelled 覆盖
_INSERT_SIGNAL_SQL = """
INSERT INTO {schema}.trade_signals
    (symbol, direction, grade, score, entry_price, dedup_key,
     status, executed_at, metadata)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
ON CONFLICT (dedup_key) DO UPDATE
    SET status       = EXCLUDED.status,
        executed_at  = EXCLUDED.executed_at,
        metadata     = EXCLUDED.metadata
    WHERE {schema}.trade_signals.status = 'cancelled'
      AND EXCLUDED.status                = 'executed'
"""

# 持久化结果状态常量
_STATUS_EXECUTED = "executed"
_STATUS_CANCELLED = "cancelled"


# -------------------- 公共接口 --------------------


def build_dedup_key(symbol: str, direction: str, created_at: datetime) -> str:
    """构造信号幂等键

    规则：`{SYMBOL}:{DIRECTION}:{YYYYMMDDHH}`
    - 统一大写，消除 symbol/direction 大小写漂移
    - 粒度到小时（含 direction、不含 grade/score 等决策细节），
      同小时同币同方向视为同一决策，幂等合并
    """
    symbol_norm = str(symbol or "").strip().upper()
    direction_norm = str(direction or "").strip().upper()
    try:
        hour_key = created_at.strftime("%Y%m%d%H")
    except (AttributeError, ValueError):
        # created_at 不是 datetime 对象 → 用当前北京时间兜底
        hour_key = datetime.now(BEIJING_TZ).strftime("%Y%m%d%H")
    return f"{symbol_norm}:{direction_norm}:{hour_key}"


async def ensure_signal_table_exists(db, schema: str) -> None:
    """幂等确保目标 schema 的 trade_signals 表具备 dedup_key 列 + 唯一索引

    使用 DatabaseManager.execute_ddl 绕过 SQL 安全校验（仅初始化场景）。
    任一 DDL 失败只记 error 日志，不抛异常——策略进程不应因建列失败退出。
    """
    try:
        schema_safe = _sanitize_schema(schema)
        await db.execute_ddl(_ALTER_ADD_DEDUP_KEY.format(schema=schema_safe))
        await db.execute_ddl(_CREATE_UNIQUE_INDEX_DEDUP.format(schema=schema_safe))
        logger.info(
            "信号持久化 DDL 已就绪",
            schema=schema_safe,
        )
    except Exception as exc:
        # 注意：这里不 raise。DDL 失败（如表不存在）不应阻断策略主流程，
        # persist_signal 后续会兜底吞异常并记录告警。
        logger.error(
            "信号持久化 DDL 执行失败（策略继续启动）",
            schema=schema,
            error=str(exc),
            exc_info=True,
        )


async def persist_signal(
    db,
    schema: str,
    signal: Dict[str, Any],
    notification: Optional[Any] = None,
) -> bool:
    """将一条已执行完毕的信号落库到 {schema}.trade_signals

    状态机：
      - signal.trade_success == True → status='executed'
      - 否则                          → status='cancelled'，metadata 记录 skip_reason/reject_reason

    硬不变量：
      - 整个过程 try/except 包裹，吞所有异常，**绝不 raise**，**绝不影响主流程**
      - 失败时先 logger.warning 记日志，再尝试 notification.send_alert（告警调用本身也 try/except）
      - 返回 True 表示落库成功（包括 ON CONFLICT 被 WHERE 子句过滤未更新的情况）；
        返回 False 表示真的发生了异常

    Args:
        db: DatabaseManager 实例
        schema: 目标 schema 名（如 "btc_eth" / "btc_eth_aggressive"）
        signal: analyze() 返回的完整信号字典（需含 trade_success 键表示 execute_signal 结果）
        notification: NotificationClient 实例（可选，用于告警；失败告警调用本身也被兜底）

    Returns:
        True 表示落库成功，False 表示发生异常（但从不 raise）
    """
    try:
        trade_success = bool(signal.get("trade_success"))
        status = _STATUS_EXECUTED if trade_success else _STATUS_CANCELLED

        # 字段归一化：signal 原值 + 派生字段（dedup_key / executed_at_naive / metadata）
        row = _build_signal_row(signal, status)

        schema_safe = _sanitize_schema(schema)
        await _write_signal_row(db, schema_safe, row)

        logger.info(
            "信号持久化成功",
            schema=schema_safe,
            symbol=row["symbol"],
            direction=row["direction"],
            status=status,
            dedup_key=row["dedup_key"],
        )
        return True

    except Exception as exc:
        # 防御：signal 非 dict（None/str/int/...）时，try 首行 .get 即抛错，
        # except 内不得再对原始 signal 调用取值方法（.get/.items 等），否则二次抛出破坏"绝不 raise"硬不变量。
        safe_signal = signal if isinstance(signal, dict) else {}
        logger.warning(
            "信号持久化失败（不影响主流程）",
            schema=schema,
            dedup_key=build_dedup_key(
                safe_signal.get("symbol", ""),
                safe_signal.get("direction", ""),
                _resolve_decision_time(safe_signal),
            ),
            error=str(exc)[:200],
            exc_info=True,
        )
        await _try_alert(
            notification,
            title="信号持久化失败",
            message=_format_alert_message(schema, safe_signal, exc),
        )
        return False


# -------------------- 内部辅助 --------------------

# 信号主字段名（这些字段不进 metadata，已作为 trade_signals 表的显式列存储）
_MAIN_COLUMN_KEYS = frozenset({
    "symbol", "direction", "grade", "score", "entry_price",
    "created_at", "trade_success",
})


def _resolve_decision_time(signal: Dict[str, Any]) -> datetime:
    """从信号字典解析"决策时间"（用于 dedup_key 的小时粒度）

    优先级：created_at → timestamp → 当前北京时间兜底。
    对非 datetime 值（如字符串、None）一律安全回退到 now(BEIJING_TZ)，
    确保 build_dedup_key 的 strftime 不会抛 AttributeError。
    """
    for key in ("created_at", "timestamp"):
        value = signal.get(key)
        if isinstance(value, datetime):
            return value
    return datetime.now(BEIJING_TZ)


def _build_signal_row(
    signal: Dict[str, Any], status: str
) -> Dict[str, Any]:
    """组装 trade_signals INSERT 所需的完整行字典

    负责：
      - 从 signal 提取主列值（symbol/direction/grade/score/entry_price）
      - 派生 dedup_key（走 build_dedup_key）
      - 生成 executed_at_naive（naive 北京时间）
      - 打包 metadata（排除主列；其余键（含 skip_reason/reject_reason 等审计字段）通用抄入）
    返回的 dict 键名与 SQL $1..$9 参数顺序一致。
    """
    symbol = signal.get("symbol", "")
    direction = signal.get("direction", "")
    grade = signal.get("grade")
    score = signal.get("score")
    entry_price = signal.get("entry_price")

    decision_time = _resolve_decision_time(signal)
    dedup_key = build_dedup_key(symbol, direction, decision_time)
    executed_at_naive = datetime.now(BEIJING_TZ).replace(tzinfo=None)

    metadata = _build_signal_metadata(signal)

    return {
        "symbol": symbol,
        "direction": direction,
        "grade": grade,
        "score": score,
        "entry_price": entry_price,
        "dedup_key": dedup_key,
        "status": status,
        "executed_at": executed_at_naive,
        "metadata": json.dumps(
            metadata, ensure_ascii=False, default=_json_default,
        ),
    }


async def _write_signal_row(db, schema_safe: str, row: Dict[str, Any]) -> None:
    """把一行已组装好的 signal row 写入 {schema}.trade_signals

    SQL 模板含 ON CONFLICT (dedup_key) DO UPDATE WHERE status='cancelled' AND EXCLUDED.status='executed'，
    即仅允许 cancelled → executed 升级覆盖。本函数本身不做异常处理，
    由 persist_signal 外层 try/except 兜住。
    """
    sql = _INSERT_SIGNAL_SQL.format(schema=schema_safe)
    await db.execute(
        sql,
        row["symbol"],
        row["direction"],
        row["grade"],
        row["score"],
        row["entry_price"],
        row["dedup_key"],
        row["status"],
        row["executed_at"],
        row["metadata"],
    )


def _format_alert_message(
    schema: str, signal: Dict[str, Any], exc: Exception
) -> str:
    """组装信号持久化失败告警文案（纯函数，便于单测）"""
    trade_success = bool(signal.get("trade_success"))
    status = _STATUS_EXECUTED if trade_success else _STATUS_CANCELLED
    return (
        f"schema={schema} symbol={signal.get('symbol','?')} "
        f"direction={signal.get('direction','?')} "
        f"status={status} error={str(exc)[:300]}"
    )


def _build_signal_metadata(
    signal: Dict[str, Any],
) -> Dict[str, Any]:
    """从完整信号字典中抽取 metadata 字段

    规则：
      - 排除已写入主列的键（symbol/direction/grade/score/entry_price/created_at/trade_success）
      - 除主列外的所有键通用抄入（隐式涵盖 skip_reason/reject_reason 等审计字段）
      - datetime 值转 isoformat 字符串（asyncpg jsonb 不接受 datetime 对象）
    """
    meta: Dict[str, Any] = {}

    # 把除主列外的所有键抄进来（含 skip_reason/reject_reason 等审计字段）
    for k, v in signal.items():
        if k in _MAIN_COLUMN_KEYS:
            continue
        meta[k] = _normalize_metadata_value(v)

    return meta


def _normalize_metadata_value(value: Any) -> Any:
    """递归处理 metadata 值：datetime → isoformat；其他类型原样保留"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _normalize_metadata_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_metadata_value(v) for v in value]
    return value


def _json_default(obj: Any) -> str:
    """json.dumps 的 default handler：兜底 datetime / Decimal / 其他可 str() 的对象"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def _sanitize_schema(schema: str) -> str:
    """schema 名安全检查：仅允许小写字母/数字/下划线，防 SQL 注入

    注：DatabaseManager.execute_ddl 虽然绕过 _validate_sql，
    但这里 f-string 直接拼 schema 进 DDL 模板，必须做前置白名单过滤。
    """
    if not isinstance(schema, str):
        raise ValueError(f"schema 必须是字符串，实际: {type(schema).__name__}")
    cleaned = schema.strip().lower()
    for ch in cleaned:
        if not (ch.isalnum() or ch == "_"):
            raise ValueError(f"schema 名含非法字符: {schema!r}")
    return cleaned


async def _try_alert(notification: Optional[Any], title: str, message: str) -> None:
    """尝试发送告警通知；任何异常（含 notification 为 None）都吞掉，绝不向上抛

    依赖 NotificationClient.send_alert(title, message, level="warning") → bool
    （见 shared/notification.py L420-446）。
    """
    if notification is None:
        return
    try:
        # send_alert 签名：(title, message, level="warning") → bool
        await notification.send_alert(
            title=title,
            message=message,
            level="warning",
        )
    except Exception as exc:
        logger.warning(
            "信号持久化告警发送失败（吞异常）",
            title=title,
            error=str(exc)[:120],
        )
