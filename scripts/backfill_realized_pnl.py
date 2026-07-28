"""
历史 realized_pnl 回填脚本

通过 Binance 收入 API 批量查询已实现盈亏，回填到 trade_records 表。

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
from typing import Optional, Dict, List, Tuple

sys.path.insert(0, "/app")


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
        self.base_url = "https://papi.binance.com"
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
        data = await self._request("GET", "/papi/v1/um/income", params)
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
    log("=" * 60)
    log("历史 realized_pnl 回填脚本启动")
    log("=" * 60)

    # 1. 获取 Binance API 凭据
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    if not api_key or not api_secret:
        log("❌ 未找到 Binance API 凭据")
        return

    # 2. 连接数据库
    log("📡 连接数据库...")
    try:
        conn = await get_db_connection()
        log("✅ 数据库连接成功")
    except Exception as e:
        log(f"❌ 数据库连接失败: {e}")
        return

    try:
        # 3. 重置所有 realized_pnl（重新匹配，确保准确性）
        log("🔄 重置所有 realized_pnl 为 NULL...")
        await conn.execute("""
            UPDATE trading.trade_records 
            SET realized_pnl = NULL 
            WHERE executed_at >= '2026-05-01'
        """)
        log("✅ 重置完成")
        
        # 4. 获取所有待回填记录（按策略和交易对分组）
        log("📊 查询所有待匹配记录...")
        rows = await conn.fetch("""
            SELECT id, strategy, symbol, order_id, side, order_type, 
                   executed_at, commission
            FROM trading.trade_records
            WHERE realized_pnl IS NULL
              AND executed_at >= '2026-05-01'
            ORDER BY strategy, symbol, executed_at
        """)
        records = [dict(r) for r in rows]
        log(f"📊 找到 {len(records)} 条待回填记录")

        if not records:
            log("✅ 没有需要回填的记录")
            return

        # 统计各策略的记录数
        from collections import Counter
        strategy_counts = Counter(r["strategy"] for r in records)
        for strategy, count in strategy_counts.most_common():
            log(f"   - {strategy}: {count} 条记录")

        # 获取所有涉及的交易对
        symbols = set(r["symbol"] for r in records)
        log(f"📈 涉及 {len(symbols)} 个交易对")

        # 5. 初始化 Binance 客户端
        log("🔑 初始化 Binance API 客户端...")
        client = BinanceBackfillClient(api_key, api_secret)

        # 6. 分片查询收入数据（每次查 7 天）
        log("📥 查询 Binance 收入历史（分片 7 天）...")
        
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        seven_days_ms = 7 * 24 * 60 * 60 * 1000
        
        all_income = []
        chunk_start = START_TIME_MS
        chunk_idx = 0
        
        while chunk_start < now_ms:
            chunk_end = min(chunk_start + seven_days_ms, now_ms)
            chunk_idx += 1
            
            try:
                income_records = await client.get_income_history(chunk_start, chunk_end)
                if income_records:
                    all_income.extend(income_records)
                    log(f"   第 {chunk_idx} 片: {len(income_records)} 条收入记录")
                else:
                    log(f"   第 {chunk_idx} 片: 0 条记录")
            except Exception as e:
                log(f"   第 {chunk_idx} 片查询失败: {e}")
            
            chunk_start = chunk_end

        log(f"📥 共获取 {len(all_income)} 条收入记录")

        # 7. 去重（分片查询边界可能存在重复记录）
        seen = set()
        deduped_income = []
        for inc in all_income:
            key = (inc.get("symbol", ""), inc.get("time", 0), inc.get("income", "0"))
            if key not in seen:
                seen.add(key)
                deduped_income.append(inc)
        all_income = deduped_income
        log(f"📥 去重后: {len(all_income)} 条收入记录")

        # 8. 按 symbol 和 time 建立索引
        from collections import defaultdict
        income_by_symbol = defaultdict(list)
        for inc in all_income:
            symbol = inc.get("symbol", "")
            if symbol:
                income_by_symbol[symbol].append(inc)

        # 9. 以收入记录为主循环，每条收入记录找到最匹配的 trade_record
        #    优先匹配出口单（STOP/TAKE_PROFIT），因为它们才是实际产生盈亏的订单
        #    入口单（LIMIT/MARKET）只有 PnL 时才会被匹配
        log("🧮 匹配收入记录到交易记录（以收入记录为主循环，优先匹配出口单）...")
        
        # 按 symbol 建立 trade_records 索引
        records_by_symbol = defaultdict(list)
        for rec in records:
            records_by_symbol[rec["symbol"]].append(rec)
        
        updated_count = 0
        total_pnl = Decimal("0")
        matched_symbols = set()
        unmatched_symbols = set()
        used_record_ids = set()  # 已匹配的记录 ID
        
        # 判断是否为出口单（STOP/TAKE_PROFIT 类型）
        def is_exit_order(order_type: str) -> bool:
            return order_type in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")
        
        # 将 trade_record 的时间转为 UTC 毫秒时间戳
        def get_record_ts(rec: dict) -> int:
            exec_time = rec["executed_at"]
            if isinstance(exec_time, datetime):
                beijing_tz = timezone(timedelta(hours=8))
                exec_utc = exec_time.replace(tzinfo=beijing_tz).astimezone(timezone.utc)
                return int(exec_utc.timestamp() * 1000)
            return 0
        
        # 对每条收入记录，从同 symbol 的未匹配记录中找到最佳匹配
        # 优先匹配出口单，且时间差最小的
        MATCH_WINDOW_MS = 7 * 24 * 60 * 60 * 1000  # 7天窗口
        EXIT_PRIORITY_BONUS = 1000  # 出口单的优先级加成（毫秒），让出口单优先于入口单
        
        for inc in all_income:
            symbol = inc.get("symbol", "")
            inc_time = inc.get("time", 0)
            income_val = inc.get("income", "0")
            
            if not symbol:
                continue
            
            # 获取该 symbol 未匹配的记录
            symbol_records = [r for r in records_by_symbol.get(symbol, [])
                              if r["id"] not in used_record_ids]
            if not symbol_records:
                continue
            
            # 找最佳匹配：出口单优先，时间差最小
            best_match = None
            best_score = MATCH_WINDOW_MS  # 越小越好，超过窗口则不匹配
            
            for rec in symbol_records:
                rec_ts = get_record_ts(rec)
                diff = abs(inc_time - rec_ts)
                
                if diff >= MATCH_WINDOW_MS:
                    continue
                
                # 出口单的 diff 减去优先级加成，使其更容易被选中
                adjusted_diff = diff
                if is_exit_order(rec["order_type"]):
                    adjusted_diff = max(0, diff - EXIT_PRIORITY_BONUS)
                
                if adjusted_diff < best_score:
                    best_score = adjusted_diff
                    best_match = rec
            
            if best_match:
                try:
                    pnl_decimal = Decimal(str(income_val))
                    await conn.execute(
                        "UPDATE trading.trade_records SET realized_pnl = $1 WHERE id = $2",
                        str(pnl_decimal), best_match["id"]
                    )
                    used_record_ids.add(best_match["id"])
                    updated_count += 1
                    total_pnl += pnl_decimal
                    matched_symbols.add(symbol)
                except Exception as e:
                    log(f"  ⚠️ 记录 {best_match['id']} 更新失败: {e}")
            # 未匹配的收入记录不统计 unmatched_symbols，因为可能没有对应 trade_record

        # 10. 输出结果
        log(f"✅ 更新完成:")
        log(f"   成功匹配: {updated_count} 条记录")
        log(f"   总盈亏: {total_pnl:.4f} USDT")
        log(f"   匹配到的交易对: {len(matched_symbols)} 个")
        log(f"   未匹配的交易对: {len(unmatched_symbols)} 个")

        # 11. 验证结果
        result = await conn.fetchrow("""
            SELECT 
                COUNT(*) as total,
                COUNT(realized_pnl) as with_pnl,
                COUNT(*) FILTER (WHERE realized_pnl IS NOT NULL AND realized_pnl != '0') as non_zero_pnl,
                SUM(COALESCE(realized_pnl::numeric, 0)) as total_pnl
            FROM trading.trade_records 
            WHERE executed_at >= '2026-05-01'
        """)
        log(f"📊 数据库验证:")
        log(f"   总记录数: {result['total']}")
        log(f"   有 realized_pnl 的记录: {result['with_pnl']}")
        log(f"   非零 realized_pnl 的记录: {result['non_zero_pnl']}")
        log(f"   总盈亏: {result['total_pnl']} USDT")

    except Exception as e:
        log(f"❌ 脚本执行失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await conn.close()
        log("👋 数据库连接已关闭")

    log("=" * 60)
    log("回填完成")
    log("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())