"""HRS 月度盈亏：按交易所 REALIZED_PNL 发生月归属，拒绝不完整或歧义结果。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

import structlog

from ai_tuner.reconciler.hrs_income_reconcile import (
    HRSReconcileConfig,
    match_income_to_trades,
)


logger = structlog.get_logger()
BEIJING = timezone(timedelta(hours=8))
MATCH_MARGIN = timedelta(days=7)


class HRSIncomeAttributionError(RuntimeError):
    """月度 HRS income 无法完整且唯一归属；必须停止分配。"""


def _beijing_aware(value: datetime) -> datetime:
    return value.astimezone(BEIJING) if value.tzinfo else value.replace(tzinfo=BEIJING)


class HRSMonthlyIncomeCollector:
    """用收入事件时间取代平仓日的累计 PNL_SUMMARY 口径。"""

    def __init__(self, db_manager, binance_client, *, page_limit: int = 1000):
        self.db = db_manager
        self.client = binance_client
        self.page_limit = page_limit

    async def collect(self, month_start: datetime, month_end: datetime) -> Decimal:
        if self.client is None:
            raise HRSIncomeAttributionError("HRS income 月度归属需要 Binance 客户端")
        start = _beijing_aware(month_start)
        end = _beijing_aware(month_end)
        if start >= end:
            raise ValueError("月度时间范围无效")

        income = await self._fetch_complete_income(start, end)
        window_start = (start - MATCH_MARGIN).replace(tzinfo=None)
        window_end = (end + MATCH_MARGIN).replace(tzinfo=None)
        rows = await self.db.fetch_all(
            "SELECT id, strategy, symbol, side, order_type, executed_at, realized_pnl "
            "FROM trading.trade_records WHERE executed_at >= $1 AND executed_at < $2",
            window_start, window_end,
        )
        records = [self._normalize_record(dict(row)) for row in rows]
        prior_rows = await self.db.fetch_all(
            "SELECT DISTINCT symbol FROM trading.trade_records "
            "WHERE strategy = 'hrs' AND executed_at < $1",
            window_start,
        )
        historical_symbols = {row["symbol"] for row in prior_rows}

        hrs_symbols = {r["symbol"] for r in records if r["strategy"] == "hrs"}
        other_symbols = {r["symbol"] for r in records if r["strategy"] != "hrs"}
        overlap = hrs_symbols & other_symbols
        if overlap:
            raise HRSIncomeAttributionError(
                f"HRS 币种与其他策略共享，无法唯一归属 income：{sorted(overlap)}"
            )

        # 历史 HRS 币种若在本窗口没有成交证据，仍可能是跨月持仓；不可默认为别的策略。
        uncertain = {
            r["symbol"] for r in income
            if r["symbol"] in historical_symbols
            and r["symbol"] not in hrs_symbols
        }
        if uncertain:
            raise HRSIncomeAttributionError(
                f"历史 HRS 币种缺少本月归属证据：{sorted(uncertain)}"
            )

        candidates = [r for r in income if r["symbol"] in hrs_symbols]
        if not candidates:
            # 空 API 响应不能覆盖已有 HRS 盈亏证据，尤其是历史补生成。
            monthly_hrs_pnl = [
                r for r in records
                if r["strategy"] == "hrs"
                and start.replace(tzinfo=None) <= r["executed_at"] < end.replace(tzinfo=None)
                and r["realized_pnl"] not in (None, Decimal("0"))
            ]
            if monthly_hrs_pnl:
                raise HRSIncomeAttributionError("HRS 有当月盈亏记录，但交易所 income 为空")
            return Decimal("0")
        matches, _ = match_income_to_trades(candidates, records, HRSReconcileConfig())
        unresolved_pairs = [
            (inc, match) for inc, match in zip(candidates, matches)
            if not match.matched or match.resolved_strategy != "hrs"
        ]
        if len(matches) != len(candidates):
            raise HRSIncomeAttributionError("HRS income 匹配结果数量不完整")
        approved = {}
        if unresolved_pairs:
            ids = [int(inc["tranId"]) for inc, _ in unresolved_pairs]
            account_id = os.environ.get("TRADE_ACCOUNT_ID", "default")
            overrides = await self.db.fetch_all(
                "SELECT tran_id, strategy, symbol, income, event_time_ms "
                "FROM trading.hrs_income_attribution "
                "WHERE account_id = $1 AND tran_id = ANY($2::bigint[])",
                account_id, ids,
            )
            approved = {int(row["tran_id"]): dict(row) for row in overrides}
        unresolved = []
        for inc, match in unresolved_pairs:
            override = approved.get(int(inc["tranId"]))
            if not override or (
                override["strategy"] != "hrs"
                or override["symbol"] != inc["symbol"]
                or Decimal(str(override["income"])) != Decimal(str(inc["income"]))
                or int(override["event_time_ms"]) != int(inc["time"])
            ):
                unresolved.append(match)
        if unresolved:
            sample = [
                (m.income_symbol, str(m.income_amount), m.income_time_ms, m.unresolved_reason)
                for m in unresolved[:5]
            ]
            raise HRSIncomeAttributionError(
                f"HRS income 未能归属：{len(unresolved)} / {len(candidates)} 笔，样本 {sample}"
            )
        total = sum((Decimal(str(inc["income"])) for inc in candidates), Decimal("0"))
        logger.info(
            "HRS 月度 income 完整归属", count=len(matches),
            override_count=len(unresolved_pairs), total=str(total),
        )
        return total

    async def _fetch_complete_income(self, start: datetime, end: datetime) -> list[dict]:
        """按北京自然日分窗，任何满页、异常或缺失事件键都阻止分配。"""
        found = {}
        cursor = start
        while cursor < end:
            next_cursor = min(cursor + timedelta(days=1), end)
            start_ms = int(cursor.timestamp() * 1000)
            end_ms = int(next_cursor.timestamp() * 1000)
            try:
                batch = await self.client.get_income_history(
                    start_time=start_ms, end_time=end_ms - 1,
                    income_type="REALIZED_PNL", limit=self.page_limit,
                )
            except Exception as exc:
                raise HRSIncomeAttributionError("HRS income 拉取失败") from exc
            if len(batch) >= self.page_limit:
                raise HRSIncomeAttributionError("HRS income 日窗口满页，无法证明已拉取完整")
            for item in batch:
                try:
                    if item["incomeType"] != "REALIZED_PNL":
                        raise HRSIncomeAttributionError("HRS income 响应包含非 REALIZED_PNL 事件")
                    tran_id = int(item["tranId"])
                    event_ms = int(item["time"])
                    symbol = item["symbol"]
                    amount = Decimal(str(item["income"]))
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    raise HRSIncomeAttributionError("HRS income 缺少可靠事件字段") from exc
                if start_ms <= event_ms < end_ms and symbol and amount != 0:
                    previous = found.get(tran_id)
                    if previous is not None and previous != item:
                        raise HRSIncomeAttributionError("同一 tranId 出现不一致的 income")
                    found[tran_id] = item
            cursor = next_cursor
        return list(found.values())

    @staticmethod
    def _normalize_record(row: dict) -> dict:
        row["id"] = str(row["id"])
        row["realized_pnl"] = (
            Decimal(str(row["realized_pnl"])) if row.get("realized_pnl") is not None else None
        )
        row["is_summary"] = row["order_type"] == "PNL_SUMMARY"
        return row
