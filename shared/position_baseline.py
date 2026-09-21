"""
持仓基线模块

职责（全项目唯一的保证金口径与基线计算来源）：
1. 统一保证金口径：单仓占用保证金 = |positionAmt| × markPrice × contractSize / leverage
   - PM 投资组合保证金账户的 positionRisk 不返回 initialMargin，故统一按上式估算；
   - contractSize 优先取 exchangeInfo，缺失时按 1 处理并标记 estimated=True。
2. 策略总占用保证金：Σ 各仓保证金（同币种多仓累加，不丢数）。
3. 启动基线重建：从交易所 positionRisk 重建持仓基线（数量 / 入场价 / 占用）。

被 R3（限额校验）、R4（基线重建）、R7（占用上报）共同复用，禁止在调用方重复实现口径。

已知坑：
- PM 账户对"零持仓币种"返回空列表而非 positionAmt=0，因此空列表必须视为"零持仓"。
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import structlog

logger = structlog.get_logger()

# contractSize 缺失时的兜底值（按 1 处理并标记为估算值）
DEFAULT_CONTRACT_SIZE = 1.0


@dataclass
class RebuiltPosition:
    """重建后的单个持仓基线

    Attributes:
        symbol: 交易对
        quantity: 持仓数量（绝对值，正数）
        entry_price: 入场价（交易所 entryPrice，缺失时为估算的 markPrice）
        margin: 占用保证金（USDT，估算口径）
        estimated_contract_size: contractSize 是否缺失并按 1 估算
        estimated_entry_price: entryPrice 是否缺失并按 markPrice 估算
    """

    symbol: str
    quantity: float
    entry_price: float
    margin: float
    estimated_contract_size: bool = False
    estimated_entry_price: bool = False


@dataclass
class BaselineRebuildResult:
    """基线重建结果

    Attributes:
        success: 交易所调用是否成功（False 表示接口失败，调用方需保守处理）
        positions: {symbol: RebuiltPosition}，仅含做空持仓
        total_margin: 全部做空持仓的占用保证金合计（USDT）
    """

    success: bool
    positions: Dict[str, RebuiltPosition]
    total_margin: float


def calc_position_margin(
    position_amt: Optional[float],
    mark_price: Optional[float],
    contract_size: Optional[float],
    leverage: Optional[float],
) -> float:
    """
    计算单个持仓的占用保证金（统一口径，全项目唯一实现）

    公式：|position_amt| × mark_price × contract_size / leverage

    边界处理：
    - 任一入参为 None / 非数值：返回 0.0
    - leverage ≤ 0：返回 0.0（配置错误由调用方校验，此处不做猜测）
    - contract_size 缺失或 ≤ 0：按 DEFAULT_CONTRACT_SIZE 处理并记估算日志

    Args:
        position_amt: 持仓数量（正负均可，内部取绝对值）
        mark_price: 标记价格
        contract_size: 合约面值（exchangeInfo 获取模型），缺失时按 1 处理
        leverage: 杠杆倍数

    Returns:
        float: 占用保证金（USDT），无效输入返回 0.0
    """
    amt = _to_abs_float(position_amt)
    price = _to_positive_float(mark_price)
    lev = _to_positive_float(leverage)
    if amt is None or price is None or lev is None:
        return 0.0

    size = _to_positive_float(contract_size)
    if size is None:
        # contractSize 缺失：按 1 估算，并记录估算告警便于看板区分精确值/估算值
        logger.warning(
            "contractSize 缺失，按 1 估算保证金",
            position_amt=amt,
            mark_price=price,
            estimated=True,
        )
        size = DEFAULT_CONTRACT_SIZE

    return amt * price * size / lev


def calc_occupied_margin(
    positions: Iterable[Dict[str, Any]],
    leverage: Optional[float],
) -> float:
    """
    计算策略总占用保证金（Σ 单仓保证金，同币种多仓累加不丢数）

    Args:
        positions: 持仓记录可迭代对象，每项需含 positionAmt / markPrice / contractSize
                   （字段缺失视为 0，按约定 positionAmt 与 markPrice 必填）
        leverage: 杠杆倍数

    Returns:
        float: 总占用保证金（USDT）
    """
    total = 0.0
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        total += calc_position_margin(
            pos.get("positionAmt"),
            pos.get("markPrice"),
            pos.get("contractSize"),
            leverage,
        )
    return total


def calc_graded_positions_margin(
    positions: Iterable[Any],
    leverage_config: Optional[Dict[str, Any]],
    default_leverage: float,
    contract_size: float = DEFAULT_CONTRACT_SIZE,
) -> float:
    """
    按「持仓等级 → 杠杆」映射累加占用保证金（MTPCS 两策略共用，全项目唯一实现）

    单仓保证金 = 数量 × 入场价 × contractSize / 该等级杠杆；
    grade 未知或无效时取 leverage_config 中最小杠杆保守高估（防除零）。

    Args:
        positions: 持仓对象可迭代对象，每项需提供 current_quantity / entry_price / grade 属性
        leverage_config: {等级: 杠杆} 映射
        default_leverage: leverage_config 为空时的兜底杠杆
        contract_size: 合约面值（USDT 本位永续合约为 1）

    Returns:
        float: 总占用保证金（USDT）；数量或入场价非正值的仓位跳过
    """
    config = leverage_config or {}
    min_leverage = min(config.values()) if config else default_leverage
    total = 0.0
    for pos in positions:
        quantity = _safe_float(getattr(pos, "current_quantity", None), 0.0)
        entry_price = _safe_float(getattr(pos, "entry_price", None), 0.0)
        if quantity <= 0 or entry_price <= 0:
            continue
        grade = getattr(pos, "grade", None)
        leverage = config.get(grade) if grade in config else min_leverage
        total += calc_position_margin(quantity, entry_price, contract_size, leverage)
    return total


async def check_entry_within_limit(
    strategy: Any,
    symbol: str,
    signal: Dict[str, Any],
    min_leverage: float,
) -> bool:
    """
    MTPCS 开仓限额判定（占用 + 新增 ≤ 生效限额），btc_eth 两策略共用

    新开仓保证金 = |quantity| × entry_price × contractSize / max(leverage, min_leverage)，
    其中 contractSize 取 DEFAULT_CONTRACT_SIZE（USDT 本位永续合约 = 1）。

    Args:
        strategy: MTPCS 策略实例（需提供 _calc_current_total_margin 与 capital_mgr）
        symbol: 交易对
        signal: 交易信号（含 quantity / entry_price / leverage）
        min_leverage: 杠杆下限（防除零兜底）

    Returns:
        bool: True 通过；False 超限拒绝（已记中文告警日志）
    """
    current_total_margin = strategy._calc_current_total_margin()
    new_margin = calc_position_margin(
        signal['quantity'],
        signal['entry_price'],
        DEFAULT_CONTRACT_SIZE,
        max(float(signal['leverage']), min_leverage),
    )
    allowed, reject_reason = await strategy.capital_mgr.can_open_within_limit(
        current_total_margin, new_margin
    )
    if not allowed:
        logger.warning(
            "总持仓保证金超限，跳过开仓",
            symbol=symbol,
            current_margin=round(current_total_margin, 2),
            new_margin=round(new_margin, 2),
            reason=reject_reason,
        )
    return allowed


def build_contract_size_map(exchange_info: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """
    从 exchangeInfo 构建 {symbol: contractSize} 映射

    Args:
        exchange_info: exchangeInfo 响应字典（含 symbols 列表）

    Returns:
        Dict[str, float]: 合约面值映射；缺失 contractSize 的币种不纳入（由计算时按 1 兜底）
    """
    result: Dict[str, float] = {}
    if not isinstance(exchange_info, dict):
        return result
    for item in exchange_info.get("symbols", []) or []:
        symbol = item.get("symbol")
        size = _to_positive_float(item.get("contractSize"))
        if symbol and size is not None:
            result[symbol] = size
    return result


def build_short_positions(
    exchange_positions: Optional[List[Dict[str, Any]]],
    leverage: Optional[float],
    contract_sizes: Optional[Dict[str, float]] = None,
) -> Dict[str, RebuiltPosition]:
    """
    将交易所 positionRisk 记录解析为 {symbol: RebuiltPosition}（仅做空，同币种累加）

    处理规则：
    - 空列表 ⇒ PM 账户零持仓（已知坑），返回空字典
    - positionAmt < 0 ⇒ 视为空头；|positionAmt| 为数量
    - entry_price 优先取 entryPrice，缺失时取 markPrice 并标记 estimated_entry_price
    - 同币种多次出现 ⇒ 数量与保证金累加，入场价按数量加权平均

    Args:
        exchange_positions: positionRisk 返回的持仓列表
        leverage: 杠杆倍数
        contract_sizes: {symbol: contractSize} 映射，缺失时按 1 处理

    Returns:
        Dict[str, RebuiltPosition]: 做空持仓基线（key 为 symbol）
    """
    result: Dict[str, RebuiltPosition] = {}
    sizes = contract_sizes or {}

    for pos in exchange_positions or []:
        if not isinstance(pos, dict):
            continue
        symbol = pos.get("symbol")
        raw_amt = pos.get("positionAmt")
        amt = _to_abs_float(raw_amt)
        # 仅处理空头（positionAmt < 0）；无法解析数量的记录跳过
        if not symbol or amt is None or _safe_float(raw_amt, 0.0) >= 0:
            continue

        mark_price = _to_positive_float(pos.get("markPrice")) or 0.0
        raw_entry = _to_positive_float(pos.get("entryPrice"))
        estimated_entry = raw_entry is None
        entry_price = raw_entry if raw_entry is not None else mark_price

        contract_size = sizes.get(symbol)
        estimated_size = _to_positive_float(contract_size) is None
        margin = calc_position_margin(amt, mark_price, contract_size, leverage)

        if symbol in result:
            result[symbol] = _merge_position(
                result[symbol], amt, entry_price, margin, estimated_entry, estimated_size
            )
        else:
            result[symbol] = RebuiltPosition(
                symbol=symbol,
                quantity=amt,
                entry_price=entry_price,
                margin=margin,
                estimated_contract_size=estimated_size,
                estimated_entry_price=estimated_entry,
            )

    return result


async def rebuild_from_exchange(
    binance_client: Any,
    leverage: Optional[float],
    contract_sizes: Optional[Dict[str, float]] = None,
) -> BaselineRebuildResult:
    """
    从交易所 positionRisk 重建做空持仓基线（启动 / 重启时调用）

    Args:
        binance_client: BinanceClient 实例（需提供 get_position / get_exchange_info）
        leverage: 杠杆倍数
        contract_sizes: 预先获取的 {symbol: contractSize} 映射；
                        为 None 时内部调用 exchangeInfo 获取（失败则按 1 估算）

    Returns:
        BaselineRebuildResult: success=False 表示交易所接口失败，调用方需保守处理
    """
    try:
        exchange_positions = await binance_client.get_position()
    except Exception as e:
        logger.error("基线重建失败：获取交易所持仓异常", error=str(e), exc_info=True)
        return BaselineRebuildResult(success=False, positions={}, total_margin=0.0)

    sizes = contract_sizes
    if sizes is None:
        sizes = await _load_contract_sizes(binance_client)

    positions = build_short_positions(exchange_positions, leverage, sizes)
    total_margin = sum(p.margin for p in positions.values())

    # 空列表 ⇒ PM 账户零持仓（已知坑），此处显式记录，便于排查"占用被记为 0"问题
    if not positions:
        logger.info("基线重建完成：交易所无做空持仓（PM 空列表视为零持仓）", leverage=leverage)
    else:
        logger.info(
            "基线重建完成",
            position_count=len(positions),
            total_margin=round(total_margin, 4),
            symbols=list(positions.keys()),
        )

    return BaselineRebuildResult(success=True, positions=positions, total_margin=total_margin)


async def _load_contract_sizes(binance_client: Any) -> Dict[str, float]:
    """
    通过 exchangeInfo 加载 {symbol: contractSize} 映射（失败时返回空字典）

    Args:
        binance_client: BinanceClient 实例

    Returns:
        Dict[str, float]: 合约面值映射；接口异常时返回空字典（后续按 1 估算）
    """
    try:
        exchange_info = await binance_client.get_exchange_info()
        return build_contract_size_map(exchange_info)
    except Exception as e:
        logger.warning("获取 exchangeInfo 失败，contractSize 将按 1 估算", error=str(e))
        return {}


def _merge_position(
    existing: RebuiltPosition,
    quantity: float,
    entry_price: float,
    margin: float,
    estimated_entry: bool,
    estimated_size: bool,
) -> RebuiltPosition:
    """
    合并同币种多仓（数量/保证金累加，入场价按数量加权平均）

    Args:
        existing: 已存在的持仓基线
        quantity: 新增数量
        entry_price: 新增部分入场价
        margin: 新增部分保证金
        estimated_entry: 新增部分入场价是否为估算值
        estimated_size: 新增部分 contractSize 是否为估算值

    Returns:
        RebuiltPosition: 合并后的持仓基线
    """
    total_qty = existing.quantity + quantity
    if total_qty > 0:
        weighted = (existing.entry_price * existing.quantity + entry_price * quantity) / total_qty
    else:
        weighted = existing.entry_price
    return RebuiltPosition(
        symbol=existing.symbol,
        quantity=total_qty,
        entry_price=weighted,
        margin=existing.margin + margin,
        estimated_contract_size=existing.estimated_contract_size or estimated_size,
        estimated_entry_price=existing.estimated_entry_price or estimated_entry,
    )


def _to_positive_float(value: Any) -> Optional[float]:
    """
    将任意输入安全转换为正浮点数（0 与负值按 None 处理）

    Args:
        value: 待转换值（可能是 None / 字符串 / Decimal / float）

    Returns:
        Optional[float]: 成功返回正浮点数；None / 非数值 / ≤0 返回 None
    """
    result = _safe_float(value, None)
    if result is None or result <= 0:
        return None
    return result


def _to_abs_float(value: Any) -> Optional[float]:
    """
    将任意输入安全转换为绝对值浮点数（用于持仓数量，做空为负值）

    Args:
        value: 待转换值（可能是 None / 字符串 / Decimal / float）

    Returns:
        Optional[float]: 成功返回非零绝对值；None / 非数值 / 0 返回 None
    """
    result = _safe_float(value, None)
    if result is None:
        return None
    result = abs(result)
    return result if result > 0 else None


def _safe_float(value: Any, default: Any) -> Any:
    """
    将任意输入安全转换为浮点数（失败时返回默认值）

    Args:
        value: 待转换值（可能是 None / 字符串 / Decimal / float）
        default: 转换失败时的返回值

    Returns:
        Any: 转换成功返回 float，否则返回 default
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default