"""同步策略夹具也需要循环，以兼容 Python 3.9 的 asyncio.Lock 构造。"""
import asyncio
import pytest


@pytest.fixture(autouse=True)
def strategy_fixture_loop():
    """为同步构造阶段补充循环；异步测试仍由 pytest-asyncio 管理。"""
    created = None
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError('循环已关闭')
    except RuntimeError:
        created = asyncio.new_event_loop()
        asyncio.set_event_loop(created)
    yield
    if created is not None and not created.is_closed():
        created.close()
