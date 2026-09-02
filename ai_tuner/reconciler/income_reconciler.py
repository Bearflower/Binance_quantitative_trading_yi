"""
统一 PnL 校准器（IncomeReconciler）

背景
----
数据库 trading.trade_records 的 realized_pnl 字段，因部分平仓链路（条件单、
异常平仓）未被策略实时回写，导致字段缺失/不全，进而使 AI 调优系统
（PnLCollector 及各策略 adapter）基于不完整的盈亏数据给出不可信建议。

本模块采用"双保险"方案中的定时校准环节：
保留策略平仓时实时回写 realized_pnl；本校准器周期性地从 Binance income API
拉取 REALIZED_PNL 权威数据，兜底补齐 trade_records 中缺失/错误的盈亏。

设计要点
--------
1. 增量校准：借助 public.pnl_reconcile_state 表记录上次校准进度
   （last_reconciled_ts，UTC 毫秒），下次运行只处理该时间点之后的新数据，
   避免重复拉取 Binance 收入。
2. 匹配算法复用 scripts/backfill_realized_pnl.py 的思路：以 Binance 收入记录
   为主循环，对同 symbol 下尚未匹配的 trade_records，优先匹配出口单
   （STOP/TAKE_PROFIT），并选择时间差最小者。该脚本为一次性离线脚本且
   不允许修改，故此处以类方法形式重新实现等价的增量算法并用中文注释说明。
3. 时区约定：Binance income 的 time 字段为 UTC 毫秒时间戳；
   trade_records.executed_at 为北京时间（无时区）。匹配/过滤时统一换算。
4. 优雅降级：任何 Binance API 调用或单条写入失败只记日志，不中断主流程；
   校准失败不影响 ai_tuner 其他任务运行。
5. 所有可调参数（回看天数、分片大小、批处理大小、匹配窗口等）均从
   ai_tuner/config.yaml 的 reconciler: 段读取，代码内仅提供默认值兜底。
"""

import structlog
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple

logger = structlog.get_logger()

# 一天的毫秒数（默认值兜底计算用，实际取值仍以 config 为准）
_DAY_MS = 24 * 60 * 60 * 1000
# 北京时区（UTC+8）
_CST = timezone(timedelta(hours=8))

# 与手工回填脚本一致：只有这些订单类型会真实产生已实现盈亏，匹配时优先
_EXIT_ORDER_TYPES = (
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
)


class IncomeReconciler:
    """统一 PnL 校准器：从 Binance 拉取权威盈亏，补齐 trade_records 缺失的 realized_pnl"""

    def __init__(self, db_manager, binance_client, config: Dict):
        """
        初始化 PnL 校准器

        Args:
            db_manager: 数据库管理器实例（DatabaseManager）
            binance_client: Binance API 客户端（BinanceClient）
            config: ai_tuner 系统配置字典，从中读取 reconciler 段
        """
        self.db = db_manager
        self.binance = binance_client
        self.cfg = config.get("reconciler", {})

        # 从配置读取所有可调参数，均提供默认值兜底（禁止硬编码）
        self.enabled = self.cfg.get("enabled", True)
        self.lookback_days = int(self.cfg.get("lookback_days", 7))
        self.chunk_days = int(self.cfg.get("chunk_days", 7))
        self.batch_size = int(self.cfg.get("batch_size", 1000))
        self.match_window_ms = int(self.cfg.get("match_window_ms", 7 * _DAY_MS))
        self.exit_priority_bonus_ms = int(self.cfg.get("exit_priority_bonus_ms", 1000))
        self.schema = self.cfg.get("schema", "trading")
        self.table = self.cfg.get("table", "trade_records")
        self.state_schema = self.cfg.get("state_schema", "public")
        self.state_table = self.cfg.get("state_table", "pnl_reconcile_state")
        self.excluded_strategies = list(self.cfg.get("excluded_strategies", []))

    async def run_once(self) -> None:
        """执行一次校准任务：拉取 Binance 权威盈亏，补齐缺失的 realized_pnl"""
        if not self.enabled:
            logger.info("PnL 校准器已禁用，跳过本次校准")
            return
        try:
            await self._ensure_state_table()
            last_ts = await self._get_last_reconciled_ts()
            now_ms = _utc_now_ms()
            # 从上次进度继续，若无进度则从"最近 N 天"的偏移处开始（相对当前时间，非硬编码日期）
            start_ms = max(last_ts, now_ms - self.lookback_days * _DAY_MS)
            end_ms = now_ms
            logger.info("开始 PnL 校准", start_ms=start_ms, end_ms=end_ms, last_ts=last_ts)

            income = await self._fetch_income_records(start_ms, end_ms)
            if not income:
                logger.info("校准窗口内无 Binance 收入记录")
                await self._update_reconciled_ts(end_ms)
                return

            pending = await self._fetch_pending_records(start_ms, end_ms)
            if not pending:
                logger.info("无可校准的缺失盈亏记录", income_count=len(income))
                await self._update_reconciled_ts(end_ms)
                return

            updated, total_pnl = await self._match_and_update(income, pending)
            await self._update_reconciled_ts(end_ms)
            logger.info(
                "PnL 校准完成",
                income_count=len(income),
                pending_count=len(pending),
                updated=updated,
                total_pnl=str(total_pnl),
            )
        except Exception as e:
            logger.error("PnL 校准任务失败", error=str(e), exc_info=True)

    # ──────────────────────────────────────────────
    # 校准进度管理
    # ──────────────────────────────────────────────

    async def _ensure_state_table(self) -> None:
        """确保校准进度表存在（用 execute_ddl 绕过 ALTER/CREATE 拦截）"""
        ddl = f"""
            CREATE TABLE IF NOT EXISTS {self.state_schema}.{self.state_table} (
                id INT PRIMARY KEY,
                last_reconciled_ts BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """
        seed = f"""
            INSERT INTO {self.state_schema}.{self.state_table} (id, last_reconciled_ts)
            VALUES (1, 0)
            ON CONFLICT (id) DO NOTHING
        """
        await self.db.execute_ddl(ddl)
        await self.db.execute(seed)

    async def _get_last_reconciled_ts(self) -> int:
        """读取上次校准进度（毫秒时间戳），无记录返回 0"""
        row = await self.db.fetch_one(
            f"SELECT last_reconciled_ts FROM {self.state_schema}.{self.state_table} WHERE id = 1"
        )
        return int(row["last_reconciled_ts"] or 0) if row else 0

    async def _update_reconciled_ts(self, ts_ms: int) -> None:
        """更新校准进度，下次只处理该时间点之后的新数据，避免重复"""
        sql = f"""
            INSERT INTO {self.state_schema}.{self.state_table} (id, last_reconciled_ts, updated_at)
            VALUES (1, $1, NOW())
            ON CONFLICT (id) DO UPDATE
                SET last_reconciled_ts = EXCLUDED.last_reconciled_ts,
                    updated_at = NOW()
        """
        await self.db.execute(sql, ts_ms)

    # ──────────────────────────────────────────────
    # Binance 收入拉取
    # ──────────────────────────────────────────────

    async def _fetch_income_records(self, start_ms: int, end_ms: int) -> list:
        """分片拉取 REALIZED_PNL 收入记录，最后去重返回"""
        collected: list = []
        chunk_ms = self.chunk_days * _DAY_MS
        chunk_start = start_ms
        while chunk_start < end_ms:
            chunk_end = min(chunk_start + chunk_ms, end_ms)
            await self._fetch_income_chunk(chunk_start, chunk_end, collected)
            chunk_start = chunk_end
        return self._dedupe_income(collected)

    async def _fetch_income_chunk(self, chunk_start: int, chunk_end: int, collected: list) -> None:
        """拉取单个时间分片内的收入记录并追加，含分页游标推进"""
        cursor = chunk_start
        while cursor < chunk_end:
            try:
                batch = await self.binance.get_income_history(
                    start_time=cursor,
                    end_time=chunk_end,
                    income_type="REALIZED_PNL",
                    limit=self.batch_size,
                )
            except Exception as e:
                logger.warning("Binance 收入查询失败", start_ms=cursor, error=str(e))
                break
            if not batch:
                break
            collected.extend(batch)
            if len(batch) < self.batch_size:
                break
            # 已取满一页：用最后一条记录的 time 前移游标继续下一页
            last_time = max(int(r.get("time") or 0) for r in batch)
            if last_time <= cursor:
                break
            cursor = last_time + 1

    @staticmethod
    def _dedupe_income(records: list) -> list:
        """对分片边界可能重复的收入记录去重（symbol + time + income 唯一）"""
        seen = set()
        result = []
        for rec in records:
            key = (rec.get("symbol", ""), rec.get("time", 0), rec.get("income", "0"))
            if key not in seen:
                seen.add(key)
                result.append(rec)
        return result

    # ──────────────────────────────────────────────
    # 待校准记录查询与匹配
    # ──────────────────────────────────────────────

    async def _fetch_pending_records(self, start_ms: int, end_ms: int) -> list:
        """
        查询待校准的 trade_records（realized_pnl 为空、时间贴近校准窗口）

        executed_at 为北京时间，故将 UTC 毫秒窗口换算为北京时间后再过滤。
        """
        bj_start = _to_beijing_naive(start_ms - self.match_window_ms)
        bj_end = _to_beijing_naive(end_ms + self.match_window_ms)
        sql = f"""
            SELECT id, symbol, order_type, executed_at
            FROM {self.schema}.{self.table}
            WHERE realized_pnl IS NULL
              AND executed_at >= $1
              AND executed_at <= $2
        """
        args: list = [bj_start, bj_end]
        if self.excluded_strategies:
            # 排除 grid 等数据存在独立表的策略，避免误匹配
            placeholders = ", ".join(
                f"${i}" for i in range(3, 3 + len(self.excluded_strategies))
            )
            sql += f" AND strategy NOT IN ({placeholders})"
            args.extend(self.excluded_strategies)
        return await self.db.fetch_all(sql, *args)

    async def _match_and_update(self, income_records: list, pending_records: list) -> Tuple[int, Decimal]:
        """以收入记录为主循环，匹配并回写 realized_pnl，返回（更新条数，累计盈亏）"""
        records_by_symbol = defaultdict(list)
        for rec in pending_records:
            records_by_symbol[rec["symbol"]].append(rec)

        used_ids = set()
        updated = 0
        total_pnl = Decimal("0")

        for inc in income_records:
            symbol = inc.get("symbol", "")
            inc_time = int(inc.get("time") or 0)
            if not symbol or inc_time <= 0:
                continue
            candidates = [
                r for r in records_by_symbol.get(symbol, []) if r["id"] not in used_ids
            ]
            match = self._find_best_match(candidates, inc_time)
            if match is None:
                continue
            try:
                pnl = Decimal(str(inc.get("income", "0")))
                await self.db.execute(
                    f"UPDATE {self.schema}.{self.table} SET realized_pnl = $1 WHERE id = $2",
                    pnl,
                    match["id"],
                )
                used_ids.add(match["id"])
                updated += 1
                total_pnl += pnl
            except Exception as e:
                logger.warning("回写 realized_pnl 失败", record_id=match["id"], error=str(e))
        return updated, total_pnl

    def _find_best_match(self, candidates: list, inc_time: int) -> Optional[dict]:
        """
        在候选记录中找最佳匹配：优先出口单、时间差最小（须在窗口内）

        与 backfill_realized_pnl.py 的匹配规则一致：
        对每条收入记录，从同 symbol 未匹配记录中选时间差最小者，
        出口单因真实产生盈亏而获得优先级加成。
        """
        best = None
        best_score = None
        for rec in candidates:
            diff = abs(inc_time - _record_utc_ms(rec["executed_at"]))
            if diff >= self.match_window_ms:
                continue
            if self._is_exit_order(rec["order_type"]):
                diff = max(0, diff - self.exit_priority_bonus_ms)
            if best is None or diff < best_score:
                best = rec
                best_score = diff
        return best

    @staticmethod
    def _is_exit_order(order_type: str) -> bool:
        """判断订单类型是否为出口单（真实产生盈亏的平仓单）"""
        return (order_type or "").upper() in _EXIT_ORDER_TYPES


# ──────────────────────────────────────────────
# 时间换算工具
# ──────────────────────────────────────────────

def _utc_now_ms() -> int:
    """当前 UTC 时间对应的毫秒时间戳"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _record_utc_ms(exec_time) -> int:
    """将 trade_records.executed_at（北京时间无时区）转换为 UTC 毫秒时间戳"""
    if not isinstance(exec_time, datetime):
        return 0
    exec_utc = exec_time.replace(tzinfo=_CST).astimezone(timezone.utc)
    return int(exec_utc.timestamp() * 1000)


def _to_beijing_naive(ms: int) -> datetime:
    """将 UTC 毫秒时间戳转换为北京时间（无时区），用于 executed_at 时间过滤"""
    utc_dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return utc_dt.astimezone(_CST).replace(tzinfo=None)


if __name__ == "__main__":
    import asyncio

    async def smoke_test() -> None:
        """冒烟测试：验证模块可正常导入并实例化（不连接外部资源）"""
        obj = IncomeReconciler(db_manager=None, binance_client=None, config={"reconciler": {}})
        logger.info("IncomeReconciler 冒烟测试通过", enabled=obj.enabled)

    asyncio.run(smoke_test())