"""Internal HTTP API. This process has no exchange credentials or trading API."""
import json
import os
from pathlib import Path
from aiohttp import web
from .hub import MarketCapHub


def create_app(path=None, registry=None):
    if registry is None:
        registry = json.loads(Path(__file__).with_name('assets.json').read_text())
    app = web.Application(client_max_size=65536)
    hub = MarketCapHub(path or os.getenv('MARKET_CAP_CACHE', '/data/market-cap.sqlite'), registry)
    app['hub'] = hub

    async def quotes(request):
        data = await request.json()
        symbols = data.get('symbols') if isinstance(data, dict) else None
        if not isinstance(symbols, list) or len(symbols)>1000 or any(not isinstance(s,str) or len(s)>40 for s in symbols):
            raise web.HTTPBadRequest(text='symbols must be a bounded string list')
        result = await hub.collect(symbols) if request.path == '/collect' else hub.read(symbols)
        return web.json_response(result)

    async def health(request):
        return web.json_response(dict(status='ok', assets=len(registry), **hub.stats))

    async def cleanup(app):
        if hub.session:
            await hub.session.close()
        hub.close()

    app.router.add_post('/collect', quotes)
    app.router.add_post('/read', quotes)
    app.router.add_get('/health', health)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == '__main__':
    web.run_app(create_app(), host='0.0.0.0', port=8080)
