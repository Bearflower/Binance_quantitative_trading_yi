"""
C1「决策与特征证据契约」—— 旁路只读基础设施。

纯模型 + 序列化 + 稳定标识生成。无 IO 副作用、不连数据库、不 import asyncpg。
用于把一次策略决策的完整上下文（当时看到的特征值、阈值、分数组件、决策意图）
以可审计、可追溯、可重放的形式序列化落盘。

稳定标识口径（防「每次重试生成新意图」幻觉）：
    cycle_id      = sha256("<strategy_id>|<symbol>|<direction>|<entry_mode>|<cycle_anchor>")[:16]
    decision_id   = sha256("<account_id>|<strategy_id>|<symbol>|<direction>|<entry_mode>|<cycle_id>")[:32]
    cycle_anchor  = 周期固定锚点（如该周期 K 线窗口起始毫秒），不是 now。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Literal

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------

# availability 取值枚举
Availability = Literal["live", "lagging", "unknown"]

# 方向枚举（与 DDL CHECK 一致）
Direction = Literal["LONG", "SHORT", "BOTH", "FLAT"]

# account_id 白名单枚举式标识（脱敏兜底前先尝试保留原值）
_ACCOUNT_WHITELIST = frozenset({
    "main", "paper", "backtest", "simulate", "dev", "prod",
})

# 疑似密钥的键名模式（递归扫描 payload 时匹配）
_SECRET_KEY_PATTERNS = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"private[_-]?key", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"passphrase", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"signature", re.IGNORECASE),
)

# 疑似密钥值的启发式（含冒号分隔 + 长度异常）
_SUSPICIOUS_VALUE_MIN_LEN = 48

# 合法哈希形态：64 位小写 hex
_HASH_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

# 异常消息 / 错误详情截断长度（避免把整个异常栈打进错误字段）
_ERROR_TRUNCATE = 200


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class FeatureEvidence:
    """单个特征的证据快照（决策当时策略看到的特征值 + 来源/质量）。"""

    name: str
    value: Any
    unit: Optional[str] = None
    event_time: Optional[datetime] = None
    available_at: Optional[datetime] = None
    availability: Availability = "unknown"
    fetched_at: Optional[datetime] = None
    source: Optional[str] = None
    quality_flag: Optional[str] = None
    fallback_reason: Optional[str] = None
    lookback_actual: Optional[str] = None


@dataclass
class DecisionEvidence:
    """一次策略决策的完整证据快照。"""

    account_id: str
    strategy_id: str
    decision_id: str
    cycle_id: str
    decision_time_utc: datetime
    code_hash: Optional[str] = None
    config_hash: Optional[str] = None
    entry_mode: Optional[str] = None
    universe_snapshot_id: Optional[str] = None
    symbol: str = ""
    direction: Direction = "FLAT"
    score_components: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None
    planned_qty: Optional[float] = None
    planned_notional: Optional[float] = None
    margin_limit: Optional[float] = None
    margin_limit_source: Optional[str] = None
    accepted: bool = False
    rejection_reason: Optional[str] = None
    features: List[FeatureEvidence] = field(default_factory=list)


@dataclass
class EvidenceSerializeResult:
    """serialize() 的返回结构：永不抛异常，用 ok/failed_records/errors 传递失败。"""

    ok: bool
    payload: Dict[str, Any]
    failed_records: int
    errors: List[str]


# ---------------------------------------------------------------------------
# 稳定标识生成（纯函数，确定性，无时间依赖）
# ---------------------------------------------------------------------------


def _sha256_hex(text: str) -> str:
    """对 UTF-8 字符串取 sha256，返回全小写十六进制。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_cycle_id(
    strategy_id: str,
    symbol: str,
    direction: str,
    entry_mode: str,
    cycle_anchor: Any,
) -> str:
    """
    生成周期稳定锚点哈希（16 位 hex）。

    Args:
        cycle_anchor: 周期起点（如该周期 K 线窗口起始毫秒、ISO 字符串、datetime）。
            注意是周期固定锚点，**不是 now**。
    """
    anchor_str = _coerce_anchor(cycle_anchor)
    raw = f"{strategy_id}|{symbol}|{direction}|{entry_mode}|{anchor_str}"
    return _sha256_hex(raw)[:16]


def generate_decision_id(
    account_id: str,
    strategy_id: str,
    symbol: str,
    direction: str,
    entry_mode: str,
    cycle_id: str,
) -> str:
    """
    生成决策意图稳定哈希（32 位 hex）。

    **只含意图键，不含 score/thresholds**（它们会漂移）。
    """
    raw = f"{account_id}|{strategy_id}|{symbol}|{direction}|{entry_mode}|{cycle_id}"
    return _sha256_hex(raw)[:32]


def _coerce_anchor(anchor: Any) -> str:
    """把各种可能的 cycle_anchor 形态规整为稳定字符串。"""
    if anchor is None:
        return ""
    if isinstance(anchor, datetime):
        # 用 UTC isoformat（含微秒），保证同一时刻同一锚点
        return anchor.isoformat()
    if isinstance(anchor, (int, float)):
        # 毫秒/秒级时间戳：统一用 int 形式，避免浮点精度漂移
        return str(int(anchor))
    return str(anchor)


# ---------------------------------------------------------------------------
# available_at 校验辅助
# ---------------------------------------------------------------------------


def check_available_at_constraint(
    feature: FeatureEvidence,
    decision_time: datetime,
) -> tuple[bool, Optional[str]]:
    """
    校验特征 available_at 语义是否满足「不得晚于决策时间」。

    返回: (passed, reason)
        - available_at 为 None 或 availability 为 unknown → 不适用，返回 (True, None)
        - available_at 存在但晚于 decision_time → 返回 (False, 原因)
        - available_at <= decision_time → 返回 (True, None)
    """
    if feature.available_at is None or feature.availability == "unknown":
        return True, None
    if feature.available_at > decision_time:
        reason = (
            f"available_at({feature.available_at.isoformat()}) "
            f"晚于 decision_time({decision_time.isoformat()}) "
            f"（特征 {feature.name}）"
        )
        return False, reason
    return True, None


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def _is_suspicious_account_id(value: str) -> bool:
    """判断 account_id 是否疑似密钥（白名单外且长度异常或含冒号分隔）。"""
    if value in _ACCOUNT_WHITELIST:
        return False
    if ":" in value and len(value) >= _SUSPICIOUS_VALUE_MIN_LEN:
        return True
    if len(value) >= 64:
        return True
    return False


def _hash_sensitive(value: str) -> str:
    """对疑似敏感值做 sha256[:16] 脱敏。"""
    return _sha256_hex(value)[:16]


def _sanitize_payload_recursive(
    node: Any,
    out_errors: List[str],
    path: str = "$",
) -> tuple[Any, int]:
    """
    递归扫描 payload，剔除形似密钥的字段。

    返回 (cleaned_node, removed_count)；剔除信息同时追加到 out_errors。
    """
    removed = 0
    if isinstance(node, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in node.items():
            if _looks_like_secret_key(k):
                out_errors.append(f"[脱敏] 剔除疑似密钥字段 key={k}（路径 {path}）")
                removed += 1
                continue
            sub_cleaned, sub_removed = _sanitize_payload_recursive(
                v, out_errors, path + "." + str(k)
            )
            removed += sub_removed
            cleaned[k] = sub_cleaned
        return cleaned, removed

    if isinstance(node, list):
        cleaned_list: List[Any] = []
        for i, item in enumerate(node):
            sub_cleaned, sub_removed = _sanitize_payload_recursive(
                item, out_errors, f"{path}[{i}]"
            )
            removed += sub_removed
            cleaned_list.append(sub_cleaned)
        return cleaned_list, removed

    return node, 0


def _looks_like_secret_key(key: str) -> bool:
    """判断 dict key 是否形似密钥（命中任一正则即视为敏感）。"""
    if not isinstance(key, str):
        return False
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


def _apply_hash_field(
    payload: Dict[str, Any],
    key: str,
    value: Optional[str],
    errors: List[str],
) -> None:
    """把 code_hash / config_hash 形态校验的公共逻辑抽成单函数。

    - value 为 None 或合法 64 位 hex → 原样保留
    - value 非法 → 哈希替换 + 追加中文告警到 errors（不计 failed_records）
    """
    if value is None or _HASH_HEX_64.match(value):
        return
    payload[key] = _hash_sensitive(value)
    errors.append(f"[脱敏] {key} 非 64 位 hex，已哈希处理")


# ---------------------------------------------------------------------------
# 序列化主入口（已拆为 3 个内部函数 + 主入口编排）
# ---------------------------------------------------------------------------


def _datetime_to_iso(dt: Optional[datetime]) -> Optional[str]:
    """datetime → ISO 8601 字符串；None 保持 None。"""
    return dt.isoformat() if dt is not None else None


def _serialize_feature(f: FeatureEvidence) -> Dict[str, Any]:
    """单个特征序列化。

    在构造 dict 之前先尝试对 value 做严格 JSON 编码（不带 default=str），
    确保不可序列化的对象能真正抛异常，被外层 catch 到并计入 failed_records。
    datetime/Decimal 等会在 _to_jsonable 阶段再处理。
    """
    # 严格校验 value 可 JSON 编码（不宽容，故意让不可序列化的对象炸）
    try:
        json.dumps(f.value)
    except (TypeError, ValueError) as e:
        raise TypeError(f"特征 value 不可 JSON 序列化: {f.name} ({type(f.value).__name__})") from e

    return {
        "name": f.name,
        "value": f.value,
        "unit": f.unit,
        "event_time": _datetime_to_iso(f.event_time),
        "available_at": _datetime_to_iso(f.available_at),
        "availability": f.availability,
        "fetched_at": _datetime_to_iso(f.fetched_at),
        "source": f.source,
        "quality_flag": f.quality_flag,
        "fallback_reason": f.fallback_reason,
        "lookback_actual": f.lookback_actual,
    }


def _to_jsonable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """用 json.dumps + loads 把 datetime/Decimal 等转成 JSON 可序列化形态。"""
    return json.loads(json.dumps(payload, default=str))


def _build_decision_payload(
    decision: DecisionEvidence,
    errors: List[str],
) -> Dict[str, Any]:
    """构造决策级字段 dict + account_id/code_hash/config_hash 脱敏。

    脱敏不计入 failed_records（值仍然有效，只是被保护性替换）。
    """
    payload: Dict[str, Any] = {
        "account_id": decision.account_id,
        "strategy_id": decision.strategy_id,
        "decision_id": decision.decision_id,
        "cycle_id": decision.cycle_id,
        "decision_time_utc": _datetime_to_iso(decision.decision_time_utc),
        "code_hash": decision.code_hash,
        "config_hash": decision.config_hash,
        "entry_mode": decision.entry_mode,
        "universe_snapshot_id": decision.universe_snapshot_id,
        "symbol": decision.symbol,
        "direction": decision.direction,
        "score_components": decision.score_components,
        "thresholds": decision.thresholds,
        "planned_qty": decision.planned_qty,
        "planned_notional": decision.planned_notional,
        "margin_limit": decision.margin_limit,
        "margin_limit_source": decision.margin_limit_source,
        "accepted": decision.accepted,
        "rejection_reason": decision.rejection_reason,
    }

    # account_id 脱敏
    if _is_suspicious_account_id(decision.account_id):
        payload["account_id"] = _hash_sensitive(decision.account_id)
        errors.append("[脱敏] account_id 疑似密钥，已哈希处理")

    # code_hash / config_hash 形态校验（抽函数 + 循环消除重复）
    for key in ("code_hash", "config_hash"):
        _apply_hash_field(payload, key, getattr(decision, key), errors)

    return payload


def _serialize_features(
    decision: DecisionEvidence,
    errors: List[str],
) -> tuple[List[Dict[str, Any]], int]:
    """逐个序列化特征 + available_at 语义校验。

    返回 (特征 payload 列表, 失败计数)。
    失败情况：单特征不可 JSON 序列化、available_at 晚于 decision_time。
    """
    feature_payloads: List[Dict[str, Any]] = []
    failed_records = 0
    for feat in decision.features:
        # available_at 语义校验（接线到主流程）
        passed, reason = check_available_at_constraint(
            feat, decision.decision_time_utc
        )
        if not passed and reason is not None:
            errors.append(reason)
            failed_records += 1

        try:
            fp = _serialize_feature(feat)
            feature_payloads.append(fp)
        except Exception as e:
            failed_records += 1
            errors.append(
                f"[特征序列化失败] name={getattr(feat, 'name', '<unknown>')} "
                f"reason={str(e)[:_ERROR_TRUNCATE]}"
            )
            logger.warning(
                "特征序列化失败，已跳过",
                feature_name=getattr(feat, "name", None),
                error=str(e)[:_ERROR_TRUNCATE],
            )
    return feature_payloads, failed_records


def _finalize_payload(
    payload: Dict[str, Any],
    errors: List[str],
) -> tuple[Dict[str, Any], int]:
    """递归脱敏 + JSON 兜底，返回 (最终 payload, 新增失败数)。"""
    cleaned, removed = _sanitize_payload_recursive(payload, errors)

    try:
        jsonable = _to_jsonable(cleaned)
    except Exception as e:
        logger.warning(
            "整体 JSON 序列化失败，payload 可能包含不可序列化对象",
            error=str(e)[:_ERROR_TRUNCATE],
        )
        errors.append(f"[整体序列化失败] reason={str(e)[:_ERROR_TRUNCATE]}")
        return cleaned, removed + 1

    return jsonable, removed


def serialize_decision(decision: DecisionEvidence) -> EvidenceSerializeResult:
    """
    序列化 DecisionEvidence → EvidenceSerializeResult。

    永不抛异常。单个特征序列化失败：不中断整体，累计 failed_records + errors，
    成功的特征仍出现在 payload。structlog 输出中文 warning。
    """
    errors: List[str] = []

    # 决策级字段 + 脱敏（account_id/code_hash/config_hash）
    payload = _build_decision_payload(decision, errors)

    # 特征列表逐个序列化 + available_at 语义校验
    feature_payloads, failed_records = _serialize_features(decision, errors)
    payload["features"] = feature_payloads

    # 递归剔除疑似密钥字段 + JSON 可序列化兜底
    jsonable, extra_failed = _finalize_payload(payload, errors)
    failed_records += extra_failed

    # ok 判定：
    #   failed_records 包含：特征序列化失败 + available_at 违约
    #   + 密钥字段剔除 + 整体 JSON 失败标记
    #   脱敏（account_id/code_hash/config_hash 哈希化）不计入 failed_records，
    #   因为值仍然有效，只是被保护性替换。
    ok = (failed_records == 0)

    if not ok:
        logger.warning(
            "决策证据序列化存在失败",
            failed_count=failed_records,
            error_count=len(errors),
        )

    return EvidenceSerializeResult(
        ok=ok,
        payload=jsonable,
        failed_records=failed_records,
        errors=errors,
    )
