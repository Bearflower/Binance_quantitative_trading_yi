"""Binance USDⓈ-M exchangeInfo 的 MIN_NOTIONAL 字段解析回归。"""
from decimal import Decimal

from backtest.btc_eth.replay.engine import ReplayEngine
from backtest.btc_eth.replay.matching import parse_symbol_filters


def test_official_notional_field_passes_engine_guard_and_parser():
    symbol = {
        "symbol": "XAUUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "7"},
        ],
    }
    selected = ReplayEngine._find_symbol_entry({"symbols": [symbol]}, "XAUUSDT")
    assert parse_symbol_filters(selected)["minNotional"] == Decimal("7")
