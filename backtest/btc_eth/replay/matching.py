"""
撮合模块：限价穿越判定、市价保守滑点、tickSize / stepSize / minNotional 舍入。

仅含纯函数，无副作用，便于测试。价格数量全部用 Decimal。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Dict, Any, List


def q_round(value: Decimal, step: Decimal, rounding=ROUND_DOWN) -> Decimal:
    """按 stepSize / tickSize 向下（或指定舍入模式）舍入到最近步长。

    例：step=0.1, value=123.45 → 123.4
    step=0.01, value=123.456 → 123.45
    step 必须 > 0。
    """
    if step <= 0:
        raise ValueError(f"step 必须 > 0，收到 step={step}")
    # Decimal 原生 quantize 对"任意步长"不可靠（只能 quantize 到 0.01, 0.001 这种幂次形式），
    # 所以用整除 + 乘法的方式做通用舍入。
    scaled = (value / step).to_integral_value(rounding=rounding)
    return scaled * step


def round_price(price: Decimal, tick_size: Decimal) -> Decimal:
    """价格按 tickSize 舍入：向上取整（买入限价挂单不会因舍入过大而永远不成交）。
    卖出限价同理：保守口径用 HALF_UP 或直接按 step 舍入即可。
    这里统一用 ROUND_HALF_UP 对齐 Binance 实际舍入习惯。
    """
    return q_round(price, tick_size, rounding=ROUND_HALF_UP)


def round_quantity(quantity: Decimal, step_size: Decimal) -> Decimal:
    """数量按 stepSize 舍入：向下取整（保证不会因舍入过大触发 minNotional 检查失败）。"""
    return q_round(quantity, step_size, rounding=ROUND_DOWN)


def clamp_notional(quantity: Decimal, price: Decimal, min_notional: Decimal) -> Decimal:
    """若 quantity * price 低于 minNotional，返回使 notional 恰等于 minNotional
    的最小 quantity（向上）。
    否则原样返回。用于撮合前的最小名义价值检查。
    """
    notional = quantity * price
    if notional >= min_notional or min_notional <= 0:
        return quantity
    # 向上取整到 min_notional / price
    # 先不考虑 stepSize 舍入——由调用方随后再 round_quantity。
    from decimal import ROUND_UP
    min_qty = (min_notional / price).to_integral_value(rounding=ROUND_UP)
    return min_qty


def price_crosses_limit(
    limit_price: Decimal,
    kline_open: Decimal,      # 接口兼容：历史签名保留，函数体不使用
    kline_high: Decimal,
    kline_low: Decimal,
    direction: str,
) -> bool:
    """判断一根 K 线内是否穿越限价：以 1m K 线的 high / low 作为穿越的充分判据。

    - LONG 方向限价买入：任何 low <= limit_price → 视为成交（保守：不会漏过穿越）。
    - SHORT 方向限价卖出：任何 high >= limit_price → 视为成交。
    完全不考虑时间顺序——单根 1m 不做更细粒度的 tick 推断。
    """
    if direction == "LONG":
        return kline_low <= limit_price
    if direction == "SHORT":
        return kline_high >= limit_price
    raise ValueError(f"不支持的 direction: {direction}")


def market_fill_price(
    kline_open: Decimal,
    slippage_bps: Decimal,
    direction: str,
) -> Decimal:
    """市价意图按下一根 1m K 线开盘价 + 保守滑点成交。

    LONG 方向：成交价 = open * (1 + slippage_bps/10000)
    SHORT 方向：成交价 = open * (1 - slippage_bps/10000)
    slippage_bps 为 Decimal，2 表示 2 bps。
    """
    bps = slippage_bps / Decimal("10000")
    if direction == "LONG":
        return kline_open * (Decimal("1") + bps)
    if direction == "SHORT":
        return kline_open * (Decimal("1") - bps)
    raise ValueError(f"不支持的 direction: {direction}")


# ---------------- 辅助：从 Binance exchangeInfo 解析 minNotional / stepSize / tickSize ----------------

def parse_symbol_filters(exchange_info_symbol: Dict[str, Any]) -> Dict[str, Decimal]:
    """从 Binance exchangeInfo 的单个 symbol 条目里抽取 PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL。

    返回：{"tickSize": Decimal, "stepSize": Decimal, "minNotional": Decimal}
    Binance USDⓈ-M 合约 exchangeInfo 里：
      - PRICE_FILTER -> tickSize
      - LOT_SIZE -> stepSize
      - MIN_NOTIONAL -> minNotional（名义价值下限，USDT）
    """
    filters = {f["filterType"]: f for f in exchange_info_symbol.get("filters", [])}
    pf = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    min_notional_filter = filters.get("MIN_NOTIONAL", {})
    return {
        "tickSize": Decimal(str(pf.get("tickSize", "0.01"))),
        "stepSize": Decimal(str(lot.get("stepSize", "0.001"))),
        "minNotional": Decimal(str(min_notional_filter.get("minNotional", "5.0"))),
    }


# ---------------- 1m K 线窗口查找 ----------------

def find_first_crossing_index(
    klines_1m: List[Dict[str, Any]],
    limit_price: Decimal,
    direction: str,
    search_start_idx: int,
    search_end_idx: int,
) -> Optional[int]:
    """在 klines_1m[search_start_idx : search_end_idx]（含头不含尾）范围内，
    返回第一次满足 price_crosses_limit 的索引；没穿越返回 None。
    """
    for i in range(search_start_idx, search_end_idx):
        k = klines_1m[i]
        if price_crosses_limit(
            limit_price=limit_price,
            kline_open=Decimal(str(k["open"])),
            kline_high=Decimal(str(k["high"])),
            kline_low=Decimal(str(k["low"])),
            direction=direction,
        ):
            return i
    return None


def kline_is_gap(kline: Dict[str, Any]) -> bool:
    """Binance TradFi 非活跃时段可能出现长时间缺 K 线。
    最简单判别：volume 为 0 或 open == high == low == close 且 volume == 0，
    视为"流动性空洞"。撮合应跳过（不视为未成交、直接推进到下一根）。
    这里作为辅助函数，调用方可按需使用。
    """
    try:
        v = Decimal(str(kline.get("volume", "0")))
        return v == 0
    except (InvalidOperation, TypeError):
        return True
