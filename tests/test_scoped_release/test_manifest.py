import copy
import pytest

def test_exact_scope_and_missing_production_evidence_block():
    from scripts.ledger_risk_manifest import template,validate_manifest,ReleaseBlocked
    manifest=template()
    validate_manifest(manifest)
    with pytest.raises(ReleaseBlocked,match='production evidence'):validate_manifest(manifest,activation=True)
    for key,value in [('images',['hrs']),('services',['hrs-strategy']),('schema_version',0)]:
        bad=copy.deepcopy(manifest);bad[key]=value
        with pytest.raises(ReleaseBlocked):validate_manifest(bad)

def test_compose_comparison_allows_only_image():
    from scripts.ledger_risk_manifest import require_image_only,ReleaseBlocked
    old={'image':'old','command':['heartbeat'],'logging':{'driver':'journald'},'volumes':['a:b']}
    new=dict(old,image='new')
    require_image_only(old,new)
    for key in ('command','logging','volumes'):
        bad=copy.deepcopy(new);bad[key]=None
        with pytest.raises(ReleaseBlocked):require_image_only(old,bad)

def test_collector_restoration_is_exact():
    from scripts.ledger_risk_manifest import validate_collector_restoration,COLLECTOR_RESTORATION,ReleaseBlocked
    config=copy.deepcopy(COLLECTOR_RESTORATION)
    validate_collector_restoration(config)
    for key in config:
        bad=copy.deepcopy(config);bad[key]=None
        with pytest.raises(ReleaseBlocked):validate_collector_restoration(bad)
