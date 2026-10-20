"""撮合模块测试：限价穿越 / 未成交 / 市价滑点 / tick-step 舍入。"""
from decimal import Decimal
import pytest

from backtest.btc_eth.replay.matching import (
    q_round, round_price, round_quantity, clamp_notional,
    price_crosses_limit, market_fill_price,
    find_first_crossing_index, kline_is_gap,
)


def test_q_round_down():
    assert q_round(Decimal("123.45"), Decimal("0.1")) == Decimal("123.4")
    assert q_round(Decimal("123.49"), Decimal("0.1")) == Decimal("123.4")
    assert q_round(Decimal("123.0001"), Decimal("0.001")) == Decimal("123.000")


def test_q_round_half_up():
    # 5.5 → 6 (HALF_UP)
    assert round_price(Decimal("123.456"), Decimal("0.01")) == Decimal("123.46")
    # 4.001 → 4.00
    assert round_price(Decimal("4.001"), Decimal("0.01")) == Decimal("4.00")


def test_round_quantity_down():
    # stepSize=0.001: 4.9999 → 4.999
    assert round_quantity(Decimal("4.9999"), Decimal("0.001")) == Decimal("4.999")
    # stepSize=0.1: 4.1 → 4.1
    assert round_quantity(Decimal("4.1"), Decimal("0.1")) == Decimal("4.1")


def test_clamp_notional_noop():
    qty = Decimal("1")
    price = Decimal("100")
    # notional = 100 >= minNotional 5
    assert clamp_notional(qty, price, Decimal("5")) == Decimal("1")


def test_clamp_notional_boost():
    qty = Decimal("0.01")
    price = Decimal("100")
    # notional = 1 < minNotional 5 → 需要向上到 0.05
    boosted = clamp_notional(qty, price, Decimal("5"))
    # 向上整 → 最小 notional/price = 5/100 = 0.05 向上取整还是 Decimal 整数
    expected = (Decimal("5") / Decimal("100")).to_integral_value(rounding="ROUND_UP")
    assert boosted == expected
    assert boosted * price >= Decimal("5")


def test_price_crosses_limit_long():
    # LONG 限价 1990，某根 K 线 low=1985, high=2010 → 应该穿越
    assert price_crosses_limit(
        Decimal("1990"), Decimal("2000"), Decimal("2010"), Decimal("1985"), "LONG"
    ) is True
    # LONG 限价 1990，low=1995 → 不穿越
    assert price_crosses_limit(
        Decimal("1990"), Decimal("2000"), Decimal("2010"), Decimal("1995"), "LONG"
    ) is False


def test_price_crosses_limit_short():
    # SHORT 限价 2010，high=2015 → 穿越
    assert price_crosses_limit(
        Decimal("2010"), Decimal("2000"), Decimal("2015"), Decimal("1990"), "SHORT"
    ) is True
    # SHORT 限价 2010，high=2008 → 不穿越
    assert price_crosses_limit(
        Decimal("2010"), Decimal("2000"), Decimal("2008"), Decimal("1990"), "SHORT"
    ) is False


def test_market_fill_price_slip_2bps():
    bps = Decimal("2")
    open_p = Decimal("2000")
    # LONG 多付 2 bps → 2000 * 1.0002 = 2000.40
    assert market_fill_price(open_p, bps, "LONG") == Decimal("2000.40")
    # SHORT 少收 2 bps → 2000 * 0.9998 = 1999.60
    assert market_fill_price(open_p, bps, "SHORT") == Decimal("1999.60")


def test_direction_raises():
    with pytest.raises(ValueError):
        price_crosses_limit(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), "INVALID")
    with pytest.raises(ValueError):
        market_fill_price(Decimal("100"), Decimal("2"), "INVALID")


def test_q_round_zero_step_raises():
    """stepSize / tickSize <= 0 是非法配置 → 必须抛 ValueError（fail-fast）。"""
    with pytest.raises(ValueError):
        q_round(Decimal("123.45"), Decimal("0"))
    with pytest.raises(ValueError):
        q_round(Decimal("123.45"), Decimal("-0.01"))


def _mkline(low, high):
    return {"open": "2000", "high": str(high), "low": str(low), "close": "2000", "volume": "1"}


def test_find_first_crossing_index_hits_and_misses():
    """窗口内首次穿越索引正确；全程未穿越返回 None（未成交语义）。"""
    klines = [_mkline(Decimal("1999"), Decimal("2001")) for _ in range(3)]
    klines.append(_mkline(Decimal("1985"), Decimal("2000")))  # 第 4 根 low 穿越限价 1990
    klines.append(_mkline(Decimal("1980"), Decimal("2000")))

    idx = find_first_crossing_index(
        klines_1m=klines, limit_price=Decimal("1990"), direction="LONG",
        search_start_idx=0, search_end_idx=len(klines),
    )
    assert idx == 3

    # 窗口含头不含尾：搜索 [0, 3) 时穿越点(索引3)不在窗口内 → None
    idx2 = find_first_crossing_index(
        klines_1m=klines, limit_price=Decimal("1990"), direction="LONG",
        search_start_idx=0, search_end_idx=3,
    )
    assert idx2 is None

    # 从索引 4 起搜索：第 4 根 low=1980 同样穿越 → 返回 4（验证窗口起点语义）
    idx3 = find_first_crossing_index(
        klines_1m=klines, limit_price=Decimal("1990"), direction="LONG",
        search_start_idx=4, search_end_idx=len(klines),
    )
    assert idx3 == 4

    # 全部 K 线 low 均高于限价 → None（限价单未穿越到数据末尾 → 未成交）
    above = [_mkline(Decimal("1999"), Decimal("2001")) for _ in range(3)]
    idx_none = find_first_crossing_index(
        klines_1m=above, limit_price=Decimal("1990"), direction="LONG",
        search_start_idx=0, search_end_idx=len(above),
    )
    assert idx_none is None


def test_kline_is_gap_volume_zero_or_invalid():
    """volume=0 视为流动性空洞；非法 volume 字段 fail-closed 判为 gap。"""
    assert kline_is_gap({"volume": "0"}) is True
    assert kline_is_gap({"volume": "100"}) is False
    assert kline_is_gap({}) is True          # 缺 volume → 默认 0 → gap
    assert kline_is_gap({"volume": "abc"}) is True  # 非法数值 → 判 gap（保守跳过）
