"""Task-local final order authorization using the existing held policy connection."""
from contextlib import contextmanager
from contextvars import ContextVar

_authorization = ContextVar('entry_submission_authorization', default=None)


def is_new_risk_order(method, endpoint, params):
    return (method.upper() == 'POST'
            and endpoint in ('/fapi/v1/order', '/papi/v1/um/order')
            and str(params.get('reduceOnly', False)).lower() != 'true'
            and str(params.get('closePosition', False)).lower() != 'true')


@contextmanager
def bind_submission_authorization(callback):
    token = _authorization.set(callback)
    try:
        yield
    finally:
        _authorization.reset(token)


async def authorize_submission(method, endpoint, params):
    callback = _authorization.get()
    if callback is not None and is_new_risk_order(method, endpoint, params):
        await callback()
