from copy import deepcopy

from shared.accounting.pm_equity_bridge import audit_usdt_bridge


def evidence():
    return dict(
        started_ms=1000, ended_ms=1500,
        before={'actualEquity': '99', 'updateTime': 1001},
        after={'actualEquity': '99', 'updateTime': 1499},
        balances=[dict(asset='USDT', totalWalletBalance='98', umUnrealizedPNL='2',
                       cmUnrealizedPNL='0', crossMarginBorrowed='0',
                       crossMarginInterest='0', negativeBalance='0')],
        index=[dict(asset='USDT', assetIndexPrice='0.99', time=1300)],
    )


def test_independent_index_reconciles_both_account_observations():
    result = audit_usdt_bridge(**evidence())
    assert result['status'] == 'VERIFIED_POINT'
    assert result['reconstructed_usd'] == '99.00'
    assert result['residual_before_usd'] == result['residual_after_usd'] == '0.00'


def test_second_account_sample_cannot_mask_incoherent_bracket():
    bundle = evidence()
    bundle['before']['actualEquity'] = '99.2'
    result = audit_usdt_bridge(**bundle)
    assert result['status'] == 'BLOCKED_DATA'
    assert 'equity_bridge_residual_exceeds_tolerance' in result['reasons']


def test_unsupported_collateral_and_stale_index_fail_closed():
    bundle = evidence()
    bnb = deepcopy(bundle['balances'][0])
    bundle['balances'].append(dict(bnb, asset='BNB', totalWalletBalance='0.1'))
    assert 'non_usdt_collateral_requires_separate_valuation' in audit_usdt_bridge(**bundle)['reasons']
    bundle = evidence()
    bundle['index'][0]['time'] = 900
    assert 'index_time_outside_observation_window' in audit_usdt_bridge(**bundle)['reasons']
