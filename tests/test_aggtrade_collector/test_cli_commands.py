"""CLI 全子命令集成：后台线程假币安 + sync 测试。

CLI 内部 asyncio.run，故测试不能在事件循环内。

覆盖 init-db/manifest（空库）/backfill（默认参数与显式窗口）/incremental/materialize/cleanup
在编排层的真实接线；字段口径由其余测试文件保证。
"""
import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import yaml
import pytest
from aiohttp import web

from services.aggtrade_collector import cli
from services.aggtrade_collector.cli import main
from services.aggtrade_collector.config import load_config

HOUR = 3600 * 1000
_PKG_CONFIG = (Path(__file__).resolve().parents[2]
               / "services" / "aggtrade_collector" / "config.yaml")


def _trade_rows():
    return [{"a": i, "p": str(100 + i), "q": "1", "f": i, "l": i,
             "T": 1000 * i, "m": False} for i in range(1, 40)]


class _FakeServerThread(threading.Thread):
    """独立事件循环线程承载假服务，避免与 CLI 内 asyncio.run 争抢循环。"""

    def __init__(self, rows):
        super().__init__(daemon=True)
        self._rows = rows
        self._ready = threading.Event()
        self.port = None

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._serve())

    async def _serve(self):
        rows = self._rows

        async def handler(request):
            q = request.query
            limit = int(q.get("limit", "1000"))
            if "fromId" in q:
                body = [r for r in rows if r["a"] >= int(q["fromId"])][:limit]
            else:
                start, end = int(q["startTime"]), int(q["endTime"])
                body = [r for r in rows if start <= r["T"] <= end][:limit]
            return web.json_response(body)

        app = web.Application()
        app.router.add_get("/fapi/v1/aggTrades", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = runner.addresses[0][1]
        self._ready.set()
        try:
            await asyncio.sleep(3600)   # 守护线程，随测试进程退出
        finally:
            await runner.cleanup()

    def wait_ready(self):
        assert self._ready.wait(5), "假服务器启动超时"
        return self.port


def _write_config(tmp_path, port):
    raw = yaml.safe_load(_PKG_CONFIG.read_text(encoding="utf-8"))
    raw["rest"]["base_url"] = f"http://127.0.0.1:{port}"
    raw["rest"]["page_sleep_min_seconds"] = 0.001
    raw["rest"]["page_sleep_max_seconds"] = 0.002
    raw["rest"]["backoff_base_seconds"] = 0.01
    cfg_path = tmp_path / "collector.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg_path


def _write_daemon_config(tmp_path, port, inc, cleanup):
    """在假服务配置上覆写 daemon 周期，返回已解析的 CollectorConfig。"""
    cfg_path = _write_config(tmp_path, port)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["daemon"] = {"incremental_interval_seconds": inc,
                     "cleanup_interval_seconds": cleanup}
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(str(cfg_path), str(tmp_path))


def _recent_rows(count=39):
    """近 1 分钟内、跨秒分布的逐笔：落在常驻引导回溯窗口 (now-48h, now] 内。"""
    base = int(time.time() * 1000) - 60_000
    return [{"a": i, "p": str(100 + i), "q": "1", "f": i, "l": i,
             "T": base + i * 1000, "m": False} for i in range(1, count + 1)]


def _stub_sleep():
    async def _sleep(_seconds):
        return None
    return _sleep


def test_all_cli_commands_against_fake_server(tmp_path):
    server = _FakeServerThread(_trade_rows())
    server.start()
    port = server.wait_ready()
    cfg_path = _write_config(tmp_path, port)
    base = str(tmp_path)

    def run(*parts):
        main(["--config", str(cfg_path), "--base-dir", base, *parts])

    run("init-db")
    run("manifest")  # 空库统计：range 缺省分支
    # 默认参数回溯（now-48h~now）：假数据在 1970 年，窗口内零行，只验证默认参数接线
    run("backfill")
    # 显式 --hours 1（覆盖 hours 给定的正数分支，窗口内同样零行）
    run("backfill", "--hours", "1")
    # 显式窗口回溯：39 笔落库
    run("backfill", "--start-ms", "0", "--end-ms", str(HOUR))
    # 轨道B（不带 --until-now，走 until_ms=None 分支）：游标行幂等重复
    run("incremental")
    # 再走一轮 --until-now（until_ms=当前时刻分支）
    run("incremental", "--until-now")
    run("materialize")

    db_path = tmp_path / "data/aggtrades/ethusdt_aggtrades.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 39
        # 首点 2000 ~ close=floor(39000)=39000，共 38 个稠密决策点
        assert conn.execute(
            "SELECT COUNT(*) FROM price_samples_1s").fetchone()[0] == 38
        assert conn.execute(
            "SELECT MIN(sample_ms) FROM price_samples_1s").fetchone()[0] == 2000
        assert conn.execute(
            "SELECT MAX(sample_ms) FROM price_samples_1s").fetchone()[0] == 39000
    finally:
        conn.close()

    # 清理：1970 年逐笔相对 now-72h 全部过期，物化样本保留
    run("cleanup")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM price_samples_1s").fetchone()[0] == 38
    finally:
        conn.close()

    # 清理后清单可再次刷新（空表统计分支经 CLI 触达）
    run("manifest")


@pytest.mark.parametrize("parts", [
    ["backfill", "--hours", "0"],
    ["backfill", "--start-ms", "2000", "--end-ms", "1000"],
])
def test_backfill_bad_range_rejected(tmp_path, parts):
    """回溯参数：非正小时数、开始不早于结束一律拒绝（不发请求）。"""
    cfg_path = _write_config(tmp_path, 1)
    main(["--config", str(cfg_path), "--base-dir", str(tmp_path), "init-db"])
    with pytest.raises(ValueError):
        main(["--config", str(cfg_path), "--base-dir", str(tmp_path), *parts])


def test_incremental_on_empty_db_raises(tmp_path):
    """库内无成交时轨道B 直接拒绝（不发请求），提示先 backfill/import-sample。"""
    cfg_path = _write_config(tmp_path, 1)  # 不会真正连该端口
    main(["--config", str(cfg_path), "--base-dir", str(tmp_path), "init-db"])
    with pytest.raises(RuntimeError, match="库内无成交记录"):
        main(["--config", str(cfg_path), "--base-dir", str(tmp_path), "incremental"])


async def test_daemon_seeds_empty_db_then_cycles_with_cleanup(tmp_path):
    """轨道B 常驻：空库先回溯引导、启动即清理一次，随后按周期增量并触发周期清理。"""
    server = _FakeServerThread(_recent_rows())
    server.start()
    port = server.wait_ready()
    cfg = _write_daemon_config(tmp_path, port, inc=1, cleanup=1)

    rounds = {"n": 0}

    def should_stop():
        rounds["n"] += 1
        return rounds["n"] > 3

    await cli._run_daemon(cfg, sleep=_stub_sleep(), should_stop=should_stop)

    conn = sqlite3.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 39
        assert conn.execute(
            "SELECT COUNT(*) FROM price_samples_1s").fetchone()[0] > 0
    finally:
        conn.close()


async def test_daemon_skips_seed_and_contains_round_failure(tmp_path, monkeypatch):
    """已就绪库不再引导；单轮故障被吞并继续；清理周期未到不触发滚动清理。"""
    server = _FakeServerThread(_recent_rows())
    server.start()
    port = server.wait_ready()
    cfg = _write_daemon_config(tmp_path, port, inc=1, cleanup=100)
    # 先在事件循环内直接回溯播种（不能用 main()，否则 asyncio.run 嵌套报错）
    now = int(time.time() * 1000)
    await cli._run_backfill(cfg, now - 3600 * 1000, now)

    calls = {"seed": 0, "cleanup": 0, "rounds": 0}

    async def recording_seed(_cfg):
        calls["seed"] += 1

    async def failing_round(_cfg, _until_now):
        calls["rounds"] += 1
        raise RuntimeError("模拟限流/网络故障")

    monkeypatch.setattr(cli, "_run_backfill", recording_seed)
    monkeypatch.setattr(cli, "_run_incremental", failing_round)
    monkeypatch.setattr(cli, "cmd_cleanup", lambda _cfg: calls.__setitem__(
        "cleanup", calls["cleanup"] + 1))

    rounds = {"n": 0}

    def should_stop():
        rounds["n"] += 1
        return rounds["n"] > 2

    await cli._run_daemon(cfg, sleep=_stub_sleep(), should_stop=should_stop)

    assert calls["seed"] == 0          # 库内已有游标 → 不触发引导回溯
    assert calls["rounds"] == 2        # 故障轮次被吞，循环继续
    assert calls["cleanup"] == 1       # 仅启动那一次；周期未到不清理
    # 逐笔保留 72h：近 1 分钟的行不被清理
    conn = sqlite3.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM agg_trades").fetchone()[0] == 39
    finally:
        conn.close()


def test_serve_subcommand_wires_daemon(tmp_path, monkeypatch):
    """serve 子命令接线到常驻编排（编排内层已被覆盖，这里不真跑无限循环）。"""
    cfg_path = _write_config(tmp_path, 1)
    called = {"n": 0}

    async def fake_daemon(_cfg):
        called["n"] += 1

    monkeypatch.setattr(cli, "_run_daemon", fake_daemon)
    main(["--config", str(cfg_path), "--base-dir", str(tmp_path), "serve"])
    assert called["n"] == 1


async def test_daemon_without_stop_guard_keeps_looping(tmp_path, monkeypatch):
    """should_stop 缺省（生产形态）时循环靠 sleep 驱动，不进入退出判定。"""
    class _Stop(Exception):
        pass

    cfg = load_config(base_dir=str(tmp_path))
    rounds = []

    async def stub_seed(_cfg, _start_ms, _end_ms):
        return None

    async def stub_round(_cfg, _until_now):
        rounds.append(1)

    async def sleep(_seconds):
        if len(rounds) >= 2:
            raise _Stop

    monkeypatch.setattr(cli, "_run_backfill", stub_seed)
    monkeypatch.setattr(cli, "_run_incremental", stub_round)
    monkeypatch.setattr(cli, "cmd_cleanup", lambda _cfg: None)

    with pytest.raises(_Stop):
        await cli._run_daemon(cfg, sleep=sleep)   # 不传 should_stop
    assert len(rounds) == 2


async def test_chunks_splits_full_batches_and_remainder():
    """_chunks：定长分批，最后不足一批的余数也要产出（44->47 满批与 49 余数）。"""
    from services.aggtrade_collector.cli import _chunks

    async def gen(count):
        for i in range(count):
            yield {"i": i}

    batches = [b async for b in _chunks(gen(5), 2)]
    assert [[item["i"] for item in batch] for batch in batches] == [
        [0, 1], [2, 3], [4]]
    assert [b async for b in _chunks(gen(0), 2)] == []
