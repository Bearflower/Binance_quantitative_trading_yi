import asyncio
import sys
import structlog

from services.kline_monitor.src import monitor as module


def test_healthy_poll_emits_container_log(tmp_path, monkeypatch, capsys):
    # 固定容器输出端，避免其他测试的全局 structlog 配置改变本用例。
    monkeypatch.setattr(module, "logger", structlog.wrap_logger(
        structlog.PrintLogger(file=sys.stdout), processors=[structlog.processors.JSONRenderer(ensure_ascii=False)],
    ))
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
