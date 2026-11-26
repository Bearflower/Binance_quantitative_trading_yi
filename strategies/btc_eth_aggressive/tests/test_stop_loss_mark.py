"""激进版与交易所同步持仓。"""
from decimal import Decimal
from types import SimpleNamespace

from strategies.btc_eth_aggressive.strategy import BTCEthStrategy
from unittest.mock import AsyncMock, MagicMock


def make_strategy() -> BTCEthStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需 mock 属性。"""
    strategy = object.__new__(BTCEthStrategy)
    return strategy


def make_position(direction: str = "LONG", quantity: Decimal = Decimal("10")):
    """构造含 direction / current_quantity 的最小持仓桩。"""
    return SimpleNamespace(direction=direction, current_quantity=quantity)


async def test_持仓同步_交易所空列表视为已平仓():
    """PM 账户对已平仓 symbol 返回空列表，应标记 closed 并清零本地持仓。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is True
    assert result["partially_closed"] is False
    assert pos.current_quantity == Decimal("0")
    assert pos.direction == "FLAT"


async def test_持仓同步_posAmt为零视为已平仓():
    """显式返回 posAmt≈0 的边缘情况，同样标记 closed。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "0"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is True
    assert pos.current_quantity == Decimal("0")
    assert pos.direction == "FLAT"


async def test_持仓同步_部分平仓个数同步():
    """实际持仓小于本地记录时，标记 partially_closed 并同步数量。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "6"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is True
    assert result["actual_quantity"] == Decimal("6")
    assert pos.current_quantity == Decimal("6")


async def test_持仓同步_持仓量正常():
    """实际多仓等于本地多仓时，状态正常不改变。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(return_value=[{"symbol": "BTCUSDT", "positionAmt": "10"}])
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is False
    assert result["actual_quantity"] == Decimal("10")
    assert pos.current_quantity == Decimal("10")


async def test_持仓同步_查询异常返回None不抛错():
    """查询交易所持仓异常时应返回 actual_quantity=None 且不抛错。"""
    strategy = make_strategy()
    strategy.binance = MagicMock()
    strategy.binance.get_position = AsyncMock(side_effect=RuntimeError("网络异常"))
    pos = make_position("LONG", Decimal("10"))
    result = await strategy._sync_position_with_exchange("BTCUSDT", pos)
    assert result["closed"] is False
    assert result["partially_closed"] is False
    assert result["actual_quantity"] is None