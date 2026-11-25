"""Build complete PM account evidence for the pure risk guard."""

import json
from decimal import Decimal

from .risk import (RiskAssessment, RiskEvidence, RiskStatus, assess_risk,
                   beijing_day, beijing_day_start_ms)


class RiskTracker:
    def __init__(self, store, exchange, limits):
        self.store = store
        self.exchange = exchange
        self.limits = limits
        self._historical_peak = None

    async def assess(self, now_ms: int) -> RiskAssessment:
        day = beijing_day(now_ms)
        start_ms = beijing_day_start_ms(now_ms)
        try:
            account = await self.exchange.account_info()
            equity = Decimal(str(account["accountEquity"]))
            income = await self.exchange.income(start_ms, now_ms)
            if any((not isinstance(row, dict) or row.get("asset") != "USDT"
                    or not isinstance(row.get("time"), int)
                    or not start_ms <= row["time"] <= now_ms)
                   for row in income):
                raise ValueError("invalid income evidence")
            transfers = [row for row in income if row.get("incomeType") == "TRANSFER"]
            if getattr(self.exchange, "pm", True) is False:
                # 只有明确的交易盈亏或成本可计入回撤。其它资金变动需人工核对。
                for row in income:
                    amount = Decimal(str(row["income"]))
                    if not amount.is_finite() or row.get("incomeType") not in (
                            "TRANSFER", "REALIZED_PNL", "FUNDING_FEE", "COMMISSION"):
                        raise ValueError("unverified classic income adjustment")
            else:
                # Operator-reviewed internal settlements are scoped to this account journal.
                if transfers and self.store.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewed_settlement'"
                ).fetchone():
                    def reviewed(row):
                        return self.store.conn.execute(
                            "SELECT 1 FROM reviewed_settlement WHERE tran_id=? AND event_ms=? "
                            "AND asset=? AND amount=?",
                            (str(row.get("tranId")), row["time"], row["asset"], str(row.get("income")))
                        ).fetchone() is not None
                    transfers = [row for row in transfers if not reviewed(row)]
                unverified = []
                for row in transfers:
                    confirmation = (await self.exchange.confirm_internal_settlement(row)
                                    if hasattr(self.exchange, "confirm_internal_settlement") else None)
                    if not isinstance(confirmation, dict):
                        unverified.append(row)
                        continue
                    self.store.conn.execute("""CREATE TABLE IF NOT EXISTS reviewed_settlement(
                        tran_id TEXT PRIMARY KEY,event_ms INTEGER,asset TEXT,amount TEXT,
                        reason TEXT,reviewed_ms INTEGER)""")
                    self.store.conn.execute("INSERT OR IGNORE INTO reviewed_settlement VALUES(?,?,?,?,?,?)",
                        (str(row["tranId"]), row["time"], row["asset"], str(row["income"]),
                         json.dumps({"source":"confirmed_same_account_margin_to_futures",
                                     "counterparty":confirmation},sort_keys=True), now_ms))
                transfers = unverified
            checkpoint = self.store.latest_equity_checkpoint(day)
            if checkpoint is None:
                positions = await self.exchange.position()
                flat = (len(positions) == 1 and
                        Decimal(str(positions[0]["positionAmt"])) == 0)
                # Midday bootstrap is permitted only for an unused, flat
                # subaccount. Around midnight a running executor can roll its
                # baseline from an immediate equity snapshot.
                verified = (not transfers and
                            (now_ms - start_ms <= 60_000 or (flat and not income)))
                baseline = equity if verified else None
                peak = equity if verified else None
            else:
                verified = bool(checkpoint["baseline_verified"])
                baseline = Decimal(checkpoint["baseline_equity"])
                peak = Decimal(checkpoint["peak_equity"])
            # Historical verified peaks survive Beijing-day rollover and restarts.
            if self._historical_peak is None:
                historical_peak = self.store.conn.execute(
                    "SELECT peak_equity FROM equity_checkpoint WHERE baseline_verified=1 "
                    "ORDER BY CAST(peak_equity AS NUMERIC) DESC LIMIT 1"
                ).fetchone()
                if historical_peak is not None:
                    self._historical_peak = Decimal(historical_peak[0])
            if peak is not None and self._historical_peak is not None:
                peak = max(peak, self._historical_peak)
            evidence = RiskEvidence(
                observed_at_ms=now_ms, baseline_day=day,
                baseline_equity_usdt=baseline, peak_equity_usdt=peak,
                equity_usdt=equity, baseline_verified=verified,
                income_complete=True, transfer_count=len(transfers),
                income_window_start_ms=start_ms, income_window_end_ms=now_ms,
            )
            result = assess_risk(evidence, self.limits)
            if result.status != RiskStatus.UNKNOWN_LOCKED:
                self.store.record_equity_checkpoint(
                    day, baseline, equity, result.next_peak_equity_usdt,
                    now_ms, verified=True, transfer_fingerprint="none")
                self._historical_peak = result.next_peak_equity_usdt
            return result
        except Exception:
            return RiskAssessment(RiskStatus.UNKNOWN_LOCKED,
                                  ("equity_or_income_evidence_unavailable",))
