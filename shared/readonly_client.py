"""只读 Binance 客户端（影子实例安全边界）

双层 fail-closed 设计：
1. 底层 _request 白名单收口：任何写端点在 HTTP 发起前即抛 ReadOnlyViolationError
2. 6 个公开写方法显式覆写：在业务调用处即失败，错误栈直指调用者

所有只读方法（get_*/查询类）正常放行。
"""
from __future__ import annotations

from typing import Dict, Optional

from shared.binance_api import BinanceClient


class ReadOnlyViolationError(Exception):
    """影子实例触达禁用写接口时抛出的异常

    异常消息包含 method / endpoint / symbol / instance_id，
    便于安全告警时定位。
    """

    def __init__(
        self,
        method: str,
        endpoint: str,
        *,
        symbol: str = "",
        instance_id: str = "shadow",
        extra: str = "",
    ):
        self.method = method
        self.endpoint = endpoint
        self.symbol = symbol
        self.instance_id = instance_id
        msg = (
            f"ReadOnlyBinanceClient 拦截写接口 "
            f"method={method} endpoint={endpoint} "
            f"symbol={symbol or '-'} instance_id={instance_id}"
        )
        if extra:
            msg += f" extra={extra}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# 6 个公开写方法显式覆写（与 binance_api.py 保持同步）
# ---------------------------------------------------------------------------


class ReadOnlyBinanceClient(BinanceClient):
    """只读 Binance 客户端子类

    禁止任何写端点（下单、撤单、条件单、杠杆），
    任何调用都抛 ReadOnlyViolationError 且 0 次 HTTP 请求。
    """

    def __init__(self, *args, instance_id: str = "shadow", **kwargs):
        super().__init__(*args, **kwargs)
        self.instance_id = instance_id

    # --- 底层收口：覆写 _request，在 HTTP 发起前判断写端点 ---
    # 注：_request 是 _request_read / _request_write 的唯一入口（binance_api.py L228-255）
    # 拦截点在此，保证未来新增写方法未显式覆写时仍会被底层拒绝。

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True,
        *,
        idempotency_key: Optional[str] = None,
        idempotent: bool = False,
    ) -> Dict:
        """只读安全边界：只放行 GET，其他所有 HTTP method 一律拦截

        契约（v2 —— 2026-10-19 修正）：
          - 旧实现用黑名单 POST/PUT/DELETE → 未来出现 PATCH/其他 method 会穿透；
          - 新实现用白名单：只有 GET 放行，任何非 GET 一律 fail-closed 抛 ReadOnlyViolationError。
          - 合法 GET 读接口（如 GET /fapi/v1/leverageBracket、GET /fapi/v1/order）不受影响。
        """
        m = (method or "").upper()
        # —— 白名单：只放行 GET（唯一合法读 method）——
        is_write_or_unknown = m != "GET"

        if is_write_or_unknown:
            symbol = (params or {}).get("symbol", "") if params else ""
            raise ReadOnlyViolationError(
                method=m,
                endpoint=endpoint,
                symbol=symbol,
                instance_id=self.instance_id,
                extra="_request 底层白名单拦截：仅 GET 放行",
            )

        return await super()._request(
            method, endpoint, params, signed,
            idempotency_key=idempotency_key, idempotent=idempotent,
        )

    # --- 6 个公开写方法显式覆写：在调用处直接失败 ---

    async def place_order(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "POST", "/fapi/v1/order",
            symbol=symbol, instance_id=self.instance_id,
            extra="place_order 显式覆写",
        )

    async def place_conditional_order(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "POST", "/papi/v1/um/algo/order",
            symbol=symbol, instance_id=self.instance_id,
            extra="place_conditional_order 显式覆写",
        )

    async def cancel_order(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "DELETE", "/fapi/v1/order",
            symbol=symbol, instance_id=self.instance_id,
            extra="cancel_order 显式覆写",
        )

    async def cancel_algo_order(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "DELETE", "/papi/v1/um/algo/order",
            symbol=symbol, instance_id=self.instance_id,
            extra="cancel_algo_order 显式覆写",
        )

    async def cancel_all_algo_orders(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "DELETE", "/papi/v1/um/algo/allOpenOrders",
            symbol=symbol, instance_id=self.instance_id,
            extra="cancel_all_algo_orders 显式覆写",
        )

    async def set_leverage(self, *args, symbol: str = "", **kwargs) -> Dict:
        raise ReadOnlyViolationError(
            "POST", "/fapi/v1/leverage",
            symbol=symbol, instance_id=self.instance_id,
            extra="set_leverage 显式覆写",
        )
