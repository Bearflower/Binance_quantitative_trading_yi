"""
详细分析各策略的盈亏和佣金分布（按币种、按方向）
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
    window_ms = window_days * 24 * 60 * 60 * 1000
    windows = []
    s = start_time
    while s < end_time:
        e = min(s + window_ms, end_time)
        windows.append((s, e))
        s = e + 1
    return windows


async def query_user_trades(binance_client, symbol: str, start_time: int, end_time: int) -> list:
    """查询指定交易对在时间范围内的所有成交记录"""
    all_trades = []
    for ws, we in split_into_windows(start_time, end_time, 7):
        try:
            trades = await binance_client._request(
                "GET", "/papi/v1/um/userTrades",
                {"symbol": symbol, "startTime": ws, "endTime": we, "limit": 1000},
                signed=True
            )
            if isinstance(trades, list):
                all_trades.extend(trades)
        except Exception:
            pass
    return all_trades


async def main():
    async with BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=os.getenv("BINANCE_USE_PM", "true").lower() == "true"
    ) as binance_client:

        # 连接数据库以获取策略-订单ID映射
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

            # 获取订单ID到策略的映射
            rows = await db.fetch_all(
                "SELECT strategy, order_id, symbol FROM trading.trade_records "
                "WHERE order_id IS NOT NULL AND order_id != ''"
            )
            order_to_strategy = {}
            strategy_symbols = defaultdict(set)
            for r in rows:
                order_to_strategy[r["order_id"]] = r["strategy"]
                strategy_symbols[r["strategy"]].add(r["symbol"])

            # 定义要分析的策略及其交易对
            analysis = {
                "MTPCS策略": sorted(strategy_symbols.get("MTPCS策略", set())),
                "新币做空策略": sorted(strategy_symbols.get("新币做空策略", set())),
                "HRS策略": sorted(strategy_symbols.get("HRS策略", set())),
            }

            # 按币种统计
            symbol_stats = {}
            strategy_by_symbol = {}  # symbol -> strategy (优先归属)

            for strat, symbols in analysis.items():
                for sym in symbols:
                    if sym not in strategy_by_symbol:
                        strategy_by_symbol[sym] = strat

            all_symbols = list(strategy_by_symbol.keys())

            # 查询所有成交记录
            for symbol in all_symbols:
                trades = await query_user_trades(binance_client, symbol, start_time, end_time)
                if not trades:
                    continue

                stat = {
                    "count": 0,
                    "commission": Decimal("0"),
                    "realized_pnl": Decimal("0"),
                    "buy_qty": Decimal("0"),
                    "sell_qty": Decimal("0"),
                    "buy_notional": Decimal("0"),
                    "sell_notional": Decimal("0"),
                }
                for t in trades:
                    qty = Decimal(str(t.get("qty", "0")))
                    price = Decimal(str(t.get("price", "0")))
                    commission = Decimal(str(t.get("commission", "0")))
                    pnl = Decimal(str(t.get("realizedPnl", "0")))
                    side = t.get("side", "")

                    notional = qty * price
                    stat["count"] += 1
                    stat["commission"] += commission
                    stat["realized_pnl"] += pnl

                    if side == "BUY":
                        stat["buy_qty"] += qty
                        stat["buy_notional"] += notional
                    else:
                        stat["sell_qty"] += qty
                        stat["sell_notional"] += notional

                symbol_stats[symbol] = stat

            # ========== 输出 ==========

            # 汇总各策略
            print(f"\n{'='*80}")
            print(f"                各策略详细分析（最近60天）")
            print(f"{'='*80}")

            for strat_name, symbols in analysis.items():
                print(f"\n{'─'*80}")
                print(f"  【{strat_name}】")
                print(f"{'─'*80}")

                total_count = 0
                total_commission = Decimal("0")
                total_pnl = Decimal("0")
                total_notional = Decimal("0")

                symbol_details = []
                for sym in symbols:
                    if sym in symbol_stats:
                        s = symbol_stats[sym]
                        total_count += s["count"]
                        total_commission += s["commission"]
                        total_pnl += s["realized_pnl"]
                        notional = s["buy_notional"] + s["sell_notional"]
                        total_notional += notional
                        symbol_details.append((sym, s, notional))

                # 按盈亏排序
                symbol_details.sort(key=lambda x: x[1]["realized_pnl"])

                print(f"  {'币种':<16} {'笔数':>5} {'佣金(USDT)':>12} {'盈亏(USDT)':>12} {'成交额(USDT)':>14} {'佣金率':>8}")
                print(f"  {'─'*16} {'─'*5} {'─'*12} {'─'*12} {'─'*14} {'─'*8}")
                for sym, s, notional in symbol_details:
                    rate = abs(s["commission"] / notional * 100) if notional > 0 else 0
                    pnl_str = f"{s['realized_pnl']:+.4f}"
                    print(f"  {sym:<16} {s['count']:>5} {s['commission']:>10.4f}  {pnl_str:>10}  {notional:>12.2f}  {rate:>6.3f}%")

                print(f"  {'─'*16} {'─'*5} {'─'*12} {'─'*12} {'─'*14} {'─'*8}")
                total_pnl_str = f"{total_pnl:+.4f}"
                total_rate = abs(total_commission / total_notional * 100) if total_notional > 0 else 0
                print(f"  {'合计':<16} {total_count:>5} {total_commission:>10.4f}  {total_pnl_str:>10}  {total_notional:>12.2f}  {total_rate:>6.3f}%")

                # 盈亏统计
                win_count = sum(1 for _, s, _ in symbol_details if s["realized_pnl"] > 0)
                loss_count = sum(1 for _, s, _ in symbol_details if s["realized_pnl"] < 0)
                zero_count = sum(1 for _, s, _ in symbol_details if s["realized_pnl"] == 0)
                print(f"  盈利币种: {win_count} | 亏损币种: {loss_count} | 持平: {zero_count}")

            # ========== 佣金与盈亏对比 ==========
            print(f"\n\n{'='*80}")
            print(f"                佣金与盈亏对比分析")
            print(f"{'='*80}")

            grand_total_commission = Decimal("0")
            grand_total_pnl = Decimal("0")

            for strat_name, symbols in analysis.items():
                total_commission = Decimal("0")
                total_pnl = Decimal("0")
                total_count = 0
                for sym in symbols:
                    if sym in symbol_stats:
                        s = symbol_stats[sym]
                        total_commission += s["commission"]
                        total_pnl += s["realized_pnl"]
                        total_count += s["count"]

                if total_count == 0:
                    continue

                grand_total_commission += total_commission
                grand_total_pnl += total_pnl

                pnl_note = ""
                if total_pnl > 0:
                    pnl_note = f"佣金占盈利 {abs(total_commission/total_pnl*100):.1f}%"
                elif total_pnl < 0:
                    pnl_note = f"佣金占亏损 {abs(total_commission/total_pnl*100):.1f}%"
                else:
                    pnl_note = "盈亏持平"

                print(f"\n  {strat_name}")
                print(f"    成交笔数: {total_count}")
                print(f"    总佣金:   {total_commission:.4f} USDT")
                print(f"    净盈亏:   {total_pnl:+.4f} USDT")
                print(f"    → {pnl_note}")
                print(f"    含佣净收益: {total_pnl - total_commission:+.4f} USDT")

            if grand_total_commission > 0 or grand_total_pnl != 0:
                print(f"\n  {'─'*50}")
                print(f"  总佣金:     {grand_total_commission:.4f} USDT")
                print(f"  总净盈亏:   {grand_total_pnl:+.4f} USDT")
                print(f"  含佣净收益: {grand_total_pnl - grand_total_commission:+.4f} USDT")

            # ========== 未匹配订单分析 ==========
            print(f"\n\n{'='*80}")
            print(f"                未匹配订单分析")
            print(f"{'='*80}")

            # 对于不在 trade_records 中的订单ID，尝试通过 symbol 归属
            unmatched_by_symbol = defaultdict(lambda: {"count": 0, "commission": Decimal("0"), "pnl": Decimal("0")})

            for symbol in all_symbols:
                if symbol not in symbol_stats:
                    continue
                s = symbol_stats[symbol]
                # 查询该symbol的成交记录
                trades = await query_user_trades(binance_client, symbol, start_time, end_time)
                for t in trades:
                    order_id = str(t.get("orderId", ""))
                    if order_id in order_to_strategy:
                        continue
                    commission = Decimal(str(t.get("commission", "0")))
                    pnl = Decimal(str(t.get("realizedPnl", "0")))
                    unmatched_by_symbol[symbol]["count"] += 1
                    unmatched_by_symbol[symbol]["commission"] += commission
                    unmatched_by_symbol[symbol]["pnl"] += pnl

            if unmatched_by_symbol:
                print(f"\n  以下交易对的成交记录未匹配到 trade_records 中的策略:")
                sorted_unmatched = sorted(unmatched_by_symbol.items(), key=lambda x: abs(x[1]["commission"]), reverse=True)
                print(f"  {'币种':<16} {'笔数':>5} {'佣金(USDT)':>12} {'盈亏(USDT)':>12}")
                print(f"  {'─'*16} {'─'*5} {'─'*12} {'─'*12}")
                for sym, s in sorted_unmatched:
                    if s["count"] > 0:
                        print(f"  {sym:<16} {s['count']:>5} {s['commission']:>10.4f}  {s['pnl']:>+10.4f}")

                total_unmatched = sum(s["commission"] for _, s in sorted_unmatched)
                total_unmatched_pnl = sum(s["pnl"] for _, s in sorted_unmatched)
                print(f"  {'─'*16} {'─'*5} {'─'*12} {'─'*12}")
                print(f"  未匹配合计:          {total_unmatched:>10.4f}  {total_unmatched_pnl:>+10.4f}")

        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())