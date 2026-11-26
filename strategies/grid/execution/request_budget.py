"""Grid-local weighted REST budget with server-directed IP cooldown.

All-account ownership scans remain all-account. This caps this executor, not
other processes on the same IP; server headers provide the shared-IP backstop.
"""
import asyncio
import time
from collections import deque
import aiohttp
from shared.binance_api import BinanceClient, BinanceAPIError


def request_weight(method, endpoint, params=None):
    name = endpoint.rsplit('/', 1)[-1]
    if name in ('openOrders', 'openAlgoOrders') and method.upper() == 'GET':
        return 1 if (params or {}).get('symbol') else 40
    if name in ('income', 'dual'):
        return 30
    if name in ('positionRisk', 'account', 'balance', 'accountConfig', 'symbolConfig',
                'allOrders', 'userTrades', 'algoOrder'):
        return 5
    if name in ('order', 'listenKey', 'exchangeInfo', 'price', 'premiumIndex'):
        return 1
    return 40  # Unknown endpoints must not silently consume unbudgeted weight.


class RequestBudget:
    def __init__(self, limit=1200, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.limit, self.clock, self.sleep = limit, clock, sleep
        self.used = deque()
        self.cooldown_until = 0.0
        self.guard = asyncio.Lock()

    def has_capacity(self, weight):
        now = self.clock()
        while self.used and self.used[0][0] <= now - 60:
            self.used.popleft()
        return (0 < weight <= self.limit and now >= self.cooldown_until
                and sum(w for _, w in self.used) + weight <= self.limit)

    async def acquire(self, weight):
        if not 0 < weight <= self.limit:
            raise ValueError('invalid request weight')
        while True:
            async with self.guard:
                now = self.clock()
                while self.used and self.used[0][0] <= now - 60:
                    self.used.popleft()
                delay = max(0, self.cooldown_until - now)
                if sum(w for _, w in self.used) + weight > self.limit:
                    delay = max(delay, self.used[0][0] + 60 - now)
                if delay <= 0:
                    self.used.append((now, weight))
                    return
            await self.sleep(delay)

    def observe(self, status, headers):
        headers = {k.lower(): v for k, v in headers.items()}
        now = self.clock()
        if status in (429, 418):
            try:
                retry = float(headers.get('retry-after', 0))
            except (ValueError, TypeError):
                retry = 0
            self.cooldown_until = max(self.cooldown_until, now + max(retry, 120 if status == 418 else 60))
        try:
            used = int(headers.get('x-mbx-used-weight-1m', 0))
        except (ValueError, TypeError):
            used = 0
        if used >= 1800:
            # Leave 600 units to other services and cleanup. Waiting an entire
            # window is conservative even if the server's reset is earlier.
            self.cooldown_until = max(self.cooldown_until, now + 60)


class GridBinanceClient(BinanceClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_budget = RequestBudget()

    async def _init_session(self):
        if self.session is None or self.session.closed:
            trace = aiohttp.TraceConfig()
            async def observed(session, context, params):
                self.request_budget.observe(params.response.status, params.response.headers)
            trace.on_request_end.append(observed)
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                headers={'X-MBX-APIKEY': self._api_key, 'Content-Type': 'application/json'},
                trace_configs=[trace])

    async def _request_once(self, method, endpoint, params=None, signed=True):
        await self.request_budget.acquire(request_weight(method, endpoint, params))
        try:
            return await super()._request_once(method, endpoint, params, signed)
        except BinanceAPIError as exc:
            if exc.code == -1003:
                self.request_budget.observe(429, {})
            raise
