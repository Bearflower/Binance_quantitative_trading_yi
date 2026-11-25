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
    folder = tmp_path / "release"
    folder.mkdir()
    timer_state = {"ActiveState": "active", "UnitFileState": "enabled"}
    def mock_run(*args):
        calls.append(args)
        if args[:3] == ("systemctl","show",timer.TIMER):
            return timer_state[args[4]]
        if len(args) == 3 and args[0] == "systemctl" and args[2] == timer.TIMER:
            action = args[1]
            if action in {"start", "stop"}:
                timer_state["ActiveState"] = "active" if action == "start" else "inactive"
            elif action in {"enable", "disable"}:
                timer_state["UnitFileState"] = "enabled" if action == "enable" else "disabled"
            return ""
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
        (timer.Path(env["ATTRIBUTION_OUTPUT"])).write_text(json.dumps(proof))
    monkeypatch.setattr(timer.subprocess,"run",shadow)
    return SimpleNamespace(digest=digest,revision=revision,folder=folder,timer_state=timer_state,override=override,calls=calls,proof=proof)


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

def test_pause_survives_reboot_and_revision_change(host):
    (host.folder / "income-timer-previous-state").write_text("active")
    host.timer_state["ActiveState"] = "inactive"
    timer.paused_state(host.folder)
    assert host.timer_state == {"ActiveState": "inactive", "UnitFileState": "disabled"}
    other_release = host.folder.parent / "next-release"
    other_release.mkdir()
    timer.resume(other_release, host.revision)
    assert host.timer_state == {"ActiveState": "active", "UnitFileState": "enabled"}


def test_originally_disabled_timer_stays_disabled(host):
    host.timer_state.update(ActiveState="inactive", UnitFileState="disabled")
    timer.resume(host.folder, host.revision)
    assert host.timer_state == {"ActiveState": "inactive", "UnitFileState": "disabled"}
