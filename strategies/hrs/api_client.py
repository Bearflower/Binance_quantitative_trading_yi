"""HRS GET response classification on the existing bounded read retry path."""
from aiohttp import ContentTypeError
import re

from shared.binance_api import BinanceAPIError, BinanceClient


class HrsReadResponseError(BinanceAPIError):
    def __init__(self, status, content_type):
        self.status_code = status
        self.content_type = (content_type if len(content_type) <= 127 and re.fullmatch(
            r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', content_type) else 'non-json')
        # The shared reader already retries5xx and rejects4xx. Unexplained
        # success/redirect HTML remains non-retryable; it never becomes data.
        code = status if 500 <= status < 600 else -9999
        super().__init__(code, f'HRS GET returned non-JSON response (HTTP {status}, type={self.content_type})')


class HrsBinanceClient(BinanceClient):
    async def get_account_info(self):
        account = await super().get_account_info()
        if self.use_unified_account is False:
            config = await self._request("GET", "/fapi/v1/accountConfig", signed=True)
            if not isinstance(config, dict):
                raise ValueError("classic_account_config_missing")
            account["accountConfig"] = {k: config.get(k) for k in
                                        ("canTrade", "dualSidePosition", "multiAssetsMargin")}
        return account

    async def _request_once(self, method, endpoint, params=None, signed=True):
        try:
            return await super()._request_once(method, endpoint, params, signed)
        except BinanceAPIError as exc:
            original = exc.__context__
            if (method.upper() != 'GET' or exc.code != -9999
                    or not isinstance(original, ContentTypeError)):
                raise
            # aiohttp retains the real HTTP status in ContentTypeError. Do not
            # log its request_info, signed URL, response body or headers.
            status = original.status
            if type(status) is not int or not 100 <= status <= 599:
                raise
            content_type = getattr(original, 'headers', {}).get('Content-Type', '') if original.headers else ''
            content_type = str(content_type).split(';', 1)[0].strip().lower()
            if not content_type:
                content_type = 'non-json'
            raise HrsReadResponseError(status, content_type) from exc
