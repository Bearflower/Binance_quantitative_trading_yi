"""Task-local final order authorization using the existing held policy connection."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_authorization = ContextVar('entry_submission_authorization', default=None)


@dataclass
class SubmissionAttempt:
    callback: object
    owner: object
    active: bool = True
    transport_started: bool = False
    denied_before_transport: bool = False


def is_new_risk_order(method, endpoint, params):
    return (method.upper() == 'POST'
            and endpoint in ('/fapi/v1/order', '/papi/v1/um/order')
            and str(params.get('reduceOnly', False)).lower() != 'true'
            and str(params.get('closePosition', False)).lower() != 'true')


@contextmanager
def bind_submission_authorization(callback):
    attempt = SubmissionAttempt(callback, asyncio.current_task())
    token = _authorization.set(attempt)
    try:
        yield attempt
    finally:
        attempt.active = False
        _authorization.reset(token)


async def authorize_submission(method, endpoint, params):
    attempt = _authorization.get()
    if attempt is not None and is_new_risk_order(method, endpoint, params):
        if not attempt.active or attempt.owner is not asyncio.current_task():
            raise RuntimeError('submission_authorization_scope_invalid')
        try:
            await attempt.callback()
        except Exception:
            if not attempt.transport_started:
                attempt.denied_before_transport = True
            raise


def reject_before_transport(method, endpoint, params):
    attempt = _authorization.get()
    if (attempt is not None and attempt.owner is asyncio.current_task()
            and is_new_risk_order(method, endpoint, params) and not attempt.transport_started):
        attempt.denied_before_transport = True


def mark_transport_started(method, endpoint, params):
    attempt = _authorization.get()
    if attempt is not None and is_new_risk_order(method, endpoint, params):
        attempt.transport_started = True
