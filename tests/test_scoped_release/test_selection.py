from pathlib import Path
import pytest
from scripts.select_deploy_mode import select_deploy_mode,DeployModeError

MARKER='.github/deploy-modes/ledger-risk-remediation-20261104'
@pytest.mark.parametrize('status',['M','D','R100'])
def test_non_add_remediation_marker_denied(status):
    with pytest.raises(DeployModeError):select_deploy_mode('push','a'*40,[(status,MARKER)])

def test_remediation_mode_requires_full_range_and_single_marker():
    assert select_deploy_mode('push','a'*40,[('A',MARKER)])=='ledger-risk-remediation'
    with pytest.raises(DeployModeError):select_deploy_mode('push','abc',[('A',MARKER)])

def test_renderer_rejects_scoped_funding_rotation():
    from scripts.render_remote_deploy import _validate,RenderError
    _validate('ledger-risk-remediation','a'*40,'12345678','keep')
    with pytest.raises(RenderError):_validate('ledger-risk-remediation','a'*40,'12345678','rotate')
