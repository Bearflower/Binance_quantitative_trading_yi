"""
查询各策略实际产生的佣金
- 使用 income 接口获取总佣金和总盈亏
- 使用 userTrades 接口（7天窗口）按策略分组统计
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager


def split_into_windows(start_time: int, end_time: int, window_days: int = 7):
    """将时间范围拆分为多个窗口（API限制7天）"""
    window_ms = window_days * 24 * 60 * 60 * 1000
    windows = []
    s = start_time
    while s < end_time:
        e = min(s + window_ms, end_time)
        windows.append((s, e))
        s = e + 1
    return windows


async def main():
    async with BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=os.getenv("BINANCE_USE_PM", "true").lower() == "true"
    ) as binance_client:

        db = DatabaseManager(
            host=os.getenv("DATABASE_HOST", "postgres"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME", "trading_platform"),
            user=os.getenv("DATABASE_USER", "trading_user"),
            password=os.getenv("DATABASE_PASSWORD", "trading_password_2024")
        )
        await db.connect()

        try:
            end_time = int(datetime.now().timestamp() * 1000)
            start_time = int((datetime.now() - timedelta(days=60)).timestamp() * 1000)

            # 1. 从数据库获取订单ID到策略的映射
            rows = await db.fetch_all(
                "SELECT strategy, order_id, symbol FROM trading.trade_records "
                "WHERE order_id IS NOT NULL AND order_id != ''"
            )
            order_to_strategy = {}
            strategy_symbols = defaultdict(set)
            strategy_order_ids = defaultdict(set)
            for r in rows:
                oid = r["order_id"]
                strat = r["strategy"]
                sym = r["symbol"]
                order_to_strategy[oid] = strat
                strategy_symbols[strat].add(sym)
                strategy_order_ids[strat].add(oid)

            print(f"\n数据库记录统计:")
            for s in sorted(strategy_order_ids.keys()):
                print(f"  {s}: {len(strategy_order_ids[s])} 个订单ID, {len(strategy_symbols[s])} 个交易对")

            # 2. 通过 income 接口获取总佣金和总盈亏
            print(f"\n{'='*70}")
            print(f"一、收入历史汇总 (最近60天)")
            print(f"{'='*70}")

            income_totals = {}
            for income_type in ["COMMISSION", "REALIZED_PNL", "FUNDING_FEE"]:
                total = Decimal("0")
                count = 0
                for ws, we in split_into_windows(start_time, end_time, 7):
                    try:
                        data = await binance_client._request(
                            "GET", "/papi/v1/um/income",
                            {
                                "incomeType": income_type,
                                "startTime": ws,
                                "endTime": we,
                                "limit": 1000
                            },
                            signed=True
                        )
                        if isinstance(data, list):
                            for item in data:
                                total += Decimal(str(item.get("income", "0")))
                                count += 1
                    except Exception as e:
                        print(f"  {income_type} [{ws}-{we}]: 查询失败 - {e}")

                income_totals[income_type] = total
                print(f"  {income_type}: {total:.4f} USDT ({count} 条)")

            # 3. 通过 userTrades 按策略分组统计
            print(f"\n{'='*70}")
            print(f"二、按策略分组统计 (最近60天, 7天窗口)")
            print(f"{'='*70}")

            # 只查询有订单ID的symbol（减少API调用）
            all_symbols = set()
            for syms in strategy_symbols.values():
                all_symbols.update(syms)

            print(f"涉及交易对: {len(all_symbols)} 个")

            strategy_commission = defaultdict(lambda: {"commission": Decimal("0"), "count": 0, "pnl": Decimal("0")})
            unmatched_commission = Decimal("0")
            unmatched_count = 0
            total_trade_count = 0
            total_trade_commission = Decimal("0")

            for symbol in sorted(all_symbols):
                for ws, we in split_into_windows(start_time, end_time, 7):
                    try:
                        trades = await binance_client._request(
                            "GET", "/papi/v1/um/userTrades",
                            {
                                "symbol": symbol,
                                "startTime": ws,
                                "endTime": we,
                                "limit": 1000
                            },
                            signed=True
                        )
                    except Exception as e:
                        continue

                    if not isinstance(trades, list):
                        continue

                    for t in trades:
                        order_id = str(t.get("orderId", ""))
                        commission = Decimal(str(t.get("commission", "0")))
                        realized_pnl = Decimal(str(t.get("realizedPnl", "0")))
                        total_trade_commission += commission
                        total_trade_count += 1

                        if order_id in order_to_strategy:
                            strat = order_to_strategy[order_id]
                            strategy_commission[strat]["commission"] += commission
                            strategy_commission[strat]["count"] += 1
                            strategy_commission[strat]["pnl"] += realized_pnl
                        else:
                            unmatched_commission += commission
                            unmatched_count += 1

            for strat in sorted(strategy_commission.keys()):
                sc = strategy_commission[strat]
                pnl_label = f"盈亏: {sc['pnl']:.4f} USDT"
                if sc["pnl"] != 0:
                    ratio = abs(sc["commission"] / sc["pnl"] * 100)
                    pnl_label += f" (佣金占比: {ratio:.2f}%)"
                print(f"\n  [{strat}]")
                print(f"    成交笔数: {sc['count']}")
                print(f"    总佣金:   {sc['commission']:.4f} USDT")
                print(f"    {pnl_label}")

            if unmatched_count > 0:
                print(f"\n  [未匹配订单]")
                print(f"    成交笔数: {unmatched_count}")
                print(f"    总佣金:   {unmatched_commission:.4f} USDT")

            print(f"\n{'-'*70}")
            print(f"userTrades 统计合计: {total_trade_commission:.4f} USDT ({total_trade_count} 笔)")
            print(f"income COMMISSION:   {income_totals['COMMISSION']:.4f} USDT")
            print(f"income REALIZED_PNL: {income_totals['REALIZED_PNL']:.4f} USDT")
            print(f"income FUNDING_FEE:  {income_totals['FUNDING_FEE']:.4f} USDT")

        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())