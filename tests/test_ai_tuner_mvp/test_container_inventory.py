import pytest

from ai_tuner.ops.container_inventory import partition


def container(name, ident, state="running", restart="unless-stopped"):
    return {"Id": ident, "Name": "/" + name, "State": {"Status": state},
            "HostConfig": {"RestartPolicy": {"Name": restart}}}


def registry():
    return {"version": 1, "entries": [{"id": "a" * 64, "name": "app-before-release",
            "successor": "app", "reason": "已替换，保留回滚证据"}]}


def test_only_registered_stopped_identity_is_retired():
    old = container("app-before-release", "a" * 64, "exited", "no")
    live = container("app", "b" * 64)
    unknown = container("other-probe", "c" * 64, "exited", "no")
    active, retired, issues = partition([old, live, unknown], registry())
    assert active == [live, unknown]
    assert retired == [old]
    assert issues == []


@pytest.mark.parametrize("change", ["running", "restart", "id", "name", "missing_successor"])
def test_retirement_drift_remains_visible(change):
    old = container("app-before-release", "a" * 64, "exited", "no")
    live = container("app", "b" * 64)
    if change == "running": old["State"]["Status"] = "running"
    if change == "restart": old["HostConfig"]["RestartPolicy"]["Name"] = "always"
    if change == "id": old["Id"] = "c" * 64
    if change == "name": old["Name"] = "/renamed"
    items = [old] if change == "missing_successor" else [old, live]
    active, retired, issues = partition(items, registry())
    assert old in active
    assert retired == []
    assert issues


def test_stopped_successor_is_still_monitored():
    old = container("app-before-release", "a" * 64, "exited", "no")
    live = container("app", "b" * 64, "exited")
    active, retired, issues = partition([old, live], registry())
    assert active == [live]
    assert retired == [old]


def test_absent_archived_container_is_not_an_incident():
    live = container("app", "b" * 64)
    assert partition([live], registry()) == ([live], [], [])


def test_missing_successor_alerts_even_after_old_container_was_removed():
    assert partition([], registry())[2]


def test_invalid_registry_fails_closed():
    with pytest.raises(ValueError):
        partition([], {"version": 9, "entries": []})
