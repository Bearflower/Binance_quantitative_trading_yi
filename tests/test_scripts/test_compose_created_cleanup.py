"""Only stale, never-started Compose replacement containers may be removed."""

import json
from datetime import datetime, timedelta, timezone
from subprocess import CompletedProcess

import pytest

from scripts.cleanup_compose_created import cleanup_created_containers


NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def container(name, cid, state, service, created=NOW - timedelta(hours=1), health=None,
              config_hash="same-hash", number="1", started_at="0001-01-01T00:00:00Z"):
    result = {
        "Id": cid,
        "Name": f"/{name}",
        "Created": created.isoformat(),
        "State": {"Status": state, "StartedAt": started_at},
        "Config": {"Labels": {
            "com.docker.compose.project": "trading_system",
            "com.docker.compose.service": service,
            "com.docker.compose.config-hash": config_hash,
            "com.docker.compose.container-number": number,
        }},
    }
    if health is not None:
        result["State"]["Health"] = {"Status": health}
    return result


class FakeDocker:
    def __init__(self, created, canonical, created_on_recheck=None):
        self.created = created
        self.canonical = canonical
        self.calls = []
        self.created_on_recheck = created_on_recheck
        self.created_inspections = 0

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["docker", "ps"]:
            return CompletedProcess(args, 0, self.created["Id"] + "\n", "")
        if args[:2] == ["docker", "inspect"]:
            target = args[-1]
            if target == self.created["Id"]:
                self.created_inspections += 1
                value = self.created_on_recheck if self.created_inspections > 1 and self.created_on_recheck else self.created
            else:
                value = self.canonical
            return CompletedProcess(args, 0, json.dumps([value]), "")
        if args[:2] == ["docker", "rm"]:
            return CompletedProcess(args, 0, self.created["Id"] + "\n", "")
        raise AssertionError(args)


def test_removes_stale_created_replacement_when_canonical_is_healthy():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical)

    assert cleanup_created_containers(run=docker, now=NOW) == ["new-id"]
    assert ["docker", "rm", "new-id"] in docker.calls


def test_dry_run_reports_candidate_without_removing_it():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical)

    assert cleanup_created_containers(run=docker, now=NOW, dry_run=True) == ["new-id"]
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_keeps_recent_created_replacement():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api",
                        created=NOW - timedelta(minutes=1))
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical)

    with pytest.raises(RuntimeError, match="recent Compose replacement"):
        cleanup_created_containers(run=docker, now=NOW)
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_keeps_unrelated_created_container():
    created = container("manual-dashboard-api", "new-id", "created", "dashboard-api")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical)

    assert cleanup_created_containers(run=docker, now=NOW) == []
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_refuses_cleanup_when_canonical_is_not_healthy():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api")
    canonical = container("dashboard-api", "old-id", "exited", "dashboard-api", health="unhealthy")
    docker = FakeDocker(created, canonical)

    with pytest.raises(RuntimeError, match="not healthy"):
        cleanup_created_containers(run=docker, now=NOW)
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_refuses_cleanup_for_different_compose_replica_number():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy",
                          number="2")
    docker = FakeDocker(created, canonical)

    with pytest.raises(RuntimeError, match="does not match"):
        cleanup_created_containers(run=docker, now=NOW)
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_allows_different_config_hash_after_failed_recreate():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api",
                        config_hash="new-config")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy",
                          config_hash="old-config")
    docker = FakeDocker(created, canonical)

    assert cleanup_created_containers(run=docker, now=NOW) == ["new-id"]


def test_refuses_cleanup_for_previously_started_container():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api",
                        started_at="2026-11-02T07:00:00Z")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical)

    with pytest.raises(RuntimeError, match="previously started"):
        cleanup_created_containers(run=docker, now=NOW)
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)


def test_refuses_cleanup_if_candidate_starts_before_removal():
    created = container("34b00cc53729_dashboard-api", "new-id", "created", "dashboard-api")
    rechecked = container("34b00cc53729_dashboard-api", "new-id", "exited", "dashboard-api",
                          started_at="2026-11-02T07:00:00Z")
    canonical = container("dashboard-api", "old-id", "running", "dashboard-api", health="healthy")
    docker = FakeDocker(created, canonical, created_on_recheck=rechecked)

    with pytest.raises(RuntimeError, match="changed before removal"):
        cleanup_created_containers(run=docker, now=NOW)
    assert not any(call[:2] == ["docker", "rm"] for call in docker.calls)
