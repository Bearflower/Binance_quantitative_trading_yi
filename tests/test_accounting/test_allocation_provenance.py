from decimal import Decimal
import pytest
from shared.accounting.allocation_provenance import entries_digest, validated_holdings_limit


def facts():
    old={'id':7,'entries':[{'strategy_id':'hrs','allocated_amount':30,'holdings_floor_usdt':'40','publication_method':'classic_history_acceptance_v1'}]}
    new={'strategy_id':'hrs','allocated_amount':20,'holdings_floor_usdt':'40','publication_method':'classic_history_acceptance_v1',
         'publication_method':'classic_history_acceptance_v1','previous_allocation_id':7,
         'previous_entries_sha256':entries_digest(old['entries'])}
    return old,new


def test_floor_does_not_reduce_holdings_or_expand_entry_value():
    old,new=facts()
    assert validated_holdings_limit(new,old)==Decimal('40')
    assert new['allocated_amount']==20


@pytest.mark.parametrize('field,value', [('holdings_floor_usdt','41'),('holdings_floor_usdt','NaN'),
    ('previous_allocation_id',8),('previous_entries_sha256','f'*64),('allocated_amount',-1),
    ('publication_method','unknown')])
def test_broken_provenance_cannot_authorize_close(field,value):
    old,new=facts();new[field]=value
    with pytest.raises(ValueError):validated_holdings_limit(new,old)


def test_json_representation_and_key_order_do_not_change_hash():
    import json
    old,_=facts()
    assert entries_digest(old['entries'])==entries_digest(json.dumps(old['entries']))


def test_duplicate_owner_is_not_first_match():
    old,new=facts();old['entries']*=2
    new['previous_entries_sha256']=entries_digest(old['entries'])
    with pytest.raises(ValueError):validated_holdings_limit(new,old)
