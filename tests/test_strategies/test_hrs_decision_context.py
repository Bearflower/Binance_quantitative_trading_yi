from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_capture_financial_context_keeps_actual_values_and_unready_ledger_explicit():
    from strategies.hrs.decision_context import capture_financial_context
    strategy = SimpleNamespace(binance_client=SimpleNamespace(get_account_info=AsyncMock(
        return_value={'totalMarginBalance': '123.456', 'totalWalletBalance': '123', 'api_key': 'hidden'}),
        get_position=AsyncMock(return_value=[]), get_um_account_positions=AsyncMock(return_value=[])),
        capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(52.31, 'db'))),
        trading_executor=SimpleNamespace(timeout_recovery=SimpleNamespace(pending=AsyncMock(return_value=[]))),
        db=SimpleNamespace(fetch_all=AsyncMock(side_effect=[[{'revision': 6}], [], [], [], [], [], [], []])))
    result = await capture_financial_context(strategy)
    assert result['account_equity'] == '123.456'
    assert result['allocation'] == '52.31'
    assert result['ledger_state'] is None
    assert result['replay_bootstrap_ready'] is False
    assert result['pending_timeout_entries'] == []
    assert 'hidden' not in str(result)
    assert set(result['state_tables']) == {'hrs_positions', 'hrs_blacklist', 'hrs_active_symbols'}


@pytest.mark.asyncio
async def test_missing_account_equity_is_not_filled_with_zero():
    from strategies.hrs.decision_context import capture_financial_context
    strategy = SimpleNamespace(binance_client=SimpleNamespace(get_account_info=AsyncMock(return_value={}),
        get_position=AsyncMock(return_value=[]), get_um_account_positions=AsyncMock(return_value=[])),
        capital_mgr=SimpleNamespace(get_effective_margin_limit=AsyncMock(return_value=(52.31, 'db'))),
        trading_executor=SimpleNamespace(timeout_recovery=SimpleNamespace(pending=AsyncMock(return_value=[]))),
        db=SimpleNamespace(fetch_all=AsyncMock(side_effect=[[{'revision': 6}], [], [], [], [], [], [], []])))
    with pytest.raises(ValueError, match='account_equity_missing'):
        await capture_financial_context(strategy)


def test_configuration_identity_binds_actual_empty_overlay_fallback(tmp_path):
    from shared.config_loader import compute_config_hash
    from strategies.hrs.decision_context import capture_configuration_identity
    folder = tmp_path/'hrs'; (folder/'tuning_overrides').mkdir(parents=True)
    (folder/'config.yaml').write_text('trading:\n  leverage: 2\n')
    (folder/'tuning_overrides/.active').write_text('V1')
    (folder/'tuning_overrides/V1.yaml').write_text('# no tuning parameters\n')
    manifest = tmp_path/'params.yaml'; manifest.write_text('strategy: hrs\n')
    config = {'trading': {'leverage': 2}}
    metadata = {'config_hash': compute_config_hash(config), 'requested_overrides_version': 'V1',
                'applied_overrides_version': None, 'load_error': 'empty overlay'}
    result = capture_configuration_identity(folder, config, metadata, params_path=manifest)
    assert set(result['config_hashes']) == {'base_config_hash','params_manifest_hash','overlay_hash'}
    assert result['overlay_applied'] is False
    assert result['documents']['overlay'] is None
    assert result['effective_config_hash'] == metadata['config_hash']
    (folder/'config.yaml').write_text('trading:\n  leverage: 10\n')
    with pytest.raises(ValueError, match='configuration_identity'):
        capture_configuration_identity(folder, config, metadata, params_path=manifest)


def test_configuration_capture_never_exports_opaque_yaml_secrets(tmp_path):
    from shared.config_loader import compute_config_hash
    from strategies.hrs.decision_context import capture_configuration_identity
    folder = tmp_path/'hrs'; folder.mkdir()
    (folder/'config.yaml').write_text('trading:\n  leverage: 2\napi_key: live-secret-value\n')
    manifest = tmp_path/'params.yaml'; manifest.write_text('strategy: hrs\n')
    config = {'trading': {'leverage': 2}, 'api_key': 'live-secret-value'}
    result = capture_configuration_identity(folder, config, {
        'config_hash': compute_config_hash(config), 'requested_overrides_version': None,
        'applied_overrides_version': None}, params_path=manifest)
    assert 'live-secret-value' not in str(result)


def test_hrs_marked_component_retains_source_time_instead_of_receipt_time():
    from strategies.hrs.decision_context import ledger_equity_observation
    from strategies.hrs.equity_evidence import canonical
    from datetime import datetime, timezone
    timestamp = datetime(2026, 11, 17, 14, tzinfo=timezone.utc)
    ledger = {'version': 6, 'segment_id': 'segment', 'ledger_state': {
        'equity': '31.85', 'as_of': timestamp.isoformat()}}
    payload = {'segment_id': 'segment', 'as_of': timestamp.isoformat(), 'result': {'ledger_version': 6}}
    _, digest = canonical(payload)
    source = {'segment_id': 'segment', 'as_of': timestamp, 'available_at': timestamp,
              'evidence_sha256': digest, 'payload': payload}
    result = ledger_equity_observation(ledger, [source])
    assert result['value'] == '31.85'
    assert result['as_of'] == timestamp.isoformat()
    assert result['evidence_sha256'] == digest
    source['payload'] = dict(payload, result={'ledger_version': 7})
    assert ledger_equity_observation(ledger, [source]) is None
