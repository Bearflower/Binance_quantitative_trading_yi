"""版本字段与 run 分段必须对应实际生产代码。"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from strategies.btc_eth.shadow import collector, main


@pytest.mark.parametrize("source", ["env", "file"])
def test_sha_only_build_populates_event_commit(monkeypatch, tmp_path, source):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)
    # 屏蔽宿主 /app/VERSION，测试真实解析器的指定输入。
    exists = collector.os.path.exists
    monkeypatch.setattr(collector.os.path, "exists", lambda p: p != "/app/VERSION" and exists(p))
    sha = "c4a0bea5b9304779e4d64a526865541afce764a6"
    if source == "env":
        monkeypatch.setenv("GIT_SHA", sha)
    else:
        (tmp_path / "VERSION").write_text(f"GIT_SHA={sha}\nDEPLOY_ID=00000601\n")
    assert collector._read_git_info() == (sha, sha)


@pytest.mark.parametrize(
    "old_commit,old_sha,new_commit,new_sha,reuse",
    [
        ("", "old-sha", "", "new-sha", False),
        ("fixed-label", "old-sha", "fixed-label", "new-sha", False),
        ("commit", "sha", "commit", "sha", True),
        ("", "", "", "", False),
        ("commit", "", "commit", "", True),
        (None, "sha", "sha", "sha", False),
        ("commit", "sha", "new-commit", "sha", False),
    ],
)
def test_run_reuse_requires_known_matching_code_identity(
    old_commit, old_sha, new_commit, new_sha, reuse
):
    repo = AsyncMock()
    repo.find_active_run.return_value = {
        "run_id": "old-run", "git_commit": old_commit, "git_sha": old_sha,
        "config_hashes": {"xau_calibrated": "frozen"},
    }
    cfg = SimpleNamespace(
        instance_id="mtcps_xau_calibrated_shadow", symbol="XAUUSDT",
        run_id_prefix="xau-calibrated-shadow", initial_virtual_equity_usdt=300,
    )
    result = asyncio.run(main._prepare_shadow_run(
        repo, cfg, now=datetime(2026, 11, 27, 14, tzinfo=timezone.utc),
        git_commit=new_commit, git_sha=new_sha,
        config_hashes={"xau_calibrated": "frozen"},
    ))
    if reuse:
        assert result == "old-run"
        repo.close_shadow_run.assert_not_awaited()
        repo.upsert_shadow_run.assert_not_awaited()
    else:
        assert result != "old-run"
        repo.close_shadow_run.assert_awaited_once_with(run_id="old-run", status="closed")
        repo.upsert_shadow_run.assert_awaited_once()
        saved = repo.upsert_shadow_run.await_args.kwargs
        assert saved["git_sha"] == new_sha
        assert saved["git_commit"] == (new_commit or new_sha)
        assert saved["config_hashes"] == {"xau_calibrated": "frozen"}


def test_same_code_with_changed_profile_does_not_reuse_run():
    repo = AsyncMock()
    repo.find_active_run.return_value = {
        "run_id": "old", "git_commit": "same", "git_sha": "same",
        "config_hashes": {"xau_original": "old-config"},
    }
    cfg = SimpleNamespace(instance_id="shadow", symbol="XAUUSDT", run_id_prefix="xau-shadow",
                          initial_virtual_equity_usdt=300)
    result = asyncio.run(main._prepare_shadow_run(
        repo, cfg, now=datetime(2026, 11, 27, tzinfo=timezone.utc),
        git_commit="same", git_sha="same", config_hashes={"xau_original": "new-config"},
    ))
    assert result != "old"
    repo.close_shadow_run.assert_awaited_once_with(run_id="old", status="closed")


@pytest.mark.parametrize("profile", ["xau_original", "xau_aggressive", "xau_calibrated"])
def test_real_runtime_assembly_propagates_sha_to_both_event_paths(monkeypatch, tmp_path, profile):
    root = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / "strategies/btc_eth/shadow/config" / f"{profile}.yaml").read_text())
    monkeypatch.setenv("SHADOW_PROFILES", profile)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.chdir(tmp_path)
    sha = "c4a0bea5b9304779e4d64a526865541afce764a6"
    (tmp_path / "VERSION").write_text(f"GIT_SHA={sha}\n")
    exists = collector.os.path.exists
    monkeypatch.setattr(collector.os.path, "exists", lambda p: p != "/app/VERSION" and exists(p))
    monkeypatch.setattr(main, "clear_readiness", lambda: None)
    monkeypatch.setattr(main, "_load_profile_configs", lambda: {profile: cfg})
    monkeypatch.setattr(main, "connect_db_from_env", AsyncMock)
    repo = AsyncMock()
    repo.find_active_run.return_value = None
    monkeypatch.setattr(main, "ShadowRepository", lambda db: repo)
    async def forbid_exchange():
        raise AssertionError("归因装配测试不得访问交易所")

    client = SimpleNamespace(get_account_info=forbid_exchange)
    monkeypatch.setattr(main, "_build_runtime_dependencies", lambda cfg: (client, None))
    captured = []

    async def collect_events(real_collector):
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        common = {"profile_id": profile, "scheduled_at": now, "evaluated_at": now}
        captured.append(real_collector._make_event(**common, decision="no_signal"))
        captured.append(real_collector._make_event_from_signal(**common, close_times={},
            signal_components={"direction": "LONG", "grade": "A", "score": 88.5,
                "entry_price": 4000, "initial_stop_loss": 3980, "tp1_price": 4020,
                "tp2_price": 4040, "atr": 10, "entry_intent": "trend"}))
        raise RuntimeError("测试结束装配")

    monkeypatch.setattr(main, "_collect_on_startup", collect_events)
    with pytest.raises(RuntimeError, match="测试结束装配"):
        asyncio.run(main.run_forever())
    saved = repo.upsert_shadow_run.await_args.kwargs
    assert saved["git_commit"] == saved["git_sha"] == sha
    assert [e["decision"] for e in captured] == ["no_signal", "candidate"]
    for event in captured:
        assert event["run_id"] == saved["run_id"]
        assert event["git_commit"] == sha
        assert event["feature_snapshot"]["entry_attribution"]["producer_git_commit"] == sha
