"""grid realtime 测试构造器（独立命名模块）。

历史上构造器定义在 conftest.py 中，测试以 ``from conftest import ...``
引用；全量收集时多个无包名 conftest 模块共享同一 sys.modules 名，
会被其他目录的 conftest 抢占导致 ImportError。
改用唯一名模块，保证全量收集与单目录收集行为一致。
"""
from decimal import Decimal

from strategies.grid.realtime.features import AggTrade, FeatureSlice


def make_trade(tid: int, t: int, price: str, qty: str = "0.1",
               maker: int = 0) -> AggTrade:
    return AggTrade(tid, Decimal(price), Decimal(qty), t, maker)


def make_feature(price=None, r=None, e=None) -> FeatureSlice:
    return FeatureSlice(price, r or {}, e or {})
