from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.full_regression_live_gate import trace_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("method,signed,path", [
    ("POST", True, "/fapi/v1/order"),
    ("DELETE", True, "/fapi/v1/order"),
    ("PUT", True, "/fapi/v1/listenKey"),
    ("GET", False, "/fapi/v1/account"),
    ("GET", True, "/papi/v1/account"),
])
async def test_probe_rejects_writes_unsigned_and_wrong_account_before_transport(method, signed, path):
    transport = AsyncMock()
    client = SimpleNamespace(_request_once=transport, _get_endpoint=lambda value: value)
    trace = []
    trace_reads(client, trace)
    with pytest.raises(RuntimeError):
        await client._request_once(method, path, signed=signed)
    transport.assert_not_awaited()
    assert trace == []


@pytest.mark.asyncio
async def test_trace_records_actual_mapped_endpoint_without_request_credentials():
    transport = AsyncMock(return_value={"ok": True})
    client = SimpleNamespace(_request_once=transport, _get_endpoint=lambda value: "/fapi/v3/account")
    trace = []
    trace_reads(client, trace)
    assert await client._request_once("GET", "/fapi/v2/account", {"private": "hidden"}) == {"ok": True}
    assert trace == [{"method": "GET", "path": "/fapi/v3/account", "signed": True}]


@pytest.mark.asyncio
async def test_failed_request_cannot_count_as_successful_exchange_evidence():
    transport = AsyncMock(side_effect=RuntimeError("exchange unavailable"))
    client = SimpleNamespace(_request_once=transport, _get_endpoint=lambda value: value)
    trace = []
    trace_reads(client, trace)
    with pytest.raises(RuntimeError, match="exchange unavailable"):
        await client._request_once("GET", "/fapi/v3/account")
    assert trace == []


def test_cli_freshness_starts_before_account_reads(monkeypatch, capsys):
    import json
    import sys
    from scripts import full_regression_live_gate as gate
    clock = [1000.0]
    async def account():
        clock[0] = 1100.0  # Slow GET/DB collection must not reset evidence age.
        return {'account': 'default'}
    monkeypatch.setattr(gate.time, 'time', lambda: clock[0])
    monkeypatch.setattr(gate, 'main_account', account)
    monkeypatch.setattr(sys, 'argv', ['probe', '--kind', 'main', '--source', 'a'*40,
        '--container', 'test-service', '--old-id', 'b'*64])
    gate.cli()
    evidence = json.loads(capsys.readouterr().out.split('LIVE_GATE_RESULT=', 1)[1])
    assert evidence['observed_at'] == 1000.0
    assert evidence['source'] == 'a'*40 and evidence['old_id'] == 'b'*64
    assert evidence['ok'] is True and evidence['read_only'] is True
