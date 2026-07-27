"""
查询MTPCS策略在BTCUSDT和XRPUSDT上的每笔交易明细

功能：
1. 通过PM账户API (papi/v1/um/userTrades) 查询最近60天的成交记录
2. 从数据库 trade_records 匹配订单ID到策略名称
3. 按 realizedPnl 从小到大排序（亏损最大的排最前）
4. 输出每笔交易的时间、方向、价格、数量、佣金、已实现盈亏

使用方式：
    cd /Users/yl/vscode/Binance_quantitative_trading
    python scripts/query_mtpcs_trades.py
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


# 北京时区
BEIJING_TZ = timezone(timedelta(hours=8))

# 要查询的交易对
TARGET_SYMBOLS = ["BTCUSDT", "XRPUSDT"]

# 目标策略名称
TARGET_STRATEGY = "MTPCS策略"


def split_into_windows(start_time: int, end_time: int, window_days: int = 7):
    """
    将时间范围拆分为多个窗口

    API限制每次查询最多7天，超过7天需要分窗口查询。
    """
    window_ms = window_days * 24 * 60 * 60 * 1000
    windows = []
    s = start_time
    while s < end_time:
        e = min(s + window_ms, end_time)
        windows.append((s, e))
        s = e + 1
    return windows


def format_time(timestamp_ms: int) -> str:
    """
    将毫秒时间戳转换为北京时间的可读字符串

    Args:
        timestamp_ms: 毫秒级时间戳

    Returns:
        格式化后的时间字符串，如 "2026-07-24 15:30:00"
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def load_order_to_strategy(db_host: str) -> dict:
    """
    从数据库加载订单ID到策略的映射

    如果数据库连接失败（如本地无法直连Docker内部数据库），
    返回空字典，仅展示API数据不匹配策略。

    Args:
        db_host: 数据库主机名

    Returns:
        订单ID -> 策略名称 的映射字典
    """
    try:
        db = DatabaseManager(
            host=db_host,
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME", "trading_platform"),
            user=os.getenv("DATABASE_USER", "trading_user"),
            password=os.getenv("DATABASE_PASSWORD", "trading_password_2024")
        )
        await db.connect()

        rows = await db.fetch_all(
            "SELECT strategy, order_id, symbol FROM trading.trade_records "
            "WHERE order_id IS NOT NULL AND order_id != ''"
        )
        order_to_strategy = {}
        for r in rows:
            oid = r["order_id"]
            order_to_strategy[oid] = r["strategy"]

        await db.disconnect()
        print(f"  数据库加载成功: {len(order_to_strategy)} 条订单记录")
        return order_to_strategy
    except Exception as e:
        print(f"  数据库连接失败 (本地无法直连Docker内部数据库): {e}")
        print(f"  将跳过策略匹配，仅展示API原始数据")
        return {}


async def main():
    """主函数：查询并输出BTCUSDT和XRPUSDT的交易明细"""
    # 初始化币安客户端（PM账户模式）
    binance_client = BinanceClient(
        api_key=os.getenv("BINANCE_API_KEY"),
        api_secret=os.getenv("BINANCE_API_SECRET"),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=os.getenv("BINANCE_USE_PM", "true").lower() == "true"
    )
    await binance_client._init_session()

    try:
        # 时间范围：最近60天
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(days=60)).timestamp() * 1000)

        print(f"{'='*100}")
        print(f"  MTPCS策略交易明细查询 (BTCUSDT / XRPUSDT)")
        print(f"  时间范围: {format_time(start_time)} ~ {format_time(end_time)}")
        print(f"{'='*100}")

        # === 第一步：从数据库获取订单ID到策略的映射 ===
        print(f"\n[1/3] 加载订单ID到策略映射...")
        order_to_strategy = await load_order_to_strategy(os.getenv("DATABASE_HOST", "postgres"))

        # 只保留MTPCS策略的订单
        mtpcs_order_ids = {
            oid for oid, strat in order_to_strategy.items()
            if strat == TARGET_STRATEGY
        }
        if order_to_strategy:
            print(f"  数据库总订单数: {len(order_to_strategy)}")
            print(f"  {TARGET_STRATEGY} 订单数: {len(mtpcs_order_ids)}")
        else:
            print(f"  未加载数据库映射，所有交易将标记为'未匹配'")

        # === 第二步：查询BTCUSDT和XRPUSDT的成交记录 ===
        print(f"\n[2/3] 查询交易对成交记录...")

        all_trades = []  # 所有匹配到的交易

        for symbol in TARGET_SYMBOLS:
            print(f"  正在查询 {symbol} ...", end=" ", flush=True)
            symbol_trades = []

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
                    if isinstance(trades, list):
                        symbol_trades.extend(trades)
                except Exception as e:
                    print(f"\n  窗口 [{format_time(ws)} ~ {format_time(we)}] 查询失败: {e}", flush=True)

            print(f"共 {len(symbol_trades)} 笔成交")

            # 将每笔交易与策略匹配
            for t in symbol_trades:
                order_id = str(t.get("orderId", ""))
                strategy = order_to_strategy.get(order_id, "未匹配")
                all_trades.append({
                    "symbol": symbol,
                    "order_id": order_id,
                    "strategy": strategy,
                    "time": t.get("time", 0),
                    "side": t.get("side", ""),
                    "price": Decimal(str(t.get("price", "0"))),
                    "qty": Decimal(str(t.get("qty", "0"))),
                    "commission": Decimal(str(t.get("commission", "0"))),
                    "commissionAsset": t.get("commissionAsset", ""),
                    "realizedPnl": Decimal(str(t.get("realizedPnl", "0"))),
                    "positionSide": t.get("positionSide", ""),
                })

        # 统计
        mtpcs_trades = [t for t in all_trades if t["strategy"] == TARGET_STRATEGY]
        unmatched_trades = [t for t in all_trades if t["strategy"] == "未匹配"]
        other_trades = [t for t in all_trades if t["strategy"] not in (TARGET_STRATEGY, "未匹配")]

        print(f"\n[3/3] 分析结果汇总:")
        print(f"  {TARGET_STRATEGY}: {len(mtpcs_trades)} 笔")
        print(f"  其他策略: {len(other_trades)} 笔")
        print(f"  未匹配:   {len(unmatched_trades)} 笔")

        # === 第三步：输出MTPCS策略的交易明细（按realizedPnl从小到大排序）===
        if mtpcs_trades:
            # 按 realizedPnl 从小到大排序（亏损最大的排最前）
            mtpcs_trades.sort(key=lambda t: t["realizedPnl"])

            for symbol in TARGET_SYMBOLS:
                symbol_trades = [t for t in mtpcs_trades if t["symbol"] == symbol]
                if not symbol_trades:
                    continue

                print(f"\n{'='*100}")
                print(f"  【{TARGET_STRATEGY}】{symbol} 交易明细 (按已实现盈亏从小到大排序)")
                print(f"  共 {len(symbol_trades)} 笔交易")
                print(f"{'='*100}")

                # 汇总统计
                total_pnl = sum(t["realizedPnl"] for t in symbol_trades)
                total_commission = sum(t["commission"] for t in symbol_trades)
                win_trades = [t for t in symbol_trades if t["realizedPnl"] > 0]
                loss_trades = [t for t in symbol_trades if t["realizedPnl"] < 0]
                zero_trades = [t for t in symbol_trades if t["realizedPnl"] == 0]

                print(f"\n  汇总统计:")
                print(f"    盈利笔数: {len(win_trades)} | 亏损笔数: {len(loss_trades)} | 持平: {len(zero_trades)}")
                print(f"    总手续费: {total_commission:.4f} USDT")
                print(f"    总已实现盈亏: {total_pnl:+.4f} USDT")
                if loss_trades:
                    max_loss = min(t["realizedPnl"] for t in loss_trades)
                    print(f"    最大单笔亏损: {max_loss:+.4f} USDT")
                if win_trades:
                    max_win = max(t["realizedPnl"] for t in win_trades)
                    print(f"    最大单笔盈利: {max_win:+.4f} USDT")

                # 输出表头
                print(f"\n  {'序号':>3} {'时间':<20} {'方向':<5} {'价格':<14} {'数量':<14} {'成交额(USDT)':<16} {'手续费':<10} {'已实现盈亏':<14} {'持仓方向':<6}")
                print(f"  {'─'*3} {'─'*20} {'─'*5} {'─'*14} {'─'*14} {'─'*16} {'─'*10} {'─'*14} {'─'*6}")

                for i, t in enumerate(symbol_trades, 1):
                    notional = t["price"] * t["qty"]
                    print(
                        f"  {i:>3} "
                        f"{format_time(t['time']):<20} "
                        f"{t['side']:<5} "
                        f"{t['price']:<14.8f} "
                        f"{t['qty']:<14.4f} "
                        f"{notional:<16.2f} "
                        f"{t['commission']:<10.4f} "
                        f"{t['realizedPnl']:<+14.4f} "
                        f"{t['positionSide']:<6}"
                    )

            # 全局汇总
            print(f"\n{'='*100}")
            print(f"  【{TARGET_STRATEGY}】全局汇总 (BTCUSDT + XRPUSDT)")
            total_pnl_all = sum(t["realizedPnl"] for t in mtpcs_trades)
            total_commission_all = sum(t["commission"] for t in mtpcs_trades)
            print(f"  总交易笔数: {len(mtpcs_trades)}")
            print(f"  总手续费:   {total_commission_all:.4f} USDT")
            print(f"  总已实现盈亏: {total_pnl_all:+.4f} USDT")
            print(f"  含佣净收益: {total_pnl_all - total_commission_all:+.4f} USDT")
            print(f"{'='*100}")

        # === 输出未匹配的交易（可能有其他策略的）===
        if unmatched_trades:
            # 按 realizedPnl 排序
            unmatched_trades.sort(key=lambda t: t["realizedPnl"])

            print(f"\n{'='*100}")
            print(f"  【未匹配订单】交易明细 (可能来自其他策略或其他账户)")
            print(f"  共 {len(unmatched_trades)} 笔交易")
            print(f"{'='*100}")

            # 按交易对分组
            unmatched_by_symbol = defaultdict(list)
            for t in unmatched_trades:
                unmatched_by_symbol[t["symbol"]].append(t)

            for symbol in TARGET_SYMBOLS:
                sym_trades = unmatched_by_symbol.get(symbol, [])
                if not sym_trades:
                    continue

                print(f"\n  {symbol} ({len(sym_trades)} 笔):")
                print(f"  {'序号':>3} {'时间':<20} {'方向':<5} {'价格':<14} {'数量':<14} {'手续费':<10} {'已实现盈亏':<14}")
                print(f"  {'─'*3} {'─'*20} {'─'*5} {'─'*14} {'─'*14} {'─'*10} {'─'*14}")

                for i, t in enumerate(sym_trades, 1):
                    print(
                        f"  {i:>3} "
                        f"{format_time(t['time']):<20} "
                        f"{t['side']:<5} "
                        f"{t['price']:<14.8f} "
                        f"{t['qty']:<14.4f} "
                        f"{t['commission']:<10.4f} "
                        f"{t['realizedPnl']:<+14.4f}"
                    )

    finally:
        await binance_client.close()


if __name__ == "__main__":
    asyncio.run(main())