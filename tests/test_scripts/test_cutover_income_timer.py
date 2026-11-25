import json
from types import SimpleNamespace
import pytest
from scripts import cutover_income_timer as timer


@pytest.fixture
def host(tmp_path, monkeypatch):
    digest = "ghcr.io/bearflower/trading-hrs@sha256:" + "a" * 64
    revision = "b" * 40
    override = tmp_path / "override"
    monkeypatch.setattr(timer, "OVERRIDE", override)
    monkeypatch.setattr(timer, "verify_units", lambda: None)
    calls = []
    def mock_run(*args):
        calls.append(args)
        if args[:3] == ("systemctl","show",timer.TIMER):
            return "inactive" if ("systemctl","stop",timer.TIMER) in calls and ("systemctl","start",timer.TIMER) not in calls else "active"
        if args[:3] == ("systemctl","show",timer.SERVICE):
            return "inactive" if args[4]=="ActiveState" else "ATTRIBUTION_IMAGE="+digest
        if args[:3] == ("docker","image","inspect"):
            return json.dumps([digest])
        if args[:2] == ("docker","inspect"):
            return "sha256:123"
        if args[:2] == ("docker","exec"):
            return {"HRS_IMAGE_REVISION":revision,"TRADE_ACCOUNT_ID":"main","BINANCE_CLASSIC_CUTOVER_MS":"1000"}[args[-1]]
    monkeypatch.setattr(timer,"run",mock_run)
    proof = dict(account_id="main", account_mode="classic", cutover_ms=1000, window_start_ms=1000, mode="shadow")
    def shadow(args, *, env, check):
        assert args[-1]=="--current-month" and "--apply" not in args
        assert env["ATTRIBUTION_IMAGE"]==digest
        (tmp_path/"income-attribution-shadow.json").write_text(json.dumps(proof))
    monkeypatch.setattr(timer.subprocess,"run",shadow)
    return SimpleNamespace(digest=digest,revision=revision,folder=tmp_path,override=override,calls=calls,proof=proof)


def test_verified_immutable_writer_restores_previously_active_timer(host):
    assert timer.paused_state(host.folder)=="active"
    timer.resume(host.folder,host.revision)
    assert host.override.read_text()==f"[Service]\nEnvironment=ATTRIBUTION_IMAGE={host.digest}\n"
    assert ("systemctl","start",timer.TIMER) in host.calls


@pytest.mark.parametrize("field,value", [("account_id","other"),("cutover_ms",999),("account_mode","pm"),("window_start_ms",999)])
def test_wrong_mode_or_account_keeps_old_writer_paused(host,field,value):
    host.proof[field]=value
    with pytest.raises(ValueError,match="预演"):
        timer.resume(host.folder,host.revision)
    assert not host.override.exists()
    assert ("systemctl","start",timer.TIMER) not in host.calls
