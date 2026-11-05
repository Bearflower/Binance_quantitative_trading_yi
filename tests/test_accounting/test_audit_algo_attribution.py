from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from scripts.audit_algo_attribution import assemble_candidates, _fetch_links


def _income(kind="REALIZED_PNL", amount="5"):
    return dict(account_id="default", symbol="ABCUSDT", trade_id=7,
                tran_id=9 if kind == "REALIZED_PNL" else 10,
                income_type=kind, income=Decimal(amount), asset="USDT",
                event_time_utc=datetime(2026, 11, 2, tzinfo=timezone.utc),
                order_id=101, realized_pnl=Decimal("5"),
                commission=Decimal("0.2"), commission_asset="USDT")


def test_exact_algo_bridge_produces_reviewable_candidates():
    links = [{"symbol": "ABCUSDT", "algo_id": 501, "actual_order_id": 101,
              "payload_sha256": "abc"}]
    owners = [{"symbol": "ABCUSDT", "algo_id": 501, "strategy_name": "hrs"}]
    events = [_income(), _income("COMMISSION", "-0.2")]
    result = assemble_candidates(events, links, owners, [])
    assert len(result["candidates"]) == 2
    assert {row["strategy"] for row in result["candidates"]} == {"hrs"}
    assert {row["income_type"] for row in result["candidates"]} == {"REALIZED_PNL", "COMMISSION"}
    assert result["rejected"] == []


def test_ambiguous_or_mismatched_evidence_stays_unattributed():
    links = [{"symbol": "ABCUSDT", "algo_id": 501, "actual_order_id": 101,
              "payload_sha256": "abc"},
             {"symbol": "ABCUSDT", "algo_id": 502, "actual_order_id": 101,
              "payload_sha256": "def"}]
    owners = [{"symbol": "ABCUSDT", "algo_id": 501, "strategy_name": "hrs"},
              {"symbol": "ABCUSDT", "algo_id": 502, "strategy_name": "btc_eth"}]
    result = assemble_candidates([_income()], links, owners, [])
    assert result["candidates"] == []
    assert result["rejected"][0]["reason"] == "ambiguous_algo_link"

    result = assemble_candidates([_income(amount="6")], links[:1], owners[:1], [])
    assert result["candidates"] == []
    assert result["rejected"][0]["reason"] == "income_fill_mismatch"


def test_existing_order_owner_is_not_duplicated_as_candidate():
    links = [{"symbol": "ABCUSDT", "algo_id": 501, "actual_order_id": 101,
              "payload_sha256": "abc"}]
    owners = [{"symbol": "ABCUSDT", "algo_id": 501, "strategy_name": "hrs"}]
    result = assemble_candidates([_income()], links, owners,
                                 [{"symbol": "ABCUSDT", "order_id": "101", "strategy": "hrs"}])
    assert result["candidates"] == []
    assert result["rejected"][0]["reason"] == "already_owned"

    result = assemble_candidates([_income()], links, owners,
                                 [{"symbol": "ABCUSDT", "order_id": "101", "strategy": "btc_eth"}])
    assert result["candidates"] == []
    assert result["rejected"][0]["reason"] == "existing_owner_conflict"


@pytest.mark.asyncio
async def test_algo_history_requires_complete_page_and_valid_identity():
    client = type("Client", (), {})()
    client._request = AsyncMock(return_value=[{"symbol": "ABCUSDT", "algoId": 501,
                                               "actualOrderId": "101", "algoStatus": "FINISHED"}])
    links = await _fetch_links(client, ["ABCUSDT"], 1000, 2000)
    assert links[0]["actual_order_id"] == 101
    assert links[0]["payload_sha256"]

    client._request.return_value = [{}] * 1000
    with pytest.raises(RuntimeError, match="未证实完整"):
        await _fetch_links(client, ["ABCUSDT"], 1000, 2000)
