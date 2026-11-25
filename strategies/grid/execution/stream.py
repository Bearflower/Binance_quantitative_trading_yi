"""PM user data stream with explicit reconnect and REST reconciliation hooks."""

import asyncio
import json
import re
import logging
import time
from typing import Awaitable, Callable, Optional

import aiohttp

from shared.binance_api import BinanceAPIError


logger = logging.getLogger(__name__)


def user_stream_url(mode: str, listen_key: str,
                    testnet_base: Optional[str] = None, *, pm: bool = True) -> str:
    if not isinstance(listen_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", listen_key):
        raise ValueError("invalid user stream listen key")
    if mode == "live":
        base = "wss://fstream.binance.com/" + ("pm" if pm else "private")
    elif mode == "testnet":
        if not testnet_base or not testnet_base.startswith("wss://"):
            raise ValueError("testnet WebSocket endpoint must be verified and explicit")
        base = testnet_base.rstrip("/")
    else:
        raise ValueError("user stream requires testnet or live mode")
    return base + "/ws/" + listen_key


class UserStream:
    def __init__(self, exchange, mode: str,
                 on_event: Callable[[dict], Awaitable[None]],
                 on_reconnect: Callable[[], Awaitable[None]],
                 on_disconnect: Callable[[], Awaitable[None]],
                 on_idle: Callable[[], Awaitable[None]],
                 *, testnet_base: Optional[str] = None, pm: bool = True):
        self.exchange = exchange
        self.mode = mode
        self.pm = pm
        self.on_event = on_event
        self.on_reconnect = on_reconnect
        self.on_disconnect = on_disconnect
        self.on_idle = on_idle
        self.testnet_base = testnet_base
        self.ready = False
        self.disconnect_stage = None

    async def run_forever(self) -> None:
        while True:
            stage = 'listen_key_create'
            socket = None
            try:
                key = await self.exchange.start_user_stream()
                url = user_stream_url(self.mode, key, self.testnet_base, pm=self.pm)
                async with aiohttp.ClientSession() as session:
                    stage = 'websocket_handshake'
                    async with session.ws_connect(url, heartbeat=45) as socket:
                        stage = 'reconcile'
                        await self.on_reconnect()
                        self.ready = True
                        logger.info("grid user stream ready after reconciliation")
                        connected_at = time.monotonic()
                        keepalive_at = connected_at + 30 * 60
                        while time.monotonic() - connected_at < 23 * 60 * 60:
                            if time.monotonic() >= keepalive_at:
                                stage = 'renew'
                                renewed = await self.exchange.keepalive_user_stream()
                                if renewed != key:
                                    raise RuntimeError("listen key changed")
                                keepalive_at = time.monotonic() + 30 * 60
                                logger.info("grid user stream renewed")
                            try:
                                stage = 'receive'
                                # aiohttp restarts its per-frame deadline on a PONG.
                                # Bound the entire wait so quiet accounts still renew.
                                message = await asyncio.wait_for(socket.receive(), timeout=60)
                            except asyncio.TimeoutError:
                                # Account streams can legitimately be idle. A
                                # REST reconciliation establishes health.
                                stage = 'idle reconciliation'
                                await self.on_idle()
                                continue
                            if message.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(message.data)
                                if not isinstance(event, dict):
                                    raise ValueError("malformed user stream event")
                                if event.get("e") == "listenKeyExpired":
                                    stage = 'listenKeyExpired'
                                    raise RuntimeError("listen key expired")
                                stage = 'account event'
                                await self.on_event(event)
                            elif message.type in (aiohttp.WSMsgType.CLOSED,
                                                  aiohttp.WSMsgType.CLOSE,
                                                  aiohttp.WSMsgType.ERROR):
                                stage = f"websocket_{message.type.name.lower()}"
                                raise RuntimeError("user stream disconnected")
            except asyncio.CancelledError:
                self.ready = False
                self.disconnect_stage = "shutdown"
                logger.info("grid user stream shutting down: stage=%s", stage)
                await self.on_disconnect()
                raise
            except Exception as exc:
                self.ready = False
                self.disconnect_stage = stage
                # Exception text and WebSocket close reasons may contain private data.
                close_code = socket.close_code if socket is not None else None
                logger.warning("grid user stream interrupted: stage=%s type=%s api_code=%s ws_close_code=%s",
                               stage, type(exc).__name__, exc.code if isinstance(exc, BinanceAPIError) else None,
                               close_code if isinstance(close_code, int) else None)
                await self.on_disconnect()
                await asyncio.sleep(5)
            else:
                self.ready = False
                self.disconnect_stage = "scheduled_rotation"
                logger.info("grid user stream rotating after scheduled connection lifetime")
                await self.on_disconnect()
