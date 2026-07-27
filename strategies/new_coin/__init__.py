"""
新币做空策略
针对新上市币种的做空策略，利用新币上市后的价格下跌趋势获利
"""

__version__ = "1.0.0"
__strategy_name__ = "new_coin_short"

from .strategy import NewCoinStrategy
from .main import main

__all__ = ["NewCoinStrategy", "main"]
