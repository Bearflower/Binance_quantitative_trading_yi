from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_tuner.monitor.hrs_closed_lot import _flat_bounds
from ai_tuner.ops import mtpcs_observation
from scripts.maintain_monthly_facts_host import windows
from shared.new_coin_recovery_evidence import owned_recovery_reduction_algos


def test_monthly_timer_respects_cutover_and_month_boundaries():
    now = datetime(2026, 12, 1, 3, tzinfo=timezone.utc)
    current = int(datetime(2026, 11, 30, 16, tzinfo=timezone.utc).timestamp() * 1000)
    cutoff = current - 12 * 3600_000
    pm = windows(now, pm=True, cutover_ms=cutoff)
    classic = windows(now, pm=False, cutover_ms=cutoff)
    assert len(pm) == 1 and pm[0][1] == cutoff - 1
    assert classic == [(cutoff, current - 1), (current, int(now.timestamp() * 1000) - 1000)]
    with pytest.raises(ValueError):
        windows(now, pm=False, cutover_ms=None)


@pytest.mark.parametrize("classic", [True, False])
def test_hrs_signed_flat_anchor_source_is_specific_to_the_active_mode(classic):
    import hashlib
    raw = {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}
    fact = dict(symbol="ETHUSDT", raw_payload=raw, at_ms=12, read_until_ms=14,
                credential_fingerprint="a"*64,
                payload_sha256=hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    for mode in ("classic", "pm"):
        fact["source"] = f"signed_{mode}_position_risk_v1"
        if (mode == "classic") == classic:
            assert _flat_bounds(fact, "ETHUSDT", 11, 15, classic=classic) == 14
        else:
            with pytest.raises(ValueError):
                _flat_bounds(fact, "ETHUSDT", 11, 15, classic=classic)


@pytest.mark.asyncio
@pytest.mark.parametrize("classic", [True, False])
async def test_tail_reduction_lookup_only_uses_matching_algo_links(classic):
    db = MagicMock(fetch_all=AsyncMock(return_value=[dict(algo_id="42")]))
    assert await owned_recovery_reduction_algos(db, "default", "ETHUSDT", 40, datetime.now(timezone.utc),
                                                classic=classic) == (42,)
    sql, *values = db.fetch_all.await_args.args
    assert 'o.source_kind=$5' in sql
    assert values[-1] == ("classic_algo_link" if classic else "pm_algo_link")


def test_classic_digest_keeps_pm_entry_and_archive_files_separate(tmp_path, monkeypatch):
    first = tmp_path / 'first_entries.json'
    first.write_text('{"btc_eth": {"state": "verified", "order_id": "pm-only"}}')
    old = tmp_path / 'order_archive_checkpoint.json'
    old.write_text('{"covered_until_ms": 100}')
    def read(command, **unused):
        if command[0] != 'docker':
            raise OSError('shadow temporarily unavailable')
        if command[2] == 'ai-tuner':
            return 'classic'
        if command[3] == 'ai-tuner':
            assert command[-3:] == ['', 'persist', 'classic']
            return {'matched': {k: 0 for k in mtpcs_observation.STRATEGIES}, 'written': 0, 'persisted': 0,
                    'covered_until_ms': 200, 'up_to_date': True}
        assert command[-1] == 'classic'
        return {'strategies': {k: None for k in mtpcs_observation.STRATEGIES}}
    monkeypatch.setattr(mtpcs_observation, 'read_json', read)
    text, _ = mtpcs_observation.prepare(tmp_path, tmp_path/'shadow', persist=True)
    assert 'pm-only' not in text
    assert json.loads(old.read_text())['covered_until_ms'] == 100
    assert json.loads((tmp_path/'classic/order_archive_checkpoint.json').read_text())['covered_until_ms'] == 200
