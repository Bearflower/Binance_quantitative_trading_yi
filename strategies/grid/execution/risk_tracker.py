"""Build complete ordinary USD-M evidence for the pure risk guard."""

from decimal import Decimal

from .risk import (RiskAssessment, RiskEvidence, RiskStatus, assess_risk,
                   beijing_day, beijing_day_start_ms)


class RiskTracker:
    def __init__(self, store, exchange, limits):
        self.store = store
        self.exchange = exchange
        self.limits = limits

    async def assess(self, now_ms: int) -> RiskAssessment:
        day = beijing_day(now_ms)
        start_ms = beijing_day_start_ms(now_ms)
        try:
            account = await self.exchange.account_info()
            equity = Decimal(str(account["totalMarginBalance"]))
            income = await self.exchange.income(start_ms, now_ms)
            if any((not isinstance(row, dict) or row.get("asset") != "USDT"
                    or not isinstance(row.get("time"), int)
                    or not start_ms <= row["time"] <= now_ms)
                   for row in income):
                raise ValueError("invalid income evidence")
            transfers = [row for row in income if row.get("incomeType") == "TRANSFER"]
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
            return result
        except Exception:
            return RiskAssessment(RiskStatus.UNKNOWN_LOCKED,
                                  ("equity_or_income_evidence_unavailable",))
