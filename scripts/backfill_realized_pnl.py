"""
历史 realized_pnl 回填脚本

通过 Binance order/userTrades 核实完整成交，只回填 trade_records 的 NULL 盈亏。
必须显式设置 TRADE_ACCOUNT_ID；不覆盖既有数据、不推测历史未知订单归属。

使用方式（在容器内执行）：
    docker exec trading_system-btc_eth python /app/backfill_realized_pnl.py

或者直接通过 SSH 执行：
    ssh root@SERVER_IP "docker exec trading_system-btc_eth python /app/backfill_realized_pnl.py"
"""

import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────

# 需要回填的时间范围（从 2026-06-01 00:00:00 UTC+8 至今）
START_TIME_MS = 1748707200000  # 2026-06-01 00:00:00 UTC+8

# 批量查询时的并发数
CONCURRENCY = 5


# ──────────────────────────────────────────────
# 日志工具
# ──────────────────────────────────────────────

def log(msg: str, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}" + (f" ({extra})" if extra else ""))


# ──────────────────────────────────────────────
# Binance API 客户端
# ──────────────────────────────────────────────

class BinanceBackfillClient:
    """简化版 Binance API 客户端"""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        from shared.binance_account_mode import configured_pm
        self.use_unified_account = configured_pm()
        self.base_url = "https://papi.binance.com" if self.use_unified_account else "https://fapi.binance.com"
        self.api_prefix = "/papi/v1/um" if self.use_unified_account else "/fapi/v1"
        self.recv_window = 10000

    async def _request(self, method: str, path: str, params: dict = None) -> dict:
        """发起经过签名的 Binance API 请求"""
        import aiohttp
        import hmac
        import hashlib
        import time
        from urllib.parse import urlencode

        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window

        # urlencode 编码后计算签名，然后手动拼接 URL（避免二次编码）
        query_string = urlencode(sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise Exception(f"API 错误 {resp.status}: {data}")
                return data

    async def get_income_history(self, start_time: int, end_time: int) -> List[Dict]:
        """查询收入历史"""
        params = {
            "incomeType": "REALIZED_PNL",
            "startTime": start_time,
            "endTime": end_time,
            "limit": 1000
        }
        data = await self._request("GET", f"{self.api_prefix}/income", params)
        return data if isinstance(data, list) else []


# ──────────────────────────────────────────────
# 数据库操作
# ──────────────────────────────────────────────

async def get_db_connection():
    """获取数据库连接"""
    import asyncpg
    import re

    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        match = re.match(r"postgresql://(.+):(.+)@(.+):(\d+)/(.+)\?", db_url)
        if match:
            user, password, host, port, db = match.groups()
            return await asyncpg.connect(
                user=user, password=password, host=host,
                port=int(port), database=db.split("?")[0]
            )
        return await asyncpg.connect(db_url)

    return await asyncpg.connect(
        user=os.environ.get("DB_USER", "trading_user"),
        password=os.environ.get("DB_PASSWORD", "trading_password_2024"),
        host=os.environ.get("DB_HOST", "trading_system-postgres"),
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ.get("DB_NAME", "trading_platform"),
    )


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

async def main():
    """仅凭精确且完整的订单成交证据回填空值。

    保留已有盈亏；全部读取和校验完成后才在单个事务中写入。
    """
    from shared.entry_fill_evidence import verified_fill
    from shared.accounting.account_cutover import classic_cutover_ms, order_epoch

    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    account = os.environ.get("TRADE_ACCOUNT_ID", "").strip()
    if not api_key or not api_secret or not account:
        log("缺少 API 凭据或显式 TRADE_ACCOUNT_ID，未执行回填")
        return 1

    conn = None
    try:
        client = BinanceBackfillClient(api_key, api_secret)
        cutover = classic_cutover_ms()
        if not client.use_unified_account and cutover is None:
            raise ValueError("经典账户回填需要明确账户模式切点")
        mode_clause = ""
        query_args = [account, datetime(2026, 5, 1)]
        if cutover is not None:
            # trade_records.executed_at 是无时区的北京时间；切点毫秒属于 classic。
            boundary = datetime.fromtimestamp(cutover / 1000, timezone(timedelta(hours=8))).replace(tzinfo=None)
            mode_clause = " AND executed_at " + ("<" if client.use_unified_account else ">=") + " $3"
            query_args.append(boundary)
        conn = await get_db_connection()
        rows = await conn.fetch(
            "SELECT id, account_id, symbol, order_id, side, executed_at "
            "FROM trading.trade_records WHERE account_id=$1 "
            "AND realized_pnl IS NULL AND status='FILLED' "
            "AND order_id IS NOT NULL AND order_id<>'' "
            "AND executed_at >= $2" + mode_clause + " ORDER BY id",
            *query_args,
        )
        updates = []
        seen_orders = set()
        for row in rows:
            key = (row["symbol"], str(row["order_id"]))
            if row["account_id"] != account or key in seen_orders:
                raise ValueError("账户或订单绑定不唯一")
            if cutover is not None and order_epoch(row["executed_at"], cutover) != ("pm" if client.use_unified_account else "classic"):
                raise ValueError("记录不属于当前账户模式的时间区间")
            seen_orders.add(key)
            params = {"symbol": row["symbol"], "orderId": str(row["order_id"])}
            order = await client._request("GET", f"{client.api_prefix}/order", params.copy())
            trades = await client._request(
                "GET", f"{client.api_prefix}/userTrades", dict(params, limit=1000))
            verified_fill(row, order, trades)
            pnl = Decimal("0")
            for trade in trades:
                value = Decimal(str(trade["realizedPnl"]))
                if not value.is_finite():
                    raise ValueError("已实现盈亏不是有限数值")
                pnl += value
            updates.append((pnl, row["id"], account, row["symbol"],
                            str(row["order_id"]), row["side"]))

        if updates:
            async with conn.transaction():
                for values in updates:
                    await conn.execute(
                        "UPDATE trading.trade_records SET realized_pnl=$1, "
                        "pnl_source='exchange_order_matched' "
                        "WHERE id=$2 AND account_id=$3 AND symbol=$4 "
                        "AND order_id=$5 AND side=$6 AND status='FILLED' "
                        "AND realized_pnl IS NULL",
                        *values,
                    )
        log("回填完成，已有盈亏保持不变", verified_orders=len(updates))
        return 0
    except Exception as exc:
        # 读取或校验失败不写入；写入失败则整笔事务回滚。
        log("回填失败，未提交本轮更改", error_type=type(exc).__name__)
        return 1
    finally:
        if conn is not None:
            await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
