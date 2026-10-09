"""features.py 单元测试：整秒采样、效率、滚动特征（含全部 None 分支）。"""
from decimal import Decimal

from strategies.grid.realtime.features import (FeatureSlice,
                                             SecondSample, compute_features,
                                             efficiency, resampled_q,
                                             sample_from_trades)
from factories import make_trade


# ───────────────────── sample_from_trades ─────────────────────

def test_sample_basic():
    trades = [make_trade(1, 1000, "100.0"), make_trade(2, 2000, "101.0")]
    times = [t.trade_time_ms for t in trades]
    ids = [t.agg_trade_id for t in trades]
    prices = [t.price for t in trades]
    s = sample_from_trades(times, ids, prices, 2000, 2000)
    assert s == SecondSample(2000, Decimal("101.0"), 2000, 2)


def test_sample_no_anchor():
    s = sample_from_trades([2000], [1], [Decimal("100")], 1000, 2000)
    assert s == SecondSample(1000, None, None, None)


def test_sample_gap_exceeded_is_none_with_anchor_metadata():
    s = sample_from_trades([1000], [1], [Decimal("100.0")], 3500, 2000)
    assert s.price is None
    assert s.anchor_time_ms == 1000 and s.anchor_trade_id == 1


def test_sample_gap_boundary_inclusive():
    # 锚距恰等于上限：价格仍有效（<= 规则）
    s = sample_from_trades([1000], [1], [Decimal("100")], 3000, 2000)
    assert s.price == Decimal("100")


def test_sample_same_millisecond_last_id_wins():
    trades = [make_trade(1, 1000, "100.0"), make_trade(2, 1000, "100.5")]
    times = [t.trade_time_ms for t in trades]
    s = sample_from_trades(times, [1, 2], [t.price for t in trades], 1000, 2000)
    assert s.price == Decimal("100.5") and s.anchor_trade_id == 2


# ───────────────────── efficiency ─────────────────────

def test_efficiency_one_way():
    q = [Decimal(1), Decimal(2), Decimal(3)]
    assert efficiency(q) == Decimal(1)


def test_efficiency_fractional():
    q = [Decimal(1), Decimal(3), Decimal(2)]
    # |2-1| / (2+1) = 1/3
    assert efficiency(q) == Decimal(1) / Decimal(3)


def test_efficiency_none_point():
    assert efficiency([Decimal(1), None, Decimal(2)]) is None


def test_efficiency_zero_denominator():
    assert efficiency([Decimal(1), Decimal(1), Decimal(1)]) == Decimal(0)


# ───────────────────── resampled_q ─────────────────────

def test_resampled_q_counts():
    prices = {1000 + i: Decimal(i) for i in range(301)}
    lookup = prices.get
    assert len(resampled_q(lookup, 3100, 300, 1)) == 301
    assert len(resampled_q(lookup, 3100, 300, 3)) == 101
    assert len(resampled_q(lookup, 3100, 300, 5)) == 61


def test_resampled_q_anchors_window_edges():
    prices = {i * 1000: Decimal(i) for i in range(0, 4001)}
    q = resampled_q(prices.get, 3_000_000, 300, 3)
    assert q[0] == Decimal(2700) and q[-1] == Decimal(3000)


def test_resampled_q_missing_points_are_none():
    q = resampled_q({}.get, 10000, 300, 5)
    assert all(v is None for v in q)


# ───────────────────── compute_features ─────────────────────

def _price_map(base_ms: int, base_price: str, step: str = "1.0"):
    prices = {}
    for i in range(301):
        prices[base_ms + i * 1000] = Decimal(base_price) + Decimal(step) * i
    return prices


def test_compute_features_returns():
    prices = _price_map(0, "100.0")
    feat = compute_features(prices.get, 300000, (60, 180, 300), 1, (300,))
    assert isinstance(feat, FeatureSlice)
    # 线性 100+i：t=300s → 400；r60=400/340-1；r300=400/100-1=3
    assert feat.price == Decimal("400.0")
    assert feat.r[60] == Decimal(400) / Decimal(340) - 1
    assert feat.r[300] == Decimal("3")
    # 单调上行 → E=1
    assert feat.e[300] == Decimal(1)


def test_compute_features_ref_none():
    prices = _price_map(0, "100.0")
    feat = compute_features(prices.get, 100000, (60, 180, 300), 1, (300,))
    assert feat.r[300] is None  # t-300s 早于首点
    assert feat.e[300] is None
    assert feat.r[60] is not None


def test_compute_features_price_none():
    def lookup(_):
        return None
    feat = compute_features(lookup, 1000, (60,), 1, (300,))
    assert feat.price is None and feat.r[60] is None and feat.e[300] is None


def test_compute_features_efficiency_none_on_gap():
    prices = _price_map(0, "100.0")
    prices[200000] = None
    feat = compute_features(prices.get, 300000, (60,), 1, (300,))
    assert feat.e[300] is None  # 窗口内存在 None 点
