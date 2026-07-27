"""
回测交易所模拟器
提供 BinanceClient 和 KLineService 兼容接口（同步版本），用于回测环境下的订单模拟和数据访问。

核心功能：
- BinanceClient 兼容接口：get_ticker_price、place_order、cancel_order、get_open_orders、get_order、get_account_balance
- KLineService 兼容接口：get_klines、get_multi_timeframe_data
- 订单模拟器 OrderSimulator：订单生命周期管理、价格穿越自动成交
- 回测专用方法：load_all_data、advance_to，严格防止未来函数
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING, Dict, List, Optional, Any

if TYPE_CHECKING:
    from .data_loader import DataLoader

logger = logging.getLogger(__name__)


# ============================================================================
# 订单模拟器
# ============================================================================

class OrderSimulator:
    """
    订单模拟器

    管理回测环境中的订单生命周期，包括：
    - 下单时生成唯一订单ID
    - 价格穿越订单价时自动成交
    - 撤销订单
    - 记录所有已成交订单

    所有操作均为同步方法，适配回测引擎的顺序执行模式。
    """

    def __init__(self):
        """初始化订单模拟器"""
        self._order_counter: int = 0
        # 待成交订单：{order_id: order_dict}
        self._pending_orders: Dict[int, Dict[str, Any]] = {}
        # 已成交订单列表
        self.filled_orders: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _generate_order_id(self) -> int:
        """
        生成唯一订单ID

        使用自增计数器生成，保证回测环境中的订单ID唯一性。
        """
        self._order_counter += 1
        return self._order_counter

    def _convert_to_decimal(self, value: Any) -> Decimal:
        """
        将输入值转换为 Decimal 类型

        Args:
            value: 数值（int、float、str、Decimal）

        Returns:
            Decimal 类型的值
        """
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: Any,
        price: Any,
        order_type: str = "LIMIT",
        **kwargs
    ) -> Dict[str, Any]:
        """
        下单（模拟）

        在回测环境中创建模拟订单，生成唯一订单ID并加入待成交队列。

        Args:
            symbol:   交易对名称，如 "ETHUSDT"
            side:     订单方向，"BUY" 或 "SELL"
            quantity: 下单数量
            price:    订单价格（限价单必填）
            order_type: 订单类型，默认 "LIMIT"
            **kwargs: 其他参数（如 timeInForce，回测中忽略）

        Returns:
            订单信息字典，包含 orderId、symbol、side、price、origQty、status、type 等字段

        Raises:
            ValueError: 参数验证失败
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        symbol = symbol.strip().upper()
        side = side.strip().upper()

        if side not in ("BUY", "SELL"):
            raise ValueError(f"无效的订单方向: {side}，必须是 BUY 或 SELL")

        qty = self._convert_to_decimal(quantity)
        if qty <= Decimal("0"):
            raise ValueError(f"数量必须大于0: {qty}")

        price_dec = self._convert_to_decimal(price) if price is not None else None
        if order_type == "LIMIT":
            if price_dec is None:
                raise ValueError("限价单必须提供价格")
            if price_dec <= Decimal("0"):
                raise ValueError(f"价格必须大于0: {price_dec}")

        order_id = self._generate_order_id()
        now_iso = datetime.now(timezone.utc).isoformat()

        order = {
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": price_dec,
            "origQty": qty,
            "executedQty": Decimal("0"),
            "status": "NEW",
            "timeInForce": kwargs.get("timeInForce", "GTC"),
            "clientOrderId": kwargs.get("newClientOrderId", f"sim_{order_id}"),
            "transactTime": now_iso,
            "created_at": now_iso,
        }

        self._pending_orders[order_id] = order

        logger.debug(
            "模拟下单成功",
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=float(price_dec) if price_dec else None,
            quantity=float(qty),
        )

        return order

    def check_fills(
        self,
        current_price: Decimal,
        current_time: str
    ) -> List[Dict[str, Any]]:
        """
        检查并执行订单成交

        遍历所有待成交订单，当当前价格穿越订单价格时自动成交：
        - 买单：current_price <= order.price 时成交
        - 卖单：current_price >= order.price 时成交

        Args:
            current_price: 当前K线收盘价（Decimal）
            current_time:  当前时间字符串

        Returns:
            本轮新成交的订单列表
        """
        newly_filled: List[Dict[str, Any]] = []
        orders_to_remove: List[int] = []

        for order_id, order in self._pending_orders.items():
            if order["status"] != "NEW":
                continue

            side = order["side"]
            order_price = order["price"]
            should_fill = False

            if side == "BUY" and current_price <= order_price:
                should_fill = True
            elif side == "SELL" and current_price >= order_price:
                should_fill = True

            if should_fill:
                # 标记成交
                order["status"] = "FILLED"
                order["executedQty"] = order["origQty"]
                # 回测中成交价即为挂单价（不考虑滑点，滑点由回测引擎层处理）
                order["execution_price"] = order_price
                order["filled_at"] = current_time

                self.filled_orders.append(order)
                orders_to_remove.append(order_id)
                newly_filled.append(order)

                logger.debug(
                    "模拟订单成交",
                    order_id=order_id,
                    symbol=order["symbol"],
                    side=side,
                    order_price=float(order_price),
                    current_price=float(current_price),
                    quantity=float(order["origQty"]),
                )

        # 从待成交列表中移除已成交订单
        for order_id in orders_to_remove:
            del self._pending_orders[order_id]

        return newly_filled

    def cancel_order(self, symbol: str, order_id: Any) -> Dict[str, Any]:
        """
        撤销指定订单

        Args:
            symbol:   交易对名称
            order_id: 订单ID

        Returns:
            被撤销的订单信息字典

        Raises:
            ValueError: 订单不存在
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        symbol = symbol.strip().upper()
        order_id_int = int(order_id)

        if order_id_int not in self._pending_orders:
            raise ValueError(f"订单不存在或已成交: order_id={order_id_int}")

        order = self._pending_orders.pop(order_id_int)
        order["status"] = "CANCELED"
        order["canceled_at"] = datetime.now(timezone.utc).isoformat()

        # 取消的订单也记录到已成交列表（便于审计）
        self.filled_orders.append(order)

        logger.debug(
            "模拟撤单成功",
            order_id=order_id_int,
            symbol=symbol,
        )

        return order

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        撤销所有未成交订单

        Args:
            symbol: 交易对（可选，不指定则撤销所有交易对的订单）

        Returns:
            成功撤销的订单数量
        """
        cancelled_count = 0
        orders_to_cancel: List[int] = []

        for order_id, order in self._pending_orders.items():
            if symbol and order["symbol"] != symbol.strip().upper():
                continue
            orders_to_cancel.append(order_id)

        for order_id in orders_to_cancel:
            order = self._pending_orders.pop(order_id)
            order["status"] = "CANCELED"
            order["canceled_at"] = datetime.now(timezone.utc).isoformat()
            self.filled_orders.append(order)
            cancelled_count += 1

        logger.debug(
            "批量撤单完成",
            cancelled_count=cancelled_count,
            symbol=symbol or "所有交易对",
        )

        return cancelled_count

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前未成交订单

        Args:
            symbol: 交易对（可选）

        Returns:
            未成交订单列表
        """
        if symbol:
            sym = symbol.strip().upper()
            return [o for o in self._pending_orders.values() if o["symbol"] == sym]
        return list(self._pending_orders.values())

    def get_order(self, symbol: str, order_id: Any) -> Dict[str, Any]:
        """
        查询指定订单详情

        先在待成交订单中查找，再在已成交订单中查找。

        Args:
            symbol:   交易对名称
            order_id: 订单ID

        Returns:
            订单信息字典

        Raises:
            ValueError: 订单不存在
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        order_id_int = int(order_id)

        # 先在待成交订单中查找
        if order_id_int in self._pending_orders:
            return self._pending_orders[order_id_int]

        # 再在已成交订单中查找
        for order in self.filled_orders:
            if order["orderId"] == order_id_int:
                return order

        raise ValueError(f"订单不存在: order_id={order_id_int}")

    def get_pending_count(self, symbol: Optional[str] = None) -> int:
        """
        获取待成交订单数量

        Args:
            symbol: 交易对（可选）

        Returns:
            待成交订单数量
        """
        if symbol:
            sym = symbol.strip().upper()
            return sum(1 for o in self._pending_orders.values() if o["symbol"] == sym)
        return len(self._pending_orders)

    def get_filled_count(self) -> int:
        """获取已成交订单总数"""
        # 只统计状态为 FILLED 的
        return sum(1 for o in self.filled_orders if o.get("status") == "FILLED")


# ============================================================================
# 交易所模拟器
# ============================================================================

class ExchangeSimulator:
    """
    回测交易所模拟器

    提供两个核心兼容接口：
    1. BinanceClient 兼容接口（同步版本）：模拟真实交易所的下单、撤单、查余额等操作
    2. KLineService 兼容接口（同步版本）：提供历史K线数据访问，严格防止未来函数

    回测专用功能：
    - load_all_data()：从 DataLoader 预加载所有时间框架数据
    - advance_to(index)：推进到指定1h K线索引，更新当前时间和价格
    - 所有 get_klines 调用只返回 current_time 之前的数据，杜绝未来函数
    """

    def __init__(
        self,
        data_loader: "DataLoader",
        initial_balance: Decimal = Decimal("10000"),
        commission_rate: Decimal = Decimal("0.0004"),
    ):
        """
        初始化回测交易所模拟器

        Args:
            data_loader:     数据加载器实例（已完成配置）
            initial_balance: 初始账户余额（USDT）
            commission_rate: 手续费率（如 0.0004 表示 0.04%）

        Raises:
            ValueError: data_loader 参数无效
        """
        # 使用鸭子类型校验，避免循环依赖和独立加载时的导入问题
        required_attrs = ("symbol", "load_multi_timeframe_data")
        if not (all(hasattr(data_loader, attr) for attr in required_attrs)):
            raise ValueError(
                f"data_loader 缺少必要属性 {required_attrs}，"
                f"实际类型为 {type(data_loader).__name__}"
            )

        if initial_balance <= Decimal("0"):
            raise ValueError(f"初始余额必须大于0: {initial_balance}")

        if commission_rate < Decimal("0") or commission_rate >= Decimal("1"):
            raise ValueError(f"手续费率必须在 [0, 1) 范围内: {commission_rate}")

        self._data_loader = data_loader
        self._symbol = data_loader.symbol
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._commission_rate = commission_rate

        # 多时间框架K线数据：{symbol: {interval: [kline_dict]}}
        self._klines: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # 当前回测位置（1h K线索引）
        self._current_index: int = -1
        # 数据是否已加载
        self._data_loaded: bool = False

        # 订单模拟器
        self.order_simulator = OrderSimulator()

        logger.info(
            "回测交易所模拟器初始化完成",
            symbol=self._symbol,
            initial_balance=float(initial_balance),
            commission_rate=float(commission_rate),
        )

    # ==================================================================
    # 回测专用方法
    # ==================================================================

    def load_all_data(self) -> None:
        """
        从 DataLoader 预加载所有时间框架数据

        将 DataLoader 加载的多时间框架K线数据转换为 Decimal 格式并存储，
        供后续 get_klines 和 advance_to 使用。

        Raises:
            RuntimeError: 数据加载失败
        """
        logger.info("开始预加载回测数据...")

        try:
            tf_data = self._data_loader.load_multi_timeframe_data()

            if not tf_data:
                raise RuntimeError("DataLoader 未返回任何时间框架数据")

            symbol_klines: Dict[str, List[Dict[str, Any]]] = {}

            for interval, raw_klines in tf_data.items():
                converted = []
                for k in raw_klines:
                    converted.append({
                        "open_time": self._parse_timestamp(k.get("timestamp", "")),
                        "open": Decimal(str(k["open"])),
                        "high": Decimal(str(k["high"])),
                        "low": Decimal(str(k["low"])),
                        "close": Decimal(str(k["close"])),
                        "volume": Decimal(str(k.get("volume", 0))),
                        "close_time": self._parse_timestamp(k.get("timestamp", "")),
                        "quote_volume": Decimal(str(k.get("quote_volume", 0))) if "quote_volume" in k else Decimal("0"),
                        "trades": int(k.get("trades", 0)) if "trades" in k else 0,
                        "timestamp": k.get("timestamp", ""),
                    })
                symbol_klines[interval] = converted

            self._klines[self._symbol] = symbol_klines
            self._data_loaded = True

            total_1h = len(symbol_klines.get("1h", []))
            logger.info(
                "回测数据预加载完成",
                symbol=self._symbol,
                timeframes=list(symbol_klines.keys()),
                kline_counts={tf: len(data) for tf, data in symbol_klines.items()},
                total_1h_klines=total_1h,
            )

        except Exception as e:
            logger.error("回测数据预加载失败", error=str(e), exc_info=True)
            raise RuntimeError(f"数据加载失败: {e}") from e

    def advance_to(self, index: int) -> None:
        """
        推进回测时间到指定1h K线索引

        更新当前价格、当前时间，并触发订单成交检查。

        Args:
            index: 1h K线索引（从0开始）

        Raises:
            RuntimeError: 数据未加载
            ValueError: 索引超出范围
        """
        if not self._data_loaded:
            raise RuntimeError("请先调用 load_all_data() 加载数据")

        klines_1h = self._klines[self._symbol].get("1h", [])
        if not klines_1h:
            raise RuntimeError("1h K线数据为空")

        if index < 0 or index >= len(klines_1h):
            raise ValueError(
                f"索引超出范围: index={index}, 有效范围 [0, {len(klines_1h) - 1}]"
            )

        self._current_index = index

        # 检查当前K线内的订单成交
        current_kline = klines_1h[index]
        self.order_simulator.check_fills(
            current_price=current_kline["close"],
            current_time=current_kline["timestamp"],
        )

        logger.debug(
            "回测时间推进",
            index=index,
            time=self.current_time,
            price=float(self.current_price),
            pending_orders=self.order_simulator.get_pending_count(),
        )

    # ------------------------------------------------------------------
    # 当前状态属性
    # ------------------------------------------------------------------

    @property
    def current_time(self) -> str:
        """当前回测时间（ISO格式字符串）"""
        klines_1h = self._klines.get(self._symbol, {}).get("1h", [])
        if self._current_index < 0 or not klines_1h:
            return ""
        return klines_1h[self._current_index]["timestamp"]

    @property
    def current_price(self) -> Decimal:
        """当前K线收盘价"""
        klines_1h = self._klines.get(self._symbol, {}).get("1h", [])
        if self._current_index < 0 or not klines_1h:
            return Decimal("0")
        return klines_1h[self._current_index]["close"]

    @property
    def current_open(self) -> Decimal:
        """当前K线开盘价"""
        klines_1h = self._klines.get(self._symbol, {}).get("1h", [])
        if self._current_index < 0 or not klines_1h:
            return Decimal("0")
        return klines_1h[self._current_index]["open"]

    @property
    def current_high(self) -> Decimal:
        """当前K线最高价"""
        klines_1h = self._klines.get(self._symbol, {}).get("1h", [])
        if self._current_index < 0 or not klines_1h:
            return Decimal("0")
        return klines_1h[self._current_index]["high"]

    @property
    def current_low(self) -> Decimal:
        """当前K线最低价"""
        klines_1h = self._klines.get(self._symbol, {}).get("1h", [])
        if self._current_index < 0 or not klines_1h:
            return Decimal("0")
        return klines_1h[self._current_index]["low"]

    @property
    def balance(self) -> Decimal:
        """当前账户余额（USDT）"""
        return self._balance

    @balance.setter
    def balance(self, value: Decimal) -> None:
        """设置账户余额"""
        self._balance = self._convert_to_decimal(value)

    @property
    def initial_balance(self) -> Decimal:
        """初始账户余额"""
        return self._initial_balance

    @property
    def commission_rate(self) -> Decimal:
        """手续费率"""
        return self._commission_rate

    # ==================================================================
    # BinanceClient 兼容接口（同步版本）
    # ==================================================================

    def get_ticker_price(self, symbol: str) -> Decimal:
        """
        获取交易对当前价格（BinanceClient 兼容）

        回测中返回当前1h K线的收盘价。

        Args:
            symbol: 交易对名称

        Returns:
            当前价格（Decimal）
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        symbol = symbol.strip().upper()
        if symbol != self._symbol:
            logger.warning(
                "查询的交易对与回测配置不匹配",
                requested=symbol,
                configured=self._symbol,
            )

        return self.current_price

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: Any,
        price: Optional[Any] = None,
        order_type: str = "LIMIT",
        **kwargs
    ) -> Dict[str, Any]:
        """
        下单（BinanceClient 兼容）

        委托给 OrderSimulator 处理。下单后立即检查是否可成交。

        Args:
            symbol:     交易对名称
            side:       订单方向，"BUY" 或 "SELL"
            quantity:   下单数量
            price:      订单价格（限价单必填）
            order_type: 订单类型，默认 "LIMIT"
            **kwargs:   其他参数

        Returns:
            订单信息字典
        """
        order = self.order_simulator.place_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            **kwargs
        )

        # 下单后立即尝试成交（同根K线内）
        self.order_simulator.check_fills(
            current_price=self.current_price,
            current_time=self.current_time,
        )

        return order

    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[Any] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        撤销订单（BinanceClient 兼容）

        Args:
            symbol:          交易对名称
            order_id:        订单ID
            client_order_id: 客户端订单ID（回测中忽略）

        Returns:
            被撤销的订单信息字典

        Raises:
            ValueError: 参数无效或订单不存在
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        if not order_id and not client_order_id:
            raise ValueError("必须提供 order_id 或 client_order_id")

        if order_id is not None and not str(order_id).strip():
            raise ValueError("订单ID不能为空")

        return self.order_simulator.cancel_order(symbol, order_id)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前挂单（BinanceClient 兼容）

        Args:
            symbol: 交易对（可选）

        Returns:
            未成交订单列表
        """
        return self.order_simulator.get_open_orders(symbol)

    def get_order(self, symbol: str, order_id: Any) -> Dict[str, Any]:
        """
        查询订单详情（BinanceClient 兼容）

        Args:
            symbol:   交易对名称
            order_id: 订单ID

        Returns:
            订单信息字典

        Raises:
            ValueError: 订单不存在
        """
        return self.order_simulator.get_order(symbol, order_id)

    def get_account_balance(self) -> Dict[str, Decimal]:
        """
        获取账户余额（BinanceClient 兼容）

        回测中只追踪 USDT 余额。
        注意：如果回测引擎有持仓，余额不含持仓价值。

        Returns:
            余额字典，如 {'USDT': Decimal('10000')}
        """
        balance = {}
        if self._balance > Decimal("0"):
            balance["USDT"] = self._balance
        return balance

    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息（BinanceClient 兼容）

        回测中返回简化版账户信息。

        Returns:
            账户信息字典
        """
        return {
            "totalMarginBalance": self._balance,
            "totalUnrealizedProfit": Decimal("0"),
            "totalWalletBalance": self._balance,
            "availableBalance": self._balance,
            "assets": [],
            "positions": [],
        }

    # ==================================================================
    # KLineService 兼容接口（同步版本）
    # ==================================================================

    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据（KLineService 兼容，同步版本）

        【关键约束】严格防止未来函数：
        - 只返回 current_time 之前（含当前）的K线数据
        - 如果当前 index 之后还有数据，不会泄露给策略

        Args:
            symbol:   交易对名称
            interval: K线周期（如 "1h"、"4h"、"1d"）
            limit:    返回数量上限

        Returns:
            K线数据列表，每个元素包含 open_time/open/high/low/close/volume/close_time 等字段

        Raises:
            ValueError: 参数验证失败
            RuntimeError: 数据未加载
        """
        if not self._data_loaded:
            raise RuntimeError("请先调用 load_all_data() 加载数据")

        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        if not interval or not isinstance(interval, str):
            raise ValueError(f"K线周期必须是非空字符串，实际为 {interval!r}")

        valid_intervals = [
            "1m", "3m", "5m", "15m", "30m",
            "1h", "2h", "4h", "6h", "8h", "12h",
            "1d", "3d", "1w", "1M",
        ]
        if interval not in valid_intervals:
            raise ValueError(f"无效的K线周期: {interval}，有效周期: {', '.join(valid_intervals)}")

        if limit <= 0 or limit > 1500:
            raise ValueError(f"数量限制必须在 1-1500 之间: {limit}")

        symbol = symbol.strip().upper()

        # 获取该交易对、该周期的所有K线
        all_klines = self._klines.get(symbol, {}).get(interval, [])
        if not all_klines:
            logger.warning(f"无K线数据: symbol={symbol}, interval={interval}")
            return []

        # 获取当前时间（用于截断未来数据）
        current_ts = self.current_time

        # 找到当前时间在K线数组中的位置，只返回 <= current_time 的数据
        cutoff_index = len(all_klines)
        if current_ts:
            for i, k in enumerate(all_klines):
                if k.get("timestamp", "") > current_ts:
                    cutoff_index = i
                    break

        # 截断到当前时间
        available_klines = all_klines[:cutoff_index]

        # 取最后 limit 根
        result = available_klines[-limit:] if len(available_klines) > limit else available_klines

        logger.debug(
            "获取K线数据（同步）",
            symbol=symbol,
            interval=interval,
            limit=limit,
            total_available=len(all_klines),
            cutoff_index=cutoff_index,
            returned=len(result),
        )

        return result

    def get_multi_timeframe_data(
        self,
        symbol: str,
        intervals: List[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取多时间框架K线数据（KLineService 兼容，同步版本）

        Args:
            symbol:    交易对名称
            intervals: 时间框架列表，默认 ["1h", "4h", "1d"]

        Returns:
            {interval: [kline_list]} 字典

        Raises:
            ValueError: 参数验证失败
        """
        if intervals is None:
            intervals = ["1h", "4h", "1d"]

        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"交易对必须是非空字符串，实际为 {symbol!r}")

        if not intervals or not isinstance(intervals, list):
            raise ValueError(f"时间框架必须是列表，实际为 {type(intervals).__name__}")

        result = {}
        for interval in intervals:
            try:
                klines = self.get_klines(symbol, interval, self._get_limit_for_interval(interval))
                if klines:
                    result[interval] = klines
                else:
                    logger.warning(f"无 {interval} 周期数据: {symbol}")
            except Exception as e:
                logger.error(
                    f"获取 {interval} 周期数据失败: {symbol}",
                    error=str(e),
                )

        logger.debug(
            "获取多时间框架数据（同步）",
            symbol=symbol,
            intervals=list(result.keys()),
            counts={tf: len(data) for tf, data in result.items()},
        )

        return result

    # ==================================================================
    # 内部辅助方法
    # ==================================================================

    def _get_limit_for_interval(self, interval: str) -> int:
        """根据周期返回默认K线数量"""
        limits = {
            "1d": 100,
            "4h": 100,
            "1h": 100,
            "15m": 100,
            "5m": 100,
            "1m": 100,
        }
        return limits.get(interval, 100)

    @staticmethod
    def _parse_timestamp(ts_str: str) -> int:
        """
        将 ISO 时间字符串解析为毫秒级时间戳

        Args:
            ts_str: ISO 格式时间字符串

        Returns:
            毫秒级 Unix 时间戳
        """
        if not ts_str:
            return 0
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _convert_to_decimal(value: Any) -> Decimal:
        """安全转换为 Decimal"""
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def get_klines_count(self, interval: str = "1h") -> int:
        """
        获取已加载的K线总数

        Args:
            interval: K线周期

        Returns:
            K线数量
        """
        return len(self._klines.get(self._symbol, {}).get(interval, []))

    @property
    def is_data_loaded(self) -> bool:
        """数据是否已加载"""
        return self._data_loaded