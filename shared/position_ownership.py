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
from typing import Optional, Dict, List

logger = structlog.get_logger()

# 开仓订单类型（用于定位"最新未平开仓单"）
_ENTRY_ORDER_TYPES = ('LIMIT', 'MARKET')

# 归属判定查询所用表：统一 schema 前缀常量，禁止散落硬编码
_TRADE_RECORDS_TABLE = "trading.trade_records"


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
    }


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
) -> bool:
    """
    边界B：判断该 symbol 是否已被"其他策略"持有未平开仓单（持仓期互斥）

    返回 owner 存在 且 owner 不是本策略。competing_record_names 用于限定互斥范围：
    - 非空 => 仅与列表内的对家策略互斥（精确互斥）；
    - 空/未传 => 与任何其他策略互斥（默认保守口径）。
    任何 DB 异常均返回 False（不放行双开，记 ERROR）。

    Args:
        db_manager: DatabaseManager 实例（可为 None）
        symbol: 交易对
        my_record_name: 本策略归属名
        competing_record_names: 共享账户对家策略名列表（空则默认与任意其他策略互斥）

    Returns:
        True 表示该 symbol 已被其他策略持有，应跳过开仓
    """
    owner = await _resolve_owner_with_lock(db_manager, symbol)
    if owner is None:
        return False  # 无归属（含查询异常降级）不放行双开
    if owner == my_record_name:
        return False  # 归属为本策略（含加仓场景），放行
    if competing_record_names:
        return owner in competing_record_names
    return True