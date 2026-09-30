"""
持仓归属隔离模块（方案C）

背景：MTPCS 原版(btc_eth)与激进版(btc_eth_aggressive)共享同一币安 PM 账户，
binance.get_position() 返回整个共享账户的全部持仓。此前策略补保护单/对账时仅按
"币种在白名单 + 数量过滤"，不区分持仓归属，导致把对家策略开的仓位误认成自己、
凭空造态叠挂保护单、互相误平。

本模块实现"开仓订单归属隔离"：
- 归属挂在开仓订单（trade_records 中 order_type IN ('LIMIT','MARKET')
  且 realized_pnl IS NULL 的未平开仓单），策略名即该订单的 strategy 字段。
- 边界A（resolve_position_owner）：按最新未平开仓单判定归属；
- 边界B互斥（is_symbol_owned_by_other）：同名币已有他人未平开仓单即跳过开仓，
  从源头杜绝双策略对同币并持未平开仓单。

所有 DB 异常/缺参均优雅降级返回 None / False，禁止抛异常中断主循环。
"""
import structlog
import zlib
from typing import Optional, Dict, List, Any

logger = structlog.get_logger()

# 开仓订单类型（用于定位"最新未平开仓单"）
_ENTRY_ORDER_TYPES = ('LIMIT', 'MARKET')

# 归属判定查询所用表：统一 schema 前缀常量，禁止散落硬编码
_TRADE_RECORDS_TABLE = "trading.trade_records"

# ============================================================
# R07 占用表（开仓窗口期预占）SQL 常量与配置默认值
#   见 docs/plans/fix-2026-09-29-p0-r01-r08-architecture.md §9.4/§11
# ============================================================
_CLAIMS_TABLE = "trading.position_claims"

# 默认值（架构 §9.4 明确给出的默认口径；均可由各策略 ownership 段覆盖）
DEFAULT_OWNERSHIP_ENABLED = True
DEFAULT_CLAIM_TTL_MINUTES = 30
DEFAULT_CLAIM_CLEANUP_INTERVAL_MINUTES = 10
DEFAULT_LOCK_TIMEOUT_SECONDS = 5

# 有效占用查询（仅未过期）
_CLAIM_SELECT_ACTIVE_SQL = (
    f"SELECT strategy FROM {_CLAIMS_TABLE} "
    "WHERE symbol = $1 AND claim_state IN ('PENDING','ACTIVE') AND expires_at > NOW() "
    "ORDER BY id DESC LIMIT 1"
)
# 释放本策略占用（只释放自己的，不误放对家）
_CLAIM_RELEASE_SQL = (
    f"UPDATE {_CLAIMS_TABLE} SET claim_state = 'RELEASED', released_at = NOW(), reason = $3 "
    "WHERE symbol = $1 AND strategy = $2 AND claim_state IN ('PENDING','ACTIVE')"
)
# 清理全部过期有效占用（置 RELEASED，保留行便于审计，不删行）
_CLAIM_CLEANUP_SQL = (
    f"UPDATE {_CLAIMS_TABLE} SET claim_state = 'RELEASED', released_at = NOW(), reason = 'expired' "
    "WHERE claim_state IN ('PENDING','ACTIVE') AND expires_at <= NOW()"
)


def _supports_claims(db_manager) -> bool:
    """
    能力探测：db_manager 是否支持占用表操作

    仅真实 DatabaseManager（类属性 supports_position_claims=True）才启用占用表判定，
    mock/旧实现自动降级为既有 trade_records 互斥语义，保证既有测试与灰度回退。
    """
    return bool(getattr(type(db_manager), 'supports_position_claims', False))


def _parse_update_count(result) -> int:
    """解析 asyncpg execute（如 'UPDATE 3'）影响的记录数，异常返回 0。"""
    if not isinstance(result, str):
        return 0
    try:
        return int(result.strip().split()[-1])
    except (ValueError, IndexError):
        return 0


def _is_owner_blocking(
    owner: Optional[str],
    my_record_name: Optional[str],
    competing_record_names: Optional[List[str]],
) -> bool:
    """
    互斥判定单点：owner 是否构成对本策略的开仓互斥

    - owner 为 None（无归属）=> 不互斥（放行）
    - owner 为本策略（含加仓场景）=> 不互斥（放行）
    - competing_record_names 非空 => 仅当 owner 命中对家列表才互斥
    - competing_record_names 为空 => 与任意其他策略互斥（默认保守口径）
    """
    if owner is None or owner == my_record_name:
        return False
    if competing_record_names:
        return owner in competing_record_names
    return True


def _open_entry_sql(status_filter: bool) -> str:
    """
    构建"最新未平开仓单"归属判定 SQL（归属/互斥底层 SQL 单点，杜绝重复）

    Args:
        status_filter: True => 追加 status='FILLED'（保护单路径权威）；
                       False => 不含 status 过滤（互斥B路径，含 NEW 挂单更保守）

    Returns:
        查询 SQL 字符串（符号通过 $1 参数化）
    """
    status_clause = " AND status = 'FILLED'" if status_filter else ""
    return (
        f"SELECT strategy FROM {_TRADE_RECORDS_TABLE} "
        "WHERE symbol = $1 AND order_type IN ('LIMIT','MARKET') "
        "AND realized_pnl IS NULL"
        + status_clause
        + " ORDER BY executed_at DESC, id DESC LIMIT 1"
    )


def load_ownership_config(config) -> dict:
    """
    读取持仓归属配置（方案C）

    从配置中读取：
      - config['strategy']['record_name']：本策略归属名（如 "MTPCS策略"）
      - config['ownership']['competing_record_names']：共享账户内的对家策略名列表
    key 缺失时优雅默认不抛异常：record_name 缺失则回退到 strategy.name 并记 warning。

    Args:
        config: 策略配置字典

    Returns:
        dict：{'my_record_name': Optional[str], 'competing_record_names': List[str]}
    """
    strategy_cfg = config.get('strategy') if isinstance(config, dict) else None
    if not isinstance(strategy_cfg, dict):
        # strategy 段缺失/为 None/非 dict，统一按空 dict 对待，保证本函数不抛异常
        strategy_cfg = {}
    my_name = strategy_cfg.get('record_name')
    if not my_name:
        # 优雅降级：从策略名推导归属名，并记录预警，避免实例化失败
        my_name = strategy_cfg.get('name')
        logger.warning(
            "ownership.record_name 配置缺失，回退到 strategy.name",
            fallback=my_name,
        )
    ownership_cfg = config.get('ownership') if isinstance(config, dict) else None
    if not isinstance(ownership_cfg, dict):
        # ownership 段缺失/为 None/非 dict，统一按空 dict 对待，保证本函数不抛异常
        ownership_cfg = {}
    competing = ownership_cfg.get('competing_record_names') or []
    return {
        'my_record_name': my_name,
        'competing_record_names': list(competing),
        # R07 占用互斥配置（缺失时取架构 §9.4 默认值，禁止在业务层硬编码）
        'enabled': _cfg_bool(ownership_cfg.get('enabled'), DEFAULT_OWNERSHIP_ENABLED),
        'claim_ttl_minutes': _cfg_int(
            ownership_cfg.get('claim_ttl_minutes'), DEFAULT_CLAIM_TTL_MINUTES
        ),
        'claim_cleanup_interval_minutes': _cfg_int(
            ownership_cfg.get('claim_cleanup_interval_minutes'),
            DEFAULT_CLAIM_CLEANUP_INTERVAL_MINUTES,
        ),
        'lock_timeout_seconds': _cfg_float(
            ownership_cfg.get('lock_timeout_seconds'), DEFAULT_LOCK_TIMEOUT_SECONDS
        ),
    }


def _cfg_bool(value, default: bool) -> bool:
    """配置布尔解析：None 取默认值；字符串 'false'/'0' 视为 False。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no', 'off')
    return bool(value)


def _cfg_int(value, default: int) -> int:
    """配置整数解析：None 或非法值取默认值。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("ownership 整数配置非法，取默认值", value=value, default=default)
        return default


def _cfg_float(value, default: float) -> float:
    """配置浮点解析：None 或非法值取默认值。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("ownership 浮点配置非法，取默认值", value=value, default=default)
        return default


async def _resolve_owner_with_lock(db_manager, symbol: str) -> Optional[str]:
    """
    在 advisory lock 事务内查询归属，串行化"查+判"（边界B互斥用）

    优先使用 DatabaseManager.fetch_one_advisory_lock（带 pg_advisory_xact_lock 的
    事务内查询）；若 db_manager 未实现该方法（如测试 mock），退化为普通 fetch_one，
    此时遗留的竞态窗口由边界A（保护单路径归属校验）兜底，可接受。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对

    Returns:
        归属策略名；查询异常/无记录/无 db_manager 返回 None
    """
    if db_manager is None:
        logger.warning("db_manager 为 None，无法判定互斥归属", symbol=symbol)
        return None
    lock_key = zlib.crc32(symbol.encode()) & 0xFFFFFFFF
    advisory_fetcher = getattr(db_manager, 'fetch_one_advisory_lock', None)
    try:
        if advisory_fetcher is not None:
            row = await advisory_fetcher(lock_key, _open_entry_sql(False), symbol)
        else:
            # 未实现 advisory 方法时退化为普通查询；竞态窗口由边界A兜底
            row = await db_manager.fetch_one(_open_entry_sql(False), symbol)
    except Exception as e:
        logger.error(
            "持仓归属（互斥）查询异常，按无归属处理",
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return None
    strategy = row.get('strategy') if row else None
    return strategy if strategy else None


async def _resolve_claim_owner(db_manager, symbol: str) -> Optional[str]:
    """
    查询该 symbol 当前的「有效占用」策略名（占用表，仅未过期，R07）

    占用表是「开仓窗口期预占」，用于在 trade_records 权威归属写入前互斥；
    查询异常/无记录/不支持占用表的 db 均返回 None（降级到 trade_records 判定）。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对

    Returns:
        占用策略名；无有效占用/查询异常/能力不足返回 None
    """
    if db_manager is None or not _supports_claims(db_manager):
        return None
    try:
        row = await db_manager.fetch_one(_CLAIM_SELECT_ACTIVE_SQL, symbol)
    except Exception as e:
        logger.error(
            "占用表查询异常，按无占用处理",
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return None
    strategy = row.get('strategy') if row else None
    return strategy if strategy else None


async def resolve_position_owner(
    db_manager,
    symbol: str,
    *,
    status_filter: bool = True,
) -> Optional[str]:
    """
    边界A：返回该 symbol 最新未平开仓单的归属策略名

    判定口径：trading.trade_records 中该 symbol 的未平开仓单
    （order_type IN ('LIMIT','MARKET') 且 realized_pnl IS NULL），
    按 executed_at DESC, id DESC 取最新一条的 strategy 字段。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对
        status_filter: True => 仅统计已成交(FILLED)开仓单（保护单路径权威）；
                       False => 不限制 status（含 NEW 挂单更保守）

    Returns:
        归属策略名；无归属/查询异常/缺参返回 None（禁止抛异常中断主循环）
    """
    if db_manager is None:
        logger.warning("db_manager 为 None，无法判定持仓归属", symbol=symbol)
        return None
    try:
        row = await db_manager.fetch_one(_open_entry_sql(status_filter), symbol)
    except Exception as e:
        logger.error(
            "持仓归属查询异常，按无归属处理",
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return None
    strategy = row.get('strategy') if row else None
    return strategy if strategy else None


async def filter_owned_positions(
    db_manager,
    my_record_name: Optional[str],
    margin_dict: Optional[Dict[str, float]],
    qty_dict: Optional[Dict[str, float]],
) -> tuple:
    """
    上报前按归属过滤（方案C触发器：看板归属失真）

    对 margin_dict/qty_dict 逐 symbol 做归属校验，剔除"最新未平开仓单归属
    非本策略"的币种，避免共享 PM 账户下把对家策略开的仓误报成自己的持仓
    （曾导致原版把激进版的 SOL 空单上报成 btc_eth，页面显示"一单一策略"）。

    容错策略：
    - db_manager / my_record_name 缺失 => 原样返回（保守，不误伤自家已成交持仓）
    - 单 symbol 归属查询异常 => 保留该 symbol（降级为不过滤，宁多报不误删自家仓）
    - 归属为 None（无未平开仓单）=> 视为通过，保留（正常历史数据无开仓单时不应被误删）

    通过条件：owner 为 None 或 owner == my_record_name。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        my_record_name: 本策略归属名（如 "MTPCS策略"），None 则不过滤
        margin_dict: {symbol: 保证金}，上报用
        qty_dict: {symbol: 持仓数量}，与 margin_dict 同步过滤

    Returns:
        (过滤后的 margin_dict, qty_dict)
    """
    # 快速失败：缺参或空持仓，直接原样返回
    if db_manager is None or not my_record_name:
        return dict(margin_dict or {}), dict(qty_dict or {})
    margin_dict = dict(margin_dict or {})
    qty_dict = dict(qty_dict or {})
    if not margin_dict and not qty_dict:
        return margin_dict, qty_dict

    symbols = set(margin_dict) | set(qty_dict)
    keep = set()
    for sym in symbols:
        try:
            # 上报过滤路径必须匹配 NEW 挂单（成交记录 status 多为 NEW），
            # 故 status_filter=False；否则归属查询恒为 None 导致过滤失效。
            owner = await resolve_position_owner(db_manager, sym, status_filter=False)
        except Exception:
            # 归属判定异常：保留该 symbol，宁多报不误删自家已成交仓
            logger.warning(
                "上报归属过滤异常，保留该 symbol",
                symbol=sym,
                exc_info=True,
            )
            keep.add(sym)
            continue
        if owner is None or owner == my_record_name:
            keep.add(sym)
        else:
            logger.info(
                "上报归属过滤：剔除非本策略持仓",
                symbol=sym,
                owner=owner,
                my_record_name=my_record_name,
            )
    return (
        {s: m for s, m in margin_dict.items() if s in keep},
        {s: q for s, q in qty_dict.items() if s in keep},
    )


async def is_symbol_owned_by_other(
    db_manager,
    symbol: str,
    my_record_name: Optional[str],
    competing_record_names: Optional[List[str]] = None,
    *,
    enabled: bool = True,
) -> bool:
    """
    边界B：判断该 symbol 是否已被"其他策略"持有（持仓期互斥）

    双重判定（R07 §9.2）：占用表「有效占用（预占）」OR
    trade_records「未平开仓单（权威）」。返回任一来源构成互斥即为 True。
    competing_record_names 用于限定互斥范围：
    - 非空 => 仅与列表内的对家策略互斥（精确互斥）；
    - 空/未传 => 与任何其他策略互斥（默认保守口径）。
    任何 DB 异常均返回 False（不放行双开，记 ERROR）。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对
        my_record_name: 本策略归属名
        competing_record_names: 共享账户对家策略名列表（空则默认与任意其他策略互斥）
        enabled: ownership.enabled 开关（D4）；False 时仅做 trade_records 判定（回到既有行为）

    Returns:
        True 表示该 symbol 已被其他策略持有，应跳过开仓
    """
    # 既有权威归属判定：trade_records 未平开仓单（语义保持不变）
    authority_owner = await _resolve_owner_with_lock(db_manager, symbol)
    if _is_owner_blocking(authority_owner, my_record_name, competing_record_names):
        return True
    if not enabled:
        return False
    # R07：占用表「有效占用」判定（开仓窗口期预占，仅支持占用表的 db 才启用）
    claim_owner = await _resolve_claim_owner(db_manager, symbol)
    return _is_owner_blocking(claim_owner, my_record_name, competing_record_names)


async def try_claim_symbol(
    db_manager,
    symbol: str,
    my_record_name: str,
    *,
    competing_record_names: Optional[List[str]] = None,
    ttl_minutes: int = DEFAULT_CLAIM_TTL_MINUTES,
    intent_id: Optional[str] = None,
    enabled: bool = DEFAULT_OWNERSHIP_ENABLED,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    开仓前尝试对该 symbol 原子占位（R07 预占互斥，开仓入口用）

    关闭开关（enabled=False）或 db_manager 不支持占用表时，降级为既有
    trade_records 互斥判定（is_symbol_owned_by_other），回到既有行为；启用时调用
    DatabaseManager.claim_position_atomic 在 advisory lock 事务内完成
    「查归属 + 写占用」原子操作（外部请求必须在锁外），并二次校验权威归属，
    避免覆盖 R07 上线前已持仓的对家。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对
        my_record_name: 本策略归属名
        competing_record_names: 对家策略名列表（空则与任意其他策略互斥）
        ttl_minutes: 占用有效期（分钟）
        intent_id: 交易决策稳定标识（缺省由 db 层生成）
        enabled: ownership.enabled 开关（D4），关闭即回到既有行为
        lock_timeout_seconds: advisory lock 获取/事务超时保护（秒）

    Returns:
        {'claimed': bool, 'owner': Optional[str], 'claim_id': Optional[int]}；
        冲突/异常均返回 claimed=False（走「已被持有，跳过开仓」，不抛异常）
    """
    if not enabled or db_manager is None or not _supports_claims(db_manager):
        blocked = await is_symbol_owned_by_other(
            db_manager, symbol, my_record_name, competing_record_names, enabled=False
        )
        return {"claimed": not blocked, "owner": None, "claim_id": None}
    lock_key = zlib.crc32(symbol.encode()) & 0xFFFFFFFF
    try:
        result = await db_manager.claim_position_atomic(
            lock_key,
            symbol,
            my_record_name,
            intent_id,
            int(ttl_minutes),
            competing_names=competing_record_names,
            lock_timeout_seconds=lock_timeout_seconds,
        )
    except Exception as e:
        logger.error(
            "占用占位异常，按冲突处理（保守不双开）",
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return {"claimed": False, "owner": None, "claim_id": None}
    # 占用冲突且冲突方构成互斥（含 owner 缺失的保守情形）→ 拒绝
    if not result.get("claimed"):
        owner = result.get("owner")
        if owner is None or _is_owner_blocking(owner, my_record_name, competing_record_names):
            return {"claimed": False, "owner": owner, "claim_id": result.get("claim_id")}
    # 二次校验权威归属（trade_records）：避免覆盖 R07 上线前已持仓的对家
    authority_owner = await _resolve_owner_with_lock(db_manager, symbol)
    if _is_owner_blocking(authority_owner, my_record_name, competing_record_names):
        await release_claim(db_manager, symbol, my_record_name, reason="authority_conflict")
        return {"claimed": False, "owner": authority_owner, "claim_id": None}
    return {"claimed": True, "owner": my_record_name, "claim_id": result.get("claim_id")}


async def release_claim(
    db_manager,
    symbol: str,
    my_record_name: str,
    reason: str = "released",
) -> bool:
    """
    释放本策略对某 symbol 的占用（开仓失败/放弃/平仓归零时调用，R07-F4）

    仅释放「本策略」的 PENDING/ACTIVE 占用，不误放对家；占用置 RELEASED
    保留行（便于审计与回滚）。异常返回 False 并告警，不抛异常中断主循环。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对
        my_record_name: 本策略归属名
        reason: 释放原因（写入 reason 字段，如 'open_failed'/'position_closed'）

    Returns:
        True 表示本策略确有占用被释放（或开关关闭/能力不足视为无需释放）；
        False 表示无占用可释放或释放查询异常
    """
    if db_manager is None or not _supports_claims(db_manager):
        return True  # 未启用占用互斥时释放视为幂等成功
    try:
        result = await db_manager.execute(
            _CLAIM_RELEASE_SQL, symbol, my_record_name, reason
        )
        return _parse_update_count(result) > 0
    except Exception as e:
        logger.error(
            "释放占用异常",
            symbol=symbol,
            strategy=my_record_name,
            error=str(e),
            exc_info=True,
        )
        return False


async def cleanup_expired_claims(db_manager) -> int:
    """
    清理全部已过期占用（主循环按 claim_cleanup_interval_minutes 定时调用，R07-F7）

    将 expires_at <= NOW() 的 PENDING/ACTIVE 占用置 RELEASED（保留行不删，
    便于审计与回滚）；异常返回 0 并告警，不抛异常中断主循环。

    Args:
        db_manager: DatabaseManager 实例（可为 None）

    Returns:
        本次置 RELEASED 的过期占用行数（0 表示无过期或执行异常）
    """
    if db_manager is None or not _supports_claims(db_manager):
        return 0
    try:
        result = await db_manager.execute(_CLAIM_CLEANUP_SQL)
        return _parse_update_count(result)
    except Exception as e:
        logger.error(
            "清理过期占用异常",
            error=str(e),
            exc_info=True,
        )
        return 0
