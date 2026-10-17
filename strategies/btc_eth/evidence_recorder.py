"""
R2｜btc_eth 策略的决策证据旁路记录器。

职责：从 analyze() 返回的 result dict 组装 DecisionEvidence + 特征，
算稳定 id（cycle_id / decision_id），调 serialize_decision 序列化，
最终通过 PostgresDecisionEvidenceSink 落库。

失败策略：整段 record() 内部 try/except 全吞，只 logger.warning；
绝不修改 result、绝不抛主循环——旁路证据采集不得影响交易决策。
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import structlog

from shared.decision_evidence import (
    DecisionEvidence,
    FeatureEvidence,
    generate_cycle_id,
    generate_decision_id,
    serialize_decision,
)

logger = structlog.get_logger()


# ── 常量（D-1：魔法数集中） ─────────────────────────────────────

# 1 小时的毫秒数（用于 cycle_anchor 取整）
_ONE_HOUR_MS = 3600 * 1000

# 数值时间戳阈值：> 1e12 视为毫秒级，否则秒级
_TS_MILLISECOND_THRESHOLD = 1e12

# 默认 db_timeout（秒）——配置缺省兜底
_DEFAULT_DB_TIMEOUT_SECONDS = 3.0

# 日志行截断（避免把异常栈/长 payload 打进字段）
_LOG_ERROR_TRUNCATE = 200

# account_id 来源优先级（与 config/env 兜底顺序一致）
_ENV_ACCOUNT_ID = "TRADE_ACCOUNT_ID"

# market_state → entry_mode 映射；缺失或未知 → 'unknown'
_ENTRY_MODE_MAP = {
    "RANGING": "ranging",
    "STRONG_TREND": "trend",
}

# 特征清单：从 result dict 提取，逐键 get 守卫，缺失跳过
_FEATURE_KEYS = [
    "score", "grade", "market_state", "entry_price", "initial_stop_loss",
    "atr", "leverage", "position_size_usdt", "quantity", "position_ratio",
    "price_change_1h", "tp1_price", "tp2_price",
]


# ── 纯函数工具 ─────────────────────────────────────────────────────

def _hourly_anchor_ms(ts: Any) -> int:
    """把决策时间戳向下取整到最近 1 小时起点的毫秒数。

    输入可为 None/int/float/datetime；None→当前时间再取整；
    datetime→timestamp() 取秒再 ×1000；int/float→直接取整。
    """
    if ts is None:
        ts = datetime.now(tz=timezone.utc)
    if isinstance(ts, datetime):
        ms = int(ts.timestamp() * 1000)
    else:
        ms = int(float(ts))  # type: ignore[arg-type]
    return ms // _ONE_HOUR_MS * _ONE_HOUR_MS


def _resolve_account_id(config: Dict[str, Any]) -> str:
    """account_id 三级兜底：config → env → 'default'。"""
    cfg_val = ""
    de_cfg = config.get("decision_evidence") if isinstance(config, dict) else None
    if isinstance(de_cfg, dict):
        cfg_val = (de_cfg.get("account_id") or "").strip()
    if cfg_val:
        return cfg_val
    env_val = os.environ.get(_ENV_ACCOUNT_ID, "").strip()
    if env_val:
        return env_val
    return "default"


def _safe_float(val: Any) -> Optional[float]:
    """安全 float；不可转则 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _build_entry_mode(result: Dict[str, Any], accepted: bool) -> str:
    """accepted 时由 market_state 映射；rejected 用 'unknown'。"""
    if not accepted:
        return "unknown"
    ms = result.get("market_state")
    if not ms:
        return "unknown"
    return _ENTRY_MODE_MAP.get(str(ms).upper(), "unknown")


def _jsonable_feature_value(val: Any) -> Any:
    """把 analyze() 返回的特征值归一为 JSON 可序列化原语（A-1）。

    Decimal → str(v)（保精度）；datetime → v.isoformat()；
    int/float/str/bool/None 原样；其他 → 尝试 json.dumps，失败 str()。
    详见函数头注释说明为什么要这么做。
    """
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    # 常规兜底：能 json.dumps 就原样，否则 str()
    import json as _json
    try:
        _json.dumps(val)
        return val
    except (TypeError, ValueError):
        return str(val)


def _normalize_decision_time(raw_ts: Any) -> datetime:
    """把各种形态的时间戳统一为带 UTC tzinfo 的 datetime（C-1）。

    naive datetime 一律按系统本地时区解释（.timestamp() 自动处理），
    再 with timestamp() 构造 UTC aware——这样 result['timestamp'] 若是
    naive 北京时间（容器 TZ=Asia/Shanghai），得到的 UTC 才是正确的，
    不会把北京时间错当成 UTC 产生 8 小时偏移。
    aware datetime 直接 astimezone(UTC)；数值时间戳秒/毫秒自动判定。
    """
    if raw_ts is None:
        return datetime.now(tz=timezone.utc)
    if isinstance(raw_ts, datetime):
        if raw_ts.tzinfo is None:
            # naive → 按系统本地时区解释（容器里是 Asia/Shanghai=UTC+8）
            # naive datetime.timestamp() 已按本地时区返回 UTC 秒
            return datetime.fromtimestamp(raw_ts.timestamp(), tz=timezone.utc)
        return raw_ts.astimezone(timezone.utc)
    # 数值时间戳：> 1e12 视为毫秒，否则秒
    ts_num = float(raw_ts)
    if ts_num > _TS_MILLISECOND_THRESHOLD:
        return datetime.fromtimestamp(ts_num / 1000.0, tz=timezone.utc)
    return datetime.fromtimestamp(ts_num, tz=timezone.utc)


# ── recorder 主类 ──────────────────────────────────────────────────

class DecisionEvidenceRecorder:
    """btc_eth 决策证据旁路记录器。

    - sink 需具备 async upsert_evidence(payload)；关闭或 None 时 record 空跑。
    - config 顶层字典，需含 decision_evidence.enabled / account_id / db_timeout_seconds。
    - 所有异常在 record() 内部吞掉，绝不抛给调用方。
    """

    def __init__(self, sink: Any, config: Dict[str, Any]) -> None:
        self.sink = sink  # 可为 None（开关关闭时）
        self.config = config or {}
        self.strategy_id = "btc_eth"
        self.account_id = _resolve_account_id(self.config)
        de_cfg = self.config.get("decision_evidence") or {}
        self.db_timeout = float(
            de_cfg.get("db_timeout_seconds") or _DEFAULT_DB_TIMEOUT_SECONDS
        )
        # D-3：与 main.py 开关口径统一（truthy bool()）
        self.enabled = bool(de_cfg.get("enabled"))

    async def record(self, result: Dict[str, Any], symbol: str) -> None:
        """旁路记录一次决策证据。全吞异常，绝不改 result。

        enabled=False 或 sink=None 时直接返回；内部 try/except 保证
        即使序列化失败也不影响主循环。
        """
        if not self.enabled or self.sink is None:
            return
        try:
            await self._do_record_async(result, symbol)
        except Exception as exc:
            logger.warning(
                "决策证据记录失败，不影响主循环",
                symbol=symbol,
                error=str(exc)[:_LOG_ERROR_TRUNCATE],
            )

    # ── 内部编排（B-2：拆为 ≤50 行的小函数） ──────────────────────────

    async def _do_record_async(self, result: Dict[str, Any], symbol: str) -> None:
        """record 内部编排：组装 DecisionEvidence → 序列化 → 落库。"""
        decision = self._build_decision(result, symbol)
        serialized = serialize_decision(decision)
        if serialized.payload is None:
            logger.warning(
                "决策证据 payload 为空，跳过落库",
                symbol=symbol, decision_id=decision.decision_id,
            )
            return
        await self._write_or_drop(serialized.payload, symbol)

    def _build_decision(
        self, result: Dict[str, Any], symbol: str,
    ) -> DecisionEvidence:
        """从 result dict + symbol 组装完整 DecisionEvidence（含稳定 id）。"""
        accepted = "direction" in result
        direction = result.get("direction") or "FLAT"
        rejection_reason = None if accepted else result.get("reason")
        entry_mode = _build_entry_mode(result, accepted)

        # C-1：naive datetime 时区归一化
        raw_ts = result.get("timestamp")
        decision_time = _normalize_decision_time(raw_ts)

        anchor_ms = _hourly_anchor_ms(raw_ts)
        cycle_id = generate_cycle_id(
            self.strategy_id, symbol, direction, entry_mode, anchor_ms,
        )
        decision_id = generate_decision_id(
            self.account_id, self.strategy_id, symbol, direction, entry_mode, cycle_id,
        )

        decision = DecisionEvidence(
            account_id=self.account_id,
            strategy_id=self.strategy_id,
            decision_id=decision_id,
            cycle_id=cycle_id,
            decision_time_utc=decision_time,
            entry_mode=entry_mode,
            symbol=symbol,
            direction=direction,
            accepted=accepted,
            rejection_reason=rejection_reason,
            planned_qty=_safe_float(result.get("quantity")),
            planned_notional=_safe_float(result.get("position_size_usdt")),
        )
        decision.features = self._build_features(result, decision_time)
        return decision

    def _build_features(
        self,
        result: Dict[str, Any],
        decision_time: datetime,
    ) -> list:
        """从 result 提取特征列表；逐键 get 守卫，缺失跳过。

        A-1：value 统一经 _jsonable_feature_value 归一为 JSON 可序列化
        原语（Decimal→str、datetime→isoformat），保证 serialize_decision
        不因为严格 json.dumps 失败而静默丢弃特征。
        """
        features = []
        for key in _FEATURE_KEYS:
            val = result.get(key)
            if val is None:
                continue
            features.append(FeatureEvidence(
                name=key,
                value=_jsonable_feature_value(val),
                unit=None,
                event_time=decision_time,
                availability="live",
            ))
        return features

    async def _write_or_drop(
        self, payload: Dict[str, Any], symbol: str,
    ) -> None:
        """带超时保护调用 sink.upsert_evidence；异常/超时全吞。"""
        try:
            await asyncio.wait_for(
                self.sink.upsert_evidence(payload),
                timeout=self.db_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "决策证据落库超时，已跳过",
                symbol=symbol, timeout=self.db_timeout,
            )
        except Exception as exc:
            logger.warning(
                "决策证据落库异常",
                symbol=symbol, error=str(exc)[:_LOG_ERROR_TRUNCATE],
            )
