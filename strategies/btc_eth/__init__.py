"""
BTC/ETH/BNB交易策略
基于评分引擎的趋势跟踪策略
"""

__version__ = "1.0.0"
__strategy_name__ = "btc_eth_trend"

from .strategy import BTCEthStrategy
from .main import main

__all__ = ["BTCEthStrategy", "main"]
