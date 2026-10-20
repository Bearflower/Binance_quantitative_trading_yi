#!/usr/bin/env python3
"""批量补全历史 K 线数据

用法：
python3 fetch_history_bulk.py --symbols XAUUSDT --intervals 1h,4h,1d --days 200
"""

import asyncio
import sys
import argparse
from datetime import datetime, timedelta
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


async def _collect_interval(
    collector, symbol: str, interval: str,
    actual_days: int, end_time: int, now_ms: int,
) -> int:
    """采集单个 (symbol, interval) 的完整历史区间。

    分页策略：单次请求 limit=1000，循环推进至满足任一终止条件：
      - 返回为空
      - 返回条数 < limit（已触达尾部）
      - current_start >= end_time
    """
    start_time = int((datetime.now() - timedelta(days=actual_days)).timestamp() * 1000)
    logger.info(f"采集 {symbol} {interval}（{actual_days}天）...")

    current_start = start_time
    interval_stored = 0

    while current_start < end_time:
        klines = await collector.collect_klines(
            symbol, interval,
            start_time=current_start,
            end_time=end_time,
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
) -> int:
    """获取单个交易对的历史数据。

    Args:
        symbol: 交易对
        days: 获取多少天的数据
        intervals: 采集周期列表（None 表示全部 INTERVAL_DAYS）
    """
    logger.info(f"开始获取 {symbol} 的历史数据（{days}天）...")

    collector = None
    binance_client = None
    try:
        collector, binance_client = await _build_collector()

        end_time = int(datetime.now().timestamp() * 1000)
        target_intervals = (
            intervals if intervals is not None else list(INTERVAL_DAYS.keys())
        )
        total_stored = 0

        for interval in target_intervals:
            interval_days = INTERVAL_DAYS[interval]
            actual_days = min(days, interval_days)
            if actual_days <= 0:
                continue

            stored = await _collect_interval(
                collector, symbol, interval, actual_days, end_time, end_time,
            )
            total_stored += stored
    finally:
        await _cleanup(binance_client)

    logger.info(f"✅ {symbol} 历史数据采集完成，共存储 {total_stored} 条")
    return total_stored


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='批量补全历史 K 线数据')
    parser.add_argument(
        '--symbols', type=str, default=','.join(DEFAULT_SYMBOLS),
        help=f'交易对列表，逗号分隔（默认：{",".join(DEFAULT_SYMBOLS)}）',
    )
    parser.add_argument(
        '--days', type=int, default=30, help='获取多少天的数据（默认 30）',
    )
    parser.add_argument(
        '--intervals', type=str, default=None,
        help='采集周期，逗号分隔（如 1h,4h,1d）；未提供则采集全部周期',
    )

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(',')]
    days = args.days
    intervals = _parse_intervals(args.intervals) if args.intervals else None

    logger.info("开始批量补全历史数据")
    logger.info("交易对：%s", symbols)
    logger.info("天数：%s", days)
    logger.info("周期：%s", intervals if intervals else "全部")

    total = 0
    for symbol in symbols:
        try:
            stored = await fetch_symbol_history(symbol, days, intervals)
            total += stored
            await asyncio.sleep(1)  # 交易对之间休息，避免请求过快
        except Exception as e:
            logger.error("❌ %s 数据采集失败：%s", symbol, e)
            import traceback
            traceback.print_exc()

    logger.info("\n%s", '=' * 60)
    logger.info("✅ 所有交易对历史数据采集完成！共存储 %s 条数据", total)
    logger.info("%s", '=' * 60)


if __name__ == "__main__":
    asyncio.run(main())
