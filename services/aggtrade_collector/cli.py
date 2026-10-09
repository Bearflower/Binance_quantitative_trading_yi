"""aggTrades 落库器命令行入口（M0 数据通路）。

用法（仓库根目录）：
    python3 -m services.aggtrade_collector.cli init-db
    python3 -m services.aggtrade_collector.cli import-sample
    python3 -m services.aggtrade_collector.cli backfill --hours 48
    python3 -m services.aggtrade_collector.cli incremental --until-now
    python3 -m services.aggtrade_collector.cli materialize
    python3 -m services.aggtrade_collector.cli cleanup
    python3 -m services.aggtrade_collector.cli manifest
    python3 -m services.aggtrade_collector.cli serve        # 轨道B 常驻（B8 部署件）

只采集行情，不调用交易接口（FR-03）；常驻形态仅 serve 子命令，其余为一次性运维命令。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, List, Optional, Sequence

from . import manifest as manifest_mod
from . import repository as repo
from .config import CollectorConfig, load_config
from .importer import load_sample_trades
from .rest_client import AggTradesRESTClient

logger = logging.getLogger("aggtrade_collector")


def _open(cfg: CollectorConfig) -> sqlite3.Connection:
    conn = repo.open_db(cfg.db_path, cfg.busy_timeout_ms)
    logger.info("行情库就绪: %s", cfg.db_path)
    return conn


async def _chunks(items: AsyncIterator[Dict], size: int) -> AsyncIterator[List[Dict]]:
    """把异步成交流缓冲成定长批次。"""
    batch: List[Dict] = []
    # CPython 3.9 下 coverage 不记录 async-for 头到循环尾的退出弧（已实测空/非空皆然）
    async for item in items:  # pragma: no branch
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _commit_trades(conn: sqlite3.Connection, trades: Sequence[Dict]) -> int:
    """单事务写一批逐笔（每页/每批提交即 checkpoint，§1.1）。"""
    with conn:
        return repo.upsert_agg_trades(conn, trades)


def _record_manifest(cfg: CollectorConfig, conn: sqlite3.Connection,
                     entry: Optional[Dict], dedup_key: Optional[str]) -> None:
    """更新清单统计并追加来源条目。"""
    data = manifest_mod.load_or_create(
        cfg.manifest_path, cfg.db_path,
        cfg.validation_start_beijing, cfg.validation_min_full_days)
    if entry is not None:
        added = manifest_mod.append_source(data, entry, dedup_key)
        logger.info("清单来源%s: %s", "已存在，保留首次记录" if not added else "已追加",
                    entry.get("source_type"))
    manifest_mod.refresh_db_stats(data, conn, cfg.symbol)
    manifest_mod.save(cfg.manifest_path, data)
    logger.info("清单已写入: %s", cfg.manifest_path)


def cmd_init_db(cfg: CollectorConfig) -> None:
    _open(cfg).close()


def cmd_import_sample(cfg: CollectorConfig, sample_path: Optional[str],
                      sha256: Optional[str]) -> None:
    path = _sample_path(cfg, sample_path)
    fingerprint = sha256 or cfg.sample_sha256
    conn = _open(cfg)
    try:
        trades = load_sample_trades(path, fingerprint, cfg.symbol, manifest_mod.now_ms())
        total = _import_in_batches(conn, trades, cfg.insert_batch_size)
        with conn:
            repo.materialize_closed_seconds(conn, cfg.symbol, manifest_mod.now_ms())
        _record_manifest(cfg, conn, _sample_entry(path, fingerprint, trades, total),
                         dedup_key="name")
        logger.info("样本导入完成: 新增 %s / 读取 %s", total, len(trades))
    finally:
        conn.close()


def _sample_path(cfg: CollectorConfig, sample_path: Optional[str]) -> Path:
    return Path(sample_path).resolve() if sample_path else cfg.sample_path


def _import_in_batches(conn: sqlite3.Connection, trades: Sequence[Dict],
                       batch_size: int) -> int:
    total = 0
    for start in range(0, len(trades), batch_size):
        total += _commit_trades(conn, trades[start:start + batch_size])
        logger.info("已写入 %s/%s", min(start + batch_size, len(trades)), len(trades))
    return total


def _sample_entry(path, fingerprint, trades, total) -> Dict:
    times = [t["trade_time_ms"] for t in trades]
    return {
        "source_type": "sample_file",
        "name": path.name,
        "sha256": fingerprint,
        "symbol": trades[0]["symbol"],
        "trade_rows_new": total,
        "time_range_ms": {"start": min(times), "end": max(times)},
        "time_range_beijing": {
            "start": manifest_mod.beijing_text(min(times)),
            "end": manifest_mod.beijing_text(max(times)),
        },
        "imported_at_ms": manifest_mod.now_ms(),
    }


def cmd_backfill(cfg: CollectorConfig, hours: Optional[int],
                 start_ms: Optional[int], end_ms: Optional[int]) -> None:
    end = end_ms or int(time.time() * 1000)
    lookback_hours = hours if hours is not None else cfg.lookback_hours
    if lookback_hours <= 0:
        raise ValueError("backfill 回溯小时数必须为正数")
    start = start_ms or (end - lookback_hours * 3600 * 1000)
    if start >= end:
        raise ValueError("backfill 开始时间必须早于结束时间")
    asyncio.run(_run_backfill(cfg, start, end))


async def _run_backfill(cfg: CollectorConfig, start_ms: int, end_ms: int) -> None:
    """轨道A：REST 1h 切片回溯落库 + 物化 + 清单记录。"""
    logger.info("轨道A 回溯: %s ~ %s",
                manifest_mod.beijing_text(start_ms), manifest_mod.beijing_text(end_ms))
    conn = _open(cfg)
    total = 0
    try:
        async with AggTradesRESTClient(cfg) as client:
            stream = client.fetch_range_agg_trades(cfg.symbol, start_ms, end_ms)
            async for batch in _chunks(stream, cfg.insert_batch_size):  # pragma: no branch
                total += _commit_trades(conn, batch)
                last_ms = batch[-1]["trade_time_ms"]
                logger.info("回溯进度: 新增累计 %s，已到 %s",
                            total, manifest_mod.beijing_text(last_ms))
        with conn:
            materialized = repo.materialize_closed_seconds(
                conn, cfg.symbol, manifest_mod.now_ms())
        _record_manifest(cfg, conn, _backfill_entry(start_ms, end_ms, total), None)
        logger.info("回溯完成: 新增 %s 笔，物化 %s 个整秒", total, materialized)
    finally:
        conn.close()


def _backfill_entry(start_ms: int, end_ms: int, total: int) -> Dict:
    return {
        "source_type": "rest_backfill",
        "trade_rows_new": total,
        "time_range_ms": {"start": start_ms, "end": end_ms},
        "time_range_beijing": {
            "start": manifest_mod.beijing_text(start_ms),
            "end": manifest_mod.beijing_text(end_ms),
        },
        "fetched_at_ms": manifest_mod.now_ms(),
    }


def cmd_incremental(cfg: CollectorConfig, until_now: bool) -> None:
    asyncio.run(_run_incremental(cfg, until_now))


async def _run_incremental(cfg: CollectorConfig, until_now: bool) -> None:
    """轨道B 一轮：fromId=库内 max 补齐到当前（常驻编排见 serve）。"""
    conn = _open(cfg)
    total = 0
    try:
        max_id = repo.get_latest_agg_trade_id(conn, cfg.symbol)
        if max_id is None:
            raise RuntimeError("库内无成交记录，请先 backfill 或 import-sample")
        until_ms = int(time.time() * 1000) if until_now else None
        async with AggTradesRESTClient(cfg) as client:
            stream = client.fetch_incremental_agg_trades(cfg.symbol, max_id, until_ms)
            async for batch in _chunks(stream, cfg.insert_batch_size):  # pragma: no branch
                total += _commit_trades(conn, batch)
        with conn:
            repo.materialize_closed_seconds(conn, cfg.symbol, manifest_mod.now_ms())
        logger.info("增量补齐完成: 新增 %s 笔", total)
    finally:
        conn.close()


def cmd_serve(cfg: CollectorConfig) -> None:
    asyncio.run(_run_daemon(cfg))


async def _run_daemon(cfg: CollectorConfig, *,
                      sleep: Callable[[float], object] = asyncio.sleep,
                      should_stop: Optional[Callable[[], bool]] = None) -> None:
    """轨道B 常驻落库（B8 部署件，计划 §0.1/§1.3）。

    启动即清理一次（重启即执行），随后按增量周期补齐、按清理周期滚动清理。
    单轮失败不杀常驻进程（§1.5 断线补齐：下一轮继续）。should_stop 仅供测试
    注入退出条件，生产不传即常驻不退出。
    """
    logger.info("轨道B 常驻启动：增量周期 %ss，清理周期 %ss",
                cfg.daemon_incremental_interval_seconds,
                cfg.daemon_cleanup_interval_seconds)
    await _ensure_seeded(cfg)
    cmd_cleanup(cfg)
    interval = cfg.daemon_incremental_interval_seconds
    elapsed = 0.0
    while should_stop is None or not should_stop():
        try:
            await _run_incremental(cfg, True)
        except Exception as exc:  # 单轮故障（限流/网络）不终止 30 天数据积累
            logger.warning("增量补齐本轮失败，%.0fs 后重试：%s", interval, exc)
        elapsed += interval
        if elapsed >= cfg.daemon_cleanup_interval_seconds:
            cmd_cleanup(cfg)
            elapsed = 0.0
        await sleep(interval)


async def _ensure_seeded(cfg: CollectorConfig) -> None:
    """空库引导：库内无游标时先回溯 lookback_hours，使增量补齐可定位（§0.1）。"""
    conn = _open(cfg)
    try:
        latest = repo.get_latest_agg_trade_id(conn, cfg.symbol)
    finally:
        conn.close()
    if latest is not None:
        return
    end = int(time.time() * 1000)
    start = end - cfg.lookback_hours * 3600 * 1000
    logger.info("库内无成交 → 初始回溯 %s 小时引导", cfg.lookback_hours)
    await _run_backfill(cfg, start, end)


def cmd_materialize(cfg: CollectorConfig) -> None:
    conn = _open(cfg)
    try:
        with conn:
            count = repo.materialize_closed_seconds(conn, cfg.symbol,
                                                    manifest_mod.now_ms())
        logger.info("物化新增 %s 个整秒", count)
    finally:
        conn.close()


def cmd_cleanup(cfg: CollectorConfig) -> None:
    conn = _open(cfg)
    try:
        result = repo.materialize_and_cleanup(
            conn, cfg.symbol, cfg.agg_trades_retention_hours, manifest_mod.now_ms())
        logger.info("清理完成: 物化 %s，删除过期逐笔 %s（保留 %sh）",
                    result["materialized"], result["deleted"],
                    cfg.agg_trades_retention_hours)
    finally:
        conn.close()


def cmd_manifest(cfg: CollectorConfig) -> None:
    conn = _open(cfg)
    try:
        _record_manifest(cfg, conn, None, None)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="grid V2.5.4 aggTrades 落库器（M0）")
    parser.add_argument("--config", help="配置文件路径（默认包内 config.yaml）")
    parser.add_argument("--base-dir", help="基准目录（覆盖相对路径解析）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    p_imp = sub.add_parser("import-sample")
    p_imp.add_argument("--sample", help="样本 JSON 路径（默认配置项）")
    p_imp.add_argument("--sha256", help="样本 SHA-256（默认配置项）")
    p_bf = sub.add_parser("backfill")
    p_bf.add_argument("--hours", type=int, help="回溯小时数（默认配置 lookback_hours）")
    p_bf.add_argument("--start-ms", type=int)
    p_bf.add_argument("--end-ms", type=int)
    p_inc = sub.add_parser("incremental")
    p_inc.add_argument("--until-now", action="store_true")
    sub.add_parser("materialize")
    sub.add_parser("cleanup")
    sub.add_parser("manifest")
    sub.add_parser("serve", help="轨道B 常驻落库（B8 部署件，容器 entrypoint）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config, args.base_dir)
    if args.command == "init-db":
        cmd_init_db(cfg)
    elif args.command == "import-sample":
        cmd_import_sample(cfg, args.sample, args.sha256)
    elif args.command == "backfill":
        cmd_backfill(cfg, args.hours, args.start_ms, args.end_ms)
    elif args.command == "incremental":
        cmd_incremental(cfg, args.until_now)
    elif args.command == "materialize":
        cmd_materialize(cfg)
    elif args.command == "cleanup":
        cmd_cleanup(cfg)
    elif args.command == "serve":
        cmd_serve(cfg)
    else:
        # subparsers required=True，到此只可能是 manifest（其余分支已穷尽）
        cmd_manifest(cfg)


if __name__ == "__main__":  # pragma: no cover
    main()
