import json
from datetime import datetime, timezone
from ai_tuner.monitor.xau_runtime import runtime_issues


def test_recreate_detected_despite_zero_restart(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    monkeypatch.setenv("XAU_RUNTIME_EVIDENCE_FILE", str(path))
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    containers = {name: dict(id="one", started_at="old", image="sha", git_sha="abc",
                             status="running", health="healthy", restart_count=0)
                  for name in ("trading_system-mtcps-xau-shadow", "trading_system-mtcps-xau-calibrated-shadow")}
    evidence = dict(sampled_at=now.isoformat(), containers=containers, journal_usage="100M")
    path.write_text(json.dumps(evidence))
    assert runtime_issues(now) == []
    containers["trading_system-mtcps-xau-calibrated-shadow"]["id"] = "two"
    path.write_text(json.dumps(evidence))
    assert any("身份变化" in issue for issue in runtime_issues(now))
