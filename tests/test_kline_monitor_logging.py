import asyncio

from services.kline_monitor.src import monitor as module


def test_healthy_poll_emits_container_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(module, "STATE_FILE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(module, "NOTIFY_ON_STARTUP", False)
    monkeypatch.setattr(module, "CHECK_INTERVAL_SECONDS", 0)
    monitor = module.HealthMonitor()
    calls = 0

    async def check_health():
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "check_health", check_health)
    asyncio.run(monitor.run())

    assert calls == 2
    assert "K 线服务健康检查通过" in capsys.readouterr().out
