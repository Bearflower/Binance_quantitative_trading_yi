"""
手工补写上周 PnL 到 trading.trade_records

从币安 PM 账户收入流水查询已实现盈亏，回写到 trade_records 表。
用法: docker exec -i trading_system-btc_eth python /app/scripts/backfill_pnl.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# 确保能找到 shared 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
import structlog

logger = structlog.get_logger()

# 策略名称映射
STRATEGY_MAP = {
    "MTPCS策略": {
        "db_strategy": "MTPCS策略",
        "config_path": "strategies/btc_eth/config.yaml",
    },
    "新币做空策略": {
        "db_strategy": "新币做空策略",
        "config_path": "strategies/new_coin/config.yaml",
    },
    "HRS策略": {
        "db_strategy": "HRS策略",
        "config_path": "strategies/hrs/config.yaml",
    },
}


async def get_strategy_symbols(db: DatabaseManager, strategy_name: str, week_start: datetime, week_end: datetime) -> set:
    """获取策略在指定时间范围内交易过的所有交易对"""
    records = await db.fetch_all(
        "SELECT DISTINCT symbol FROM trading.trade_records "
        "WHERE strategy = $1 AND executed_at >= $2 AND executed_at < $3",
        strategy_name, week_start, week_end
    )
    return {r["symbol"] for r in records if r.get("symbol")}


async def backfill_pnl():
    """主流程"""
    # 时间范围：上周一 ~ 本周一
    now = datetime.now()
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = this_monday - timedelta(days=7)
    week_end = this_monday

    # 转换为毫秒时间戳（UTC）
    week_start_ms = int(week_start.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    week_end_ms = int(week_end.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)

    # 连接数据库
    db = DatabaseManager(
        host=os.getenv("DB_HOST", "postgres"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "trading_platform"),
        user=os.getenv("DB_USER", "trading_user"),
        password=os.getenv("DB_PASSWORD", "trading_pass"),
    )
    await db.connect()

    # 初始化 BinanceClient
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = BinanceClient(api_key, api_secret, use_unified_account=True)
    await client._init_session()

    try:
        # 查询收入流水
        logger.info("查询币安收入流水", week_start=week_start.isoformat(), week_end=week_end.isoformat())
        income_records = await client.get_income_history(
            start_time=week_start_ms,
            end_time=week_end_ms,
            income_type="REALIZED_PNL",
            limit=1000
        )
        logger.info("获取到收入记录", count=len(income_records))

        if not income_records:
            logger.warning("未获取到任何收入记录")
            return

        # 按交易对聚合 PnL
        symbol_pnl: dict = {}
        for rec in income_records:
            symbol = rec.get("symbol", "")
            income = float(rec.get("income", 0))
            if symbol not in symbol_pnl:
                symbol_pnl[symbol] = []
            symbol_pnl[symbol].append({
                "income": income,
                "time": rec.get("time", 0),
                "income_type": rec.get("incomeType", ""),
            })

        logger.info("收入记录按交易对聚合", symbols=list(symbol_pnl.keys()))

        # 对每个策略，查找没有 PnL 的记录并补写
        for strategy_name, strategy_info in STRATEGY_MAP.items():
            db_strategy = strategy_info["db_strategy"]
            symbols = await get_strategy_symbols(db, db_strategy, week_start, week_end)

            if not symbols:
                logger.info("策略无交易记录", strategy=strategy_name)
                continue

            logger.info("开始补写策略PnL", strategy=strategy_name, symbols=symbols)

            for symbol in symbols:
                # 查找该交易对没有 PnL 的记录（按时间降序，取最新的匹配）
                records = await db.fetch_all(
                    "SELECT id, order_id, side, order_type, quantity, executed_at "
                    "FROM trading.trade_records "
                    "WHERE strategy = $1 AND symbol = $2 "
                    "AND executed_at >= $3 AND executed_at < $4 "
                    "AND realized_pnl IS NULL "
                    "ORDER BY executed_at DESC",
                    db_strategy, symbol, week_start, week_end
                )

                if not records:
                    continue

                # 从收入流水中获取该交易对的 PnL
                pnl_entries = symbol_pnl.get(symbol, [])
                if not pnl_entries:
                    logger.info("无收入记录", strategy=strategy_name, symbol=symbol)
                    continue

                # 计算总 PnL
                total_pnl = sum(e["income"] for e in pnl_entries)
                logger.info(
                    "找到收入记录",
                    strategy=strategy_name,
                    symbol=symbol,
                    pnl_entries=len(pnl_entries),
                    total_pnl=round(total_pnl, 4),
                    trade_records=len(records)
                )

                # 按时间匹配：将每条收入记录匹配到最近的 trade_records 记录
                remaining_pnl = total_pnl
                for i, rec in enumerate(records):
                    if i >= len(pnl_entries):
                        break
                    pnl_entry = pnl_entries[i] if i < len(pnl_entries) else pnl_entries[-1]
                    pnl_value = pnl_entry["income"]

                    # 更新 realized_pnl
                    await db.execute(
                        "UPDATE trading.trade_records SET realized_pnl = $1 WHERE id = $2",
                        str(pnl_value),
                        rec["id"]
                    )
                    logger.info(
                        "补写PnL成功",
                        strategy=strategy_name,
                        symbol=symbol,
                        record_id=rec["id"],
                        pnl=round(pnl_value, 4)
                    )

                # 如果有剩余 PnL 未匹配，写入到最新的记录
                matched_count = min(len(records), len(pnl_entries))
                if matched_count < len(records) and total_pnl != 0:
                    # 将剩余总 PnL 写到最后一条记录
                    remaining = total_pnl - sum(e["income"] for e in pnl_entries[:matched_count])
                    if remaining != 0:
                        await db.execute(
                            "UPDATE trading.trade_records SET realized_pnl = $1 WHERE id = $2",
                            str(remaining),
                            records[-1]["id"]
                        )
                        logger.info(
                            "补写剩余PnL",
                            strategy=strategy_name,
                            symbol=symbol,
                            record_id=records[-1]["id"],
                            pnl=round(remaining, 4)
                        )

        logger.info("PnL补写完成")

    finally:
        await client.close()
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(backfill_pnl())