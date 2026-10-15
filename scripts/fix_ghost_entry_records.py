#!/usr/bin/env python3
"""
幽灵入场记录清理脚本（D4，方案 9.4）

背景：trade_records 中存在大量 status=NEW 的幽灵入场记录——限价单超时撤单后
未被标记终态，被 position_ownership 互斥 SQL 命中导致 SNPSUSDT 等真实持仓
币种被误拦（详见 docs/plans/fix-2026-10-14-ghost-entry-record-mutual-exclusion.md）。

本脚本逐单与交易所对账，按四分类处置：
  1. 交易所 CANCELED/EXPIRED/-2013 不存在 → 本地置 CANCELED
  2. 交易所 FILLED + 币种持仓为 0（已平仓）→ 本地置 CLOSED
  3. 交易所 FILLED + 币种持仓非 0 → 保留不动（真实持仓）
  4. 交易所仍为 NEW（遗留活单）→ 不标记不撤单，输出清单人工决策
  5. 查询异常 → 跳过并记录，宁保守不动

默认 dry-run（只输出清单，不写库），--execute 才落库。逐单 ≥0.2s 限速避免
触发币安限频。凭据从环境/.env 读取，缺失时明确报错退出。
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import asyncpg
from dotenv import load_dotenv

# 项目根路径（使 shared 包可导入）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.binance_api import BinanceClient, BinanceAPIError  # noqa: E402


# ------------------------------------------------------------
# SQL 常量（与 position_ownership / trade_logger 风格一致）
# ------------------------------------------------------------
# 候选集：order_type IN LIMIT/MARKET AND status=NEW AND realized_pnl IS NULL
# 注释：已平仓但未被 update_realized_pnl 回写 realized_pnl 的真实入场记录也会
#       命中此查询（结构性不变量问题）——四分类的第 2 类专门处理这类记录
_CANDIDATE_SQL = """
    SELECT id, symbol, strategy, order_id, order_type, status, realized_pnl,
           executed_at
    FROM trading.trade_records
    WHERE order_type IN ('LIMIT', 'MARKET')
      AND status = 'NEW'
      AND realized_pnl IS NULL
"""


def build_candidate_query(symbols=None, strategies=None, max_rows=None):
    """
    在候选集基础上追加可选的 symbol/strategy 过滤，返回 (sql, params)

    - symbols / strategies：可迭代的币种/策略名，命中即保留（ANY 语义）；为空则不限制；
    - 过滤值一律以 $n 占位参数化，禁止字符串拼接（既有安全口径，避免注入与范围失控）；
    - max_rows 非 None 时追加 LIMIT（同样参数化）。

    Returns:
        (sql 字符串, 参数列表)
    """
    filters = []
    params = []
    for column, values in (("symbol", symbols), ("strategy", strategies)):
        if values:
            params.append(list(values))
            filters.append(f"AND {column} = ANY(${len(params)})")
    parts = [_CANDIDATE_SQL.strip()]
    parts.extend(filters)
    parts.append("ORDER BY executed_at ASC, id ASC")
    if max_rows is not None:
        params.append(int(max_rows))
        parts.append(f"LIMIT ${len(params)}")
    return "\n    ".join(parts), params

# 置 CANCELED（交易所确认未成交）
_MARK_CANCELED_SQL = """
    UPDATE trading.trade_records SET status = 'CANCELED' WHERE id = $1
"""

# 置 CLOSED（交易所确认已平仓：FILLED + positionRisk 归零）
_MARK_CLOSED_SQL = """
    UPDATE trading.trade_records SET status = 'CLOSED' WHERE id = $1
"""


# ------------------------------------------------------------
# 四分类判定逻辑（纯函数，便于单元测试）
# ------------------------------------------------------------
def classify_entry(
    order_status: Optional[str],
    error_code: Optional[int],
    position_amt: Optional[float],
) -> str:
    """
    对单条 NEW 入场记录做四分类判定

    Args:
        order_status: 交易所 get_order 返回的 status（None 表示查询失败或 -2013 不存在）
        error_code: get_order 抛出的 BinanceAPIError.code（None 表示无错）
        position_amt: get_position 返回的该 symbol positionAmt（None 表示查询失败）

    Returns:
        分类标识：
          'CANCELED'   → 交易所确认未成交
                         （CANCELED/EXPIRED 状态 / -2011 订单已消失 / -2013 No such order）
                         → 本地置 CANCELED
          'CLOSED'     → 交易所 FILLED + 持仓归零 → 本地置 CLOSED
          'KEPT'       → 交易所 FILLED + 持仓非 0 → 保留不动
          'LIVE'       → 交易所仍为 NEW/PARTIALLY_FILLED 活单 → 不标记，输出清单
          'SKIP'       → 查询异常（get_order / positionRisk 任一失败）→ 跳过记录
    """
    # F4 加固：-2011（订单不存在/已消失）与 -2013 同归"订单不存在"类，
    # 与 cancel_order 的 -2011 处置语义一致（订单已消失 = 等同于被撤/过期）
    if error_code in (-2011, -2013) or order_status in ("CANCELED", "EXPIRED"):
        return "CANCELED"

    # 查询异常：有 error_code 且非 -2011/-2013 → SKIP
    if error_code is not None and error_code not in (-2011, -2013):
        return "SKIP"

    if order_status is None:
        # get_order 返回 None 且无错码（不该出现，保守跳过）
        return "SKIP"

    # 交易所仍为 NEW/PARTIALLY_FILLED → 活单
    if order_status in ("NEW", "PARTIALLY_FILLED"):
        return "LIVE"

    # FILLED 或 FILLED(PM)
    if order_status == "FILLED":
        if position_amt is None:
            # 持仓查询也失败 → 保守跳过
            return "SKIP"
        if abs(position_amt) < 1e-12:
            return "CLOSED"
        else:
            return "KEPT"

    # 其他未知状态（如 REJECTED 等）→ 跳过保守
    return "SKIP"


# ------------------------------------------------------------
# 逐单对账（异步主逻辑）
# ------------------------------------------------------------
async def audit_entry(
    client: BinanceClient,
    row: asyncpg.Record,
    *,
    min_interval: float = 0.2,
) -> Dict:
    """
    对单条 trade_records NEW 入场记录做交易所对账与四分类

    Args:
        client: BinanceClient 实例（需已启动 session）
        row: trade_records 候选行（含 id/symbol/order_id 等）
        min_interval: 两次 API 调用的最小间隔秒数（限速）

    Returns:
        dict: {"category": str, "row": row, "order_status": str|None,
               "position_amt": float|None, "error": str|None}
    """
    symbol = row["symbol"]
    order_id_str = row.get("order_id")

    # 限速
    await asyncio.sleep(min_interval)

    order_status = None
    error_code = None
    error_msg = None

    # ---------- 交易所订单查询 ----------
    if order_id_str:
        try:
            order_int = int(order_id_str)
        except (ValueError, TypeError):
            return {
                "category": "SKIP",
                "row": row,
                "order_status": None,
                "position_amt": None,
                "error": f"order_id 非整数: {order_id_str}",
            }
        try:
            order = await client.get_order(symbol, order_int)
            order_status = order.get("status") if order else None
        except BinanceAPIError as e:
            error_code = e.code
            error_msg = str(e)
        except Exception as e:
            error_msg = f"get_order 异常: {e}"
    else:
        # order_id 为 NULL 的记录无法向交易所查询 → 跳过
        return {
            "category": "SKIP",
            "row": row,
            "order_status": None,
            "position_amt": None,
            "error": "order_id 为空，无法向交易所查询",
        }

    # ---------- 交易所持仓查询 ----------
    # 仅当订单是 FILLED 状态时，持仓查询才有分类价值
    position_amt = None
    if order_status == "FILLED":
        await asyncio.sleep(min_interval)
        try:
            positions = await client.get_position(symbol)
            # 查询成功即视为可判定：无该币种持仓行 => 持仓归零（而非查询失败）
            # 否则「查到但无持仓」被误判为 SKIP，FILLED+已平仓的历史记录永不收敛
            position_amt = 0.0
            for pos in positions or []:
                if pos.get("symbol") == symbol.upper():
                    pos_amt_str = pos.get("positionAmt", "0")
                    try:
                        position_amt = float(pos_amt_str)
                    except (ValueError, TypeError):
                        position_amt = None
                    break
        except Exception as e:
            position_amt = None  # 查询异常 → 保守 SKIP
            error_msg = (error_msg + f"; get_position 异常: {e}") if error_msg else f"get_position 异常: {e}"

    # ---------- 分类 ----------
    category = classify_entry(order_status, error_code, position_amt)

    return {
        "category": category,
        "row": row,
        "order_status": order_status,
        "position_amt": position_amt,
        "error": error_msg,
    }


async def run_audit(
    db_conn: asyncpg.Connection,
    client: BinanceClient,
    *,
    execute: bool = False,
    max_rows: Optional[int] = None,
    min_interval: float = 0.2,
    symbols: Optional[List[str]] = None,
    strategies: Optional[List[str]] = None,
) -> Dict[str, List[Dict]]:
    """
    执行全量对账循环：候选集 → 逐单对账 → 汇总分类

    Args:
        db_conn: asyncpg 数据库连接
        client: BinanceClient 实例
        execute: True 时落库；False 时只输出（dry-run）
        max_rows: 候选集上限（None 表示全部）
        min_interval: 两次 API 调用间隔秒数
        symbols: 仅处理指定币种（None 表示不限制）
        strategies: 仅处理指定策略（None 表示不限制）

    Returns:
        dict: {"CANCELED": [...], "CLOSED": [...], "KEPT": [...],
               "LIVE": [...], "SKIP": [...]}
    """
    print(f"\n{'=' * 70}")
    print(f"幽灵入场记录清理脚本（D4） — {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"{'=' * 70}")

    # 1. 拉取候选集（symbol/strategy 过滤一律参数化）
    query, params = build_candidate_query(symbols, strategies, max_rows)
    rows = await db_conn.fetch(query, *params)
    scope = []
    if symbols:
        scope.append(f"symbol IN {list(symbols)}")
    if strategies:
        scope.append(f"strategy IN {list(strategies)}")
    scope_text = ("，范围：" + "；".join(scope)) if scope else ""
    print(f"\n候选集：共 {len(rows)} 条 NEW 入场记录"
          f"（order_type IN LIMIT/MARKET, realized_pnl IS NULL）{scope_text}")

    if not rows:
        print("无候选记录，脚本退出。")
        return {}

    # 2. 逐单对账
    # 用 asyncio.Semaphore 控制并发（避免瞬时请求打爆限频）
    semaphore = asyncio.Semaphore(3)

    async def _one(row):
        async with semaphore:
            return await audit_entry(client, row, min_interval=min_interval)

    results = []
    for i, row in enumerate(rows):
        result = await _one(row)
        results.append(result)
        # 进度反馈（每 50 条输出一次）
        if (i + 1) % 50 == 0 or (i + 1) == len(rows):
            print(f"  已处理 {i + 1}/{len(rows)} ...")

    # 3. 按分类汇总
    buckets: Dict[str, List[Dict]] = {
        "CANCELED": [],
        "CLOSED": [],
        "KEPT": [],
        "LIVE": [],
        "SKIP": [],
    }
    for r in results:
        buckets[r["category"]].append(r)

    # 4. dry-run 输出清单
    print(f"\n{'=' * 70}")
    print("分类汇总")
    print(f"{'=' * 70}")
    for cat in ("CANCELED", "CLOSED", "KEPT", "LIVE", "SKIP"):
        items = buckets[cat]
        print(f"\n[{cat}] {len(items)} 条")
        for it in items[:10]:  # 每类只打印前 10 条
            row = it["row"]
            extra = ""
            if cat == "CANCELED":
                extra = f" (order_status={it['order_status']})"
            elif cat == "CLOSED":
                extra = f" (positionAmt={it['position_amt']})"
            elif cat == "KEPT":
                extra = f" (positionAmt={it['position_amt']})"
            elif cat == "SKIP":
                extra = f" error={it['error']}"
            print(f"  id={row['id']} symbol={row['symbol']:10} "
                  f"strategy={row['strategy']:15} order_id={row['order_id']}{extra}")
        if len(items) > 10:
            print(f"  ... 省略剩余 {len(items) - 10} 条")

    if not execute:
        print(f"\n[DRY-RUN] 未落库。加 --execute 以上述分类结果写库。")
        return buckets

    # 5. 落库
    print(f"\n{'=' * 70}")
    print("落库执行")
    print(f"{'=' * 70}")

    tx_canceled = 0
    tx_closed = 0

    # CANCELED + CLOSED 在一个事务内执行（幂等，单条失败不影响其他）
    for cat, sql in (("CANCELED", _MARK_CANCELED_SQL), ("CLOSED", _MARK_CLOSED_SQL)):
        items = buckets[cat]
        print(f"\n[{cat}] 执行 UPDATE ...")
        for it in items:
            row = it["row"]
            try:
                await db_conn.execute(sql, row["id"])
                if cat == "CANCELED":
                    tx_canceled += 1
                else:
                    tx_closed += 1
            except Exception as e:
                print(f"  id={row['id']} 落库失败: {e}")

    print(f"\n落库完成：CANCELED={tx_canceled}，CLOSED={tx_closed}")
    print(f"KEPT={len(buckets['KEPT'])} 条保留不变；"
          f"LIVE={len(buckets['LIVE'])} 条活单需人工决策；"
          f"SKIP={len(buckets['SKIP'])} 条跳过。")
    return buckets


# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> int:
    """异步主入口：初始化连接 → 对账 → 输出/落库 → 清理。"""

    # 1. 凭据检查
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DB_URL") or os.getenv("POSTGRES_URL")

    missing = []
    if not api_key:
        missing.append("BINANCE_API_KEY")
    if not api_secret:
        missing.append("BINANCE_API_SECRET")
    if not db_url:
        missing.append("DATABASE_URL / DB_URL / POSTGRES_URL")

    if missing:
        print(f"[BLOCKED] 以下环境变量缺失：{', '.join(missing)}", file=sys.stderr)
        print("请在 .env 或 shell 环境中配置后重试。", file=sys.stderr)
        return 2

    # 2. 初始化 BinanceClient（使用 PM 账户 = 默认 use_unified_account=True）
    client = BinanceClient(
        api_key=api_key,
        api_secret=api_secret,
        use_unified_account=not args.regular_account,
    )

    # 3. 初始化 DB 连接
    db_conn = await asyncpg.connect(db_url)

    try:
        await run_audit(
            db_conn,
            client,
            execute=args.execute,
            max_rows=args.max_rows,
            min_interval=args.interval,
            symbols=args.symbol,
            strategies=args.strategy,
        )
    finally:
        await db_conn.close()
        if client.session:
            await client.session.close()

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="幽灵入场记录清理脚本：逐单交易所对账四分类处置"
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="落库执行（默认 dry-run 只输出清单）",
    )
    p.add_argument(
        "--regular-account",
        action="store_true",
        help="使用普通合约账户（默认 PM 统一账户）",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="候选集上限（默认全部）",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="两次 API 调用最小间隔秒数（默认 0.2）",
    )
    p.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="仅处理指定币种（可重复：--symbol BNBUSDT --symbol SOLUSDT）",
    )
    p.add_argument(
        "--strategy",
        action="append",
        default=None,
        help="仅处理指定策略（可重复：--strategy btc_eth_aggressive）",
    )
    return p.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
