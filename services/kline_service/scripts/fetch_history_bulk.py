#!/usr/bin/env python3
"""批量补全历史 K 线数据

用法：
python3 fetch_history_bulk.py --symbols XAUUSDT --intervals 1h,4h,1d --days 200
"""

from __future__ import annotations

import asyncio
import sys
import json
import argparse
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 添加项目根目录（services/kline_service/）与共享模块路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from shared.core.database import db_manager
from core.binance_client import BinanceClient
from core.collector import KlineCollector
from core.registry import registry
from shared.utils.logger import get_logger

logger = get_logger(__name__)

# 默认交易对列表
DEFAULT_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

# 时间周期和对应的数据天数（cap 上限，受 --days 共同约束）
INTERVAL_DAYS = {
    '1d': 200,   # 日线采集 200 天
    '4h': 30,    # 4 小时采集 30 天
    '1h': 15,    # 1 小时采集 15 天
    '15m': 7,    # 15 分钟采集 7 天
    '5m': 3,     # 5 分钟采集 3 天
    '1m': 1,     # 1 分钟采集 1 天
}

# interval → 秒数（用于精确区间模式下 end 向下对齐到 interval 边界）
INTERVAL_SECONDS = {
    '1m': 60,
    '5m': 300,
    '15m': 900,
    '1h': 3600,
    '4h': 14400,
    '1d': 86400,
}

# 单次请求条数上限（币安 API 硬限 1000）
BATCH_LIMIT = 1000


def _parse_intervals(raw: str) -> list:
    """解析 CLI --intervals 参数，过滤合法值。

    选择「报错退出」而非告警跳过：用户显式指定 intervals 时，静默跳过
    非法值会造成"命令看似成功但实际漏采"的假象，风险远大于立即失败。
    同时对输入保序去重，避免重复采同一周期。
    """
    selected = [s.strip() for s in raw.split(',') if s.strip()]
    valid = set(INTERVAL_DAYS.keys())
    unknown = [i for i in selected if i not in valid]
    if unknown:
        logger.error(f"无法识别的 interval：{unknown}，有效值：{sorted(valid)}")
        sys.exit(2)
    # 保序去重：同一用户可能意外重复指定，避免重复采集
    deduped = list(dict.fromkeys(selected))
    if not deduped:
        logger.error("未解析出任何有效 interval，请检查 --intervals 参数")
        sys.exit(2)
    return deduped


def _filter_unclosed(klines: list, now_ms: int) -> list:
    """过滤尚未闭合的 K 线（close_time >= 当前时间）"""
    return [k for k in klines if k.close_time < now_ms]


def _parse_iso_utc(raw: str, label: str) -> datetime:
    """解析 ISO 8601 UTC 字符串，必须带 Z 或 +00:00 后缀。

    拒绝不带时区的"naive"字符串（如 2026-10-21T00:00:00），
    避免把本地时间误当作 UTC 截数据。
    """
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(
            f"{label} 必须为 ISO 8601 UTC 字符串（带 Z 或 +00:00），如 2026-10-21T00:00:00Z"
        ) from exc
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError(f"{label} 必须带 UTC 时区（Z 或 +00:00），不能用本地时区")
    return dt.astimezone(timezone.utc)


def _align_end_down(end_dt: datetime, interval: str) -> datetime:
    """把 end 按 interval 向下对齐，只采闭合 K 线。

    例：end=2026-10-21T00:30:00Z，interval=1h → 2026-10-21T00:00:00Z。
    """
    sec = INTERVAL_SECONDS[interval]
    aligned_epoch = int(end_dt.timestamp()) // sec * sec
    return datetime.fromtimestamp(aligned_epoch, tz=timezone.utc)


def _resolve_window(args) -> tuple | None:
    """解析 --start/--end/--days 的组合，做互斥校验。

    Returns:
        None → 走旧的 --days 路径（args.days 由 main 回退到默认 30）
        (start_dt, end_dt) → 精确区间模式

    Raises:
        SystemExit → 通过 parser.error(...) 触发 exit(2)，交给 main 向上抛。
    """
    has_days = args.days is not None
    has_start = args.start is not None
    has_end = args.end is not None

    if has_start != has_end:
        # 只给其中一个 → 必须成对出现
        raise ValueError(
            "--start 与 --end 必须同时提供（精确区间模式），不要只给一个"
        )

    if not has_start and not has_end:
        # 两个都没给 → 走旧的 --days 路径
        # （即使 --days 也没给，main 会回退到默认 30，这里不报错）
        return None

    # 精确区间模式：start 和 end 都给了
    if has_days:
        raise ValueError(
            "--days 与 --start/--end 互斥，只能选一种方式指定采集范围"
        )

    start_dt = _parse_iso_utc(args.start, "--start")
    end_dt = _parse_iso_utc(args.end, "--end")
    if end_dt <= start_dt:
        raise ValueError(
            f"--end({args.end}) 必须晚于 --start({args.start})"
        )
    return start_dt, end_dt


async def _collect_interval(
    collector, symbol: str, interval: str,
    start_ms: int, end_ms: int, now_ms: int,
) -> int:
    """对给定的 [start_ms, end_ms) 区间做分页采集并入库闭合 K 线。

    终止条件（任一满足）即退出分页循环：
      - collector 返回空列表
      - collector 返回条数 < BATCH_LIMIT（币安侧已达尾部）
      - current_start >= end_ms
    """
    logger.info(f"采集 {symbol} {interval}（区间 {start_ms} → {end_ms}）...")

    current_start = start_ms
    interval_stored = 0

    while current_start < end_ms:
        klines = await collector.collect_klines(
            symbol, interval,
            start_time=current_start,
            # Binance endTime 包含端点；减 1ms 才能保持本函数的 [start,end) 契约
            end_time=end_ms - 1,
            limit=BATCH_LIMIT,
        )

        if not klines:
            logger.warning(f"未获取到 {symbol} {interval} 的数据，终止该周期")
            break

        # 闭合过滤：只入库 close_time < 当前时间的 K 线
        closed = _filter_unclosed(klines, now_ms)
        if closed:
            stored = await collector.store_klines(closed)
            interval_stored += stored

        # 终止条件二：返回条数不足 limit（币安侧已达尾部）
        if len(klines) < BATCH_LIMIT:
            break

        # 按最后一根 K 线的 close_time + 1 推进下一批起点
        current_start = klines[-1].close_time + 1
        await asyncio.sleep(0.2)  # 避免请求过快

    logger.info(f"✅ {symbol} {interval}: 存储 {interval_stored} 条")
    return interval_stored


def _resolve_interval_range(
    interval: str, is_exact: bool,
    start_dt: datetime | None, end_dt: datetime | None,
    days: int,
) -> tuple[int, int] | None:
    """为单个 interval 推导 (start_ms, end_ms)，返回 None 表示跳过。

    - 精确区间模式：起点按 interval 向上对齐，终点向下对齐（只采闭合 K 线）
    - 旧 --days 模式：起点由 days 推 + INTERVAL_DAYS cap，终点为现在
    """
    if is_exact:
        assert start_dt is not None and end_dt is not None
        sec = INTERVAL_SECONDS[interval]
        start_epoch = int(math.ceil(start_dt.timestamp() / sec) * sec)
        start_ms = start_epoch * 1000
        end_ms = int(_align_end_down(end_dt, interval).timestamp()) * 1000
        if end_ms <= start_ms:
            return None  # 区间内无闭合 K 线，跳过
        return start_ms, end_ms

    interval_days = INTERVAL_DAYS[interval]
    actual_days = min(days, interval_days)
    if actual_days <= 0:
        return None
    start_ms = int((datetime.now() - timedelta(days=actual_days)).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return start_ms, end_ms


async def _build_collector():
    """初始化所有外部依赖并构造 KlineCollector。

    注意顺序：db.connect 必须在 registry.initialize 之前（后者依赖已连接的
    数据库读取白名单），registry.initialize 必须在任何采集操作之前（否则
    build_kline_table_name 白名单校验会失败——修复独立进程白名单阻断问题）。

    失败路径自回滚：中间步骤抛异常时，主动关闭已建立的 db/binance 连接，
    避免调用方未进入 try/finally 时资源泄漏（defect #2 fix）。
    """
    binance_client = None
    try:
        await db_manager.connect()
        logger.info("✅ 数据库连接成功")
        await registry.initialize()
        logger.info("✅ 标的注册表已初始化")

        binance_client = BinanceClient()
        await binance_client.connect()
        logger.info("✅ 币安 API 连接成功")

        collector = KlineCollector(
            binance_client=binance_client,
            db=db_manager,
            symbols=[],
            intervals=[],
            strict_errors=True,
        )
        return collector, binance_client
    except Exception:
        # 自回滚：关闭已建立的连接，再抛
        if binance_client is not None:
            try:
                await binance_client.disconnect()
            except Exception:
                pass
        try:
            await db_manager.disconnect()
        except Exception:
            pass
        raise


async def _cleanup(binance_client):
    """资源释放：关闭币安连接 + 数据库连接。

    binance_client 为 None 时跳过（对应 _build_collector 在 binance.connect
    之前就失败的场景）；db_manager.disconnect 即使未 connect 也安全（幂等）。
    """
    if binance_client is not None:
        await binance_client.disconnect()
    await db_manager.disconnect()


async def fetch_symbol_history(
    symbol: str, days: int = 30, intervals: list = None,
    start_dt: datetime | None = None, end_dt: datetime | None = None,
) -> tuple:
    """获取单个交易对的历史数据（精确区间或 --days 路径）。

    Returns:
        (total_stored, failures) —— failures 为 list[dict]，项含 symbol/interval/error
    """
    is_exact = start_dt is not None and end_dt is not None
    mode_label = (
        f"精确区间 {start_dt} → {end_dt}" if is_exact else f"{days}天"
    )
    logger.info(f"开始获取 {symbol} 的历史数据（{mode_label}）...")

    collector = binance_client = None
    total_stored = 0
    failures: list[dict] = []
    try:
        collector, binance_client = await _build_collector()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        target_intervals = (
            intervals if intervals is not None else list(INTERVAL_DAYS.keys())
        )
        for interval in target_intervals:
            try:
                range_res = _resolve_interval_range(
                    interval, is_exact, start_dt, end_dt, days,
                )
                if range_res is None:
                    continue
                start_ms, end_ms = range_res
                stored = await _collect_interval(
                    collector, symbol, interval, start_ms, end_ms, now_ms,
                )
                total_stored += stored
            except Exception as exc:
                failures.append({
                    'symbol': symbol, 'interval': interval,
                    'error': f"{type(exc).__name__}: {exc}",
                })
    finally:
        await _cleanup(binance_client)

    logger.info(f"✅ {symbol} 历史数据采集完成，共存储 {total_stored} 条")
    return total_stored, failures


async def _collect_all_symbols(
    symbols: list, days: int, intervals: list | None,
    is_exact: bool, start_dt: datetime | None, end_dt: datetime | None,
) -> tuple[int, list[dict]]:
    """遍历 symbols，调用 fetch_symbol_history，聚合 (total, all_failures)。

    单个 symbol 整体抛异常时，按全部 interval 兜底记录失败条目。
    """
    total = 0
    all_failures: list[dict] = []
    covered_intervals = (
        intervals if intervals is not None else list(INTERVAL_DAYS.keys())
    )
    for symbol in symbols:
        try:
            if is_exact:
                stored, failures = await fetch_symbol_history(
                    symbol, days=0, intervals=intervals,
                    start_dt=start_dt, end_dt=end_dt,
                )
            else:
                stored, failures = await fetch_symbol_history(
                    symbol, days=days, intervals=intervals,
                )
            total += stored
            all_failures.extend(failures)
            await asyncio.sleep(1)  # 交易对之间休息，避免请求过快
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("❌ %s 数据采集整体失败：%s", symbol, error_msg)
            for iv in covered_intervals:
                all_failures.append({
                    'symbol': symbol, 'interval': iv, 'error': error_msg,
                })
    return total, all_failures


def _print_summary(total: int, failures: list[dict]) -> int:
    """打印采集汇总日志；返回 0（全部成功）或 1（有失败）。"""
    print("")
    logger.info("%s", '=' * 60)
    if failures:
        logger.error(
            "❌ 批量采集完成但有 %d 个 (symbol, interval) 失败；共存储 %d 条",
            len(failures), total,
        )
        logger.error("%s", '=' * 60)
        print(json.dumps(failures, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    logger.info("✅ 所有交易对历史数据采集完成！共存储 %s 条数据", total)
    logger.info("%s", '=' * 60)
    return 0


async def main() -> int:
    """主函数；返回退出码（0 全部成功 / 1 有失败 / 2 参数错误）。"""
    parser = argparse.ArgumentParser(description='批量补全历史 K 线数据')
    parser.add_argument(
        '--symbols', type=str, default=','.join(DEFAULT_SYMBOLS),
        help=f'交易对列表，逗号分隔（默认：{",".join(DEFAULT_SYMBOLS)}）',
    )
    parser.add_argument(
        '--days', type=int, default=None,
        help='获取多少天的数据（默认 30）；与 --start/--end 互斥',
    )
    parser.add_argument(
        '--intervals', type=str, default=None,
        help='采集周期，逗号分隔（如 1h,4h,1d）；未提供则采集全部周期',
    )
    parser.add_argument(
        '--start', type=str, default=None,
        help='精确区间起点 ISO 8601 UTC（如 2026-10-01T00:00:00Z）；必须与 --end 同时使用，与 --days 互斥',
    )
    parser.add_argument(
        '--end', type=str, default=None,
        help='精确区间终点 ISO 8601 UTC（如 2026-10-21T00:00:00Z）；必须与 --start 同时使用，与 --days 互斥',
    )

    args = parser.parse_args()
    try:
        window = _resolve_window(args)
    except ValueError as exc:
        parser.error(str(exc))  # 内部 sys.exit(2)

    symbols = [s.strip() for s in args.symbols.split(',')]
    intervals = _parse_intervals(args.intervals) if args.intervals else None
    is_exact = window is not None
    days = args.days if (not is_exact and args.days is not None) else 30
    start_dt, end_dt = window if is_exact else (None, None)

    mode_label = (
        f"精确区间 {start_dt.isoformat()} → {end_dt.isoformat()}"
        if is_exact else f"{days}天"
    )
    logger.info("开始批量补全历史数据（%s）", mode_label)
    logger.info("交易对：%s", symbols)
    logger.info("周期：%s", intervals if intervals else "全部")

    total, failures = await _collect_all_symbols(
        symbols, days, intervals, is_exact, start_dt, end_dt,
    )
    return _print_summary(total, failures)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
