"""PostgreSQL persistence for the pure HRS ledger and gates.

Use ``async with repo.locked(account_id, segment_id) as session`` around the
load -> reconcile -> evaluate -> commit -> risk reservation operation. The yielded
``session.connection`` permits reservations in the SAME DB transaction. Holding
only a process-local lock or calling load/commit separately does not serialize an
order decision. Exchange requests remain outside this short DB transaction; the
caller must persist its reservation before releasing the lock.

One account has one active segment; deposits/reallocations must be CashFlow events,
not new segments. Segment switching is rejected so it cannot bypass old latches.
Apply the SQL migration through deployment tooling; this repository runs no DDL.
"""
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json

from .loss_gate import LossGateState, SHANGHAI, reset_rolling_latch
from .risk_ledger import (
    CashFlow, EquityCheckpoint, IncomeEvent, LedgerNotReady, LedgerState, aware, decimal_value,
)


class RepositoryConflict(RuntimeError):
    """Concurrent, conflicting or stale state; recompute before authorizing risk."""


@dataclass(frozen=True)
class StoredRiskState:
    version: int
    ledger: LedgerState
    gate: LossGateState


def _json_value(value):
    if isinstance(value, Decimal):
        decimal_value(value, "persisted_decimal")
        return str(value)
    if isinstance(value, datetime):
        return aware(value, "persisted").isoformat()
    raise TypeError(type(value).__name__)


def _encode(value):
    return json.dumps(value, default=_json_value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def ledger_to_dict(state: LedgerState) -> dict:
    return json.loads(_encode(asdict(state)))


def ledger_from_dict(data: dict) -> LedgerState:
    values = dict(data)
    for key in ("inception", "as_of"):
        values[key] = datetime.fromisoformat(values[key])
    for key in ("cash_allocation", "income_total", "units", "equity", "nav"):
        values[key] = Decimal(values[key])
    for key, event_type in (("incomes", IncomeEvent), ("cash_flows", CashFlow)):
        events = []
        for raw in values[key]:
            event = dict(raw)
            event["amount"] = Decimal(event["amount"])
            event["occurred_at"] = datetime.fromisoformat(event["occurred_at"])
            if key == "cash_flows" and event.get("nav_before") is not None:
                event["nav_before"] = Decimal(event["nav_before"])
            events.append(event_type(**event))
        values[key] = tuple(events)
    return LedgerState(**values)


def _stored(row):
    return StoredRiskState(row["version"], ledger_from_dict(_decode(row["ledger_state"])),
                           LossGateState.from_dict(_decode(row["gate_state"])))


class LossGateRepository:
    def __init__(self, db, *, lock_timeout_ms: int = 5000):
        if not isinstance(lock_timeout_ms, int) or lock_timeout_ms <= 0:
            raise ValueError("positive_lock_timeout_required")
        self.db = db
        self.lock_timeout_ms = lock_timeout_ms

    @asynccontextmanager
    async def locked(self, account_id: str, segment_id: str):
        if not account_id or not segment_id:
            raise ValueError("account_and_segment_required")
        if self.db.pool is None:
            await self.db.connect()
        # Account-wide, independent of segment so a new segment cannot bypass it.
        key = int.from_bytes(hashlib.sha256(f"hrs-loss-gate:{account_id}".encode()).digest()[:8], "big", signed=True)
        async with self.db.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT set_config('lock_timeout', $1, true)", f"{self.lock_timeout_ms}ms")
                # Same first lock as policy activation and final order submission.
                # Never acquire the policy lock after holding the loss lock.
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 7183))", account_id)
                await connection.execute("SELECT pg_advisory_xact_lock($1)", key)
                session = LossGateSession(connection, account_id, segment_id)
                await session.load()  # Reject a conflicting active segment immediately.
                yield session

    async def load(self, account_id: str, segment_id: str) -> StoredRiskState | None:
        async with self.locked(account_id, segment_id) as session:
            return await session.load()

    async def commit(self, account_id: str, ledger: LedgerState, gate: LossGateState,
                     *, expected_version: int | None, checkpoint_id: str) -> StoredRiskState:
        async with self.locked(account_id, ledger.segment_id) as session:
            return await session.commit(ledger, gate, expected_version=expected_version, checkpoint_id=checkpoint_id)

    async def reset_rolling(self, account_id: str, segment_id: str, *, expected_version: int,
                            reason: str, evidence: str, at: datetime) -> StoredRiskState:
        async with self.locked(account_id, segment_id) as session:
            return await session.reset_rolling(expected_version=expected_version, reason=reason, evidence=evidence, at=at)

    async def checkpoints(self, account_id: str, segment_id: str, *, since: datetime | None = None):
        async with self.locked(account_id, segment_id) as session:
            return await session.checkpoints(since=since)


class LossGateSession:
    def __init__(self, connection, account_id: str, segment_id: str):
        self.connection = connection
        self.account_id = account_id
        self.segment_id = segment_id

    async def load(self) -> StoredRiskState | None:
        row = await self.connection.fetchrow(
            "SELECT segment_id,version,ledger_state,gate_state FROM trading.hrs_loss_gate_state "
            "WHERE account_id=$1 FOR UPDATE", self.account_id)
        if row is None:
            return None
        if row["segment_id"] != self.segment_id:
            raise RepositoryConflict("segment_conflict")
        return _stored(row)

    async def checkpoints(self, *, since: datetime | None = None) -> tuple[EquityCheckpoint, ...]:
        if since is not None:
            aware(since, "since")
        rows = await self.connection.fetch(
            "SELECT as_of,equity,units,nav FROM trading.hrs_risk_equity_checkpoints "
            "WHERE account_id=$1 AND segment_id=$2 AND ($3::timestamptz IS NULL OR as_of >= $3) ORDER BY as_of",
            self.account_id, self.segment_id, since)
        return tuple(EquityCheckpoint(self.segment_id, row["as_of"], row["equity"], row["units"], row["nav"]) for row in rows)

    async def commit(self, ledger: LedgerState, gate: LossGateState, *, expected_version: int | None,
                     checkpoint_id: str) -> StoredRiskState:
        # A nested savepoint also protects callers that handle an error while
        # retaining the surrounding reservation transaction.
        async with self.connection.transaction():
            return await self._commit(ledger, gate, expected_version=expected_version, checkpoint_id=checkpoint_id)

    async def _commit(self, ledger: LedgerState, gate: LossGateState, *, expected_version: int | None,
                      checkpoint_id: str) -> StoredRiskState:
        if not checkpoint_id or ledger.segment_id != self.segment_id or gate.segment_id != self.segment_id:
            raise RepositoryConflict("checkpoint_segment_conflict")
        try:
            aware(ledger.as_of, "checkpoint")
            aware(ledger.inception, "inception")
            for field in ("equity", "units", "nav"):
                decimal_value(getattr(ledger, field), field, positive=True)
            if ledger.equity / ledger.units != ledger.nav or ledger.inception > ledger.as_of:
                raise LedgerNotReady("inconsistent_checkpoint")
            for events in (ledger.incomes, ledger.cash_flows):
                if len({event.event_id for event in events}) != len(events):
                    raise LedgerNotReady("duplicate_event_inventory")
            if sum((event.amount for event in ledger.incomes), Decimal("0")) != ledger.income_total:
                raise LedgerNotReady("inconsistent_income_total")
        except (LedgerNotReady, TypeError, AttributeError):
            raise RepositoryConflict("invalid_ledger_checkpoint") from None
        ledger_data = ledger_to_dict(ledger)
        gate_data = gate.to_dict()
        payload = {"ledger": ledger_data, "gate": gate_data}
        current = await self.load()
        prior = await self.connection.fetchrow(
            "SELECT payload FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND checkpoint_id=$2",
            self.account_id, checkpoint_id)
        if prior is not None:
            if _decode(prior["payload"]) != payload:
                raise RepositoryConflict("checkpoint_conflict")
            if current is None:
                raise RepositoryConflict("checkpoint_without_state")
            return current  # A stale retry must not overwrite a later reset/state.
        if (current.version if current else None) != expected_version:
            raise RepositoryConflict("version_conflict")
        if current:
            if ledger.as_of <= current.ledger.as_of:
                raise RepositoryConflict("checkpoint_time_conflict")
            for name in ("incomes", "cash_flows"):
                old_events = {event.event_id: event for event in getattr(current.ledger, name)}
                new_events = {event.event_id: event for event in getattr(ledger, name)}
                if any(new_events.get(event_id) != event for event_id, event in old_events.items()):
                    raise RepositoryConflict("event_history_conflict")
            if ledger.inception != current.ledger.inception:
                raise RepositoryConflict("inception_conflict")
            old_flow_ids = {event.event_id for event in current.ledger.cash_flows}
            flow_delta = sum((event.amount for event in ledger.cash_flows if event.event_id not in old_flow_ids), Decimal("0"))
            if ledger.cash_allocation != current.ledger.cash_allocation + flow_delta:
                raise RepositoryConflict("unrecorded_cashflow")
            if current.gate.daily_latched and not gate.daily_latched:
                if (gate.day <= current.gate.day or gate.day_origin_nav is None
                        or gate.day != ledger.as_of.astimezone(SHANGHAI).date()):
                    raise RepositoryConflict("daily_reset_requires_valid_new_day")
            if current.gate.rolling_latched and not gate.rolling_latched:
                raise RepositoryConflict("rolling_reset_requires_controlled_operation")
            if gate.revision < current.gate.revision:
                raise RepositoryConflict("gate_revision_conflict")
        for event_kind, events in (("income", ledger.incomes), ("cashflow", ledger.cash_flows)):
            persisted_events = () if current is None else (current.ledger.incomes if event_kind == "income" else current.ledger.cash_flows)
            persisted_ids = {event.event_id for event in persisted_events}
            for event in events:
                if event.event_id in persisted_ids:
                    continue  # The immutable history comparison above verified these.
                event_payload = json.loads(_encode(asdict(event)))
                existing = await self.connection.fetchrow(
                    "SELECT payload FROM trading.hrs_risk_ledger_events WHERE account_id=$1 AND event_kind=$2 AND event_id=$3",
                    self.account_id, event_kind, event.event_id)
                if existing is not None:
                    if _decode(existing["payload"]) != event_payload:
                        raise RepositoryConflict("event_history_conflict")
                    continue
                await self.connection.execute(
                    "INSERT INTO trading.hrs_risk_ledger_events (account_id,segment_id,event_kind,event_id,occurred_at,payload) "
                    "VALUES ($1,$2,$3,$4,$5,$6::jsonb)", self.account_id, self.segment_id, event_kind,
                    event.event_id, event.occurred_at, _encode(event_payload))
        same_time = await self.connection.fetchval(
            "SELECT checkpoint_id FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND segment_id=$2 AND as_of=$3",
            self.account_id, self.segment_id, ledger.as_of)
        if same_time is not None:
            raise RepositoryConflict("checkpoint_time_conflict")
        await self.connection.execute(
            "INSERT INTO trading.hrs_risk_equity_checkpoints "
            "(account_id,segment_id,checkpoint_id,as_of,equity,units,nav,payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
            self.account_id, self.segment_id, checkpoint_id, ledger.as_of, ledger.equity, ledger.units, ledger.nav, _encode(payload))
        version = 1 if current is None else current.version + 1
        if current is None:
            await self.connection.execute(
                "INSERT INTO trading.hrs_loss_gate_state (account_id,segment_id,version,ledger_state,gate_state) "
                "VALUES ($1,$2,$3,$4::jsonb,$5::jsonb)",
                self.account_id, self.segment_id, version, _encode(ledger_data), _encode(gate_data))
        else:
            result = await self.connection.execute(
                "UPDATE trading.hrs_loss_gate_state SET version=$3,ledger_state=$4::jsonb,gate_state=$5::jsonb,updated_at=NOW() "
                "WHERE account_id=$1 AND segment_id=$2 AND version=$6", self.account_id, self.segment_id,
                version, _encode(ledger_data), _encode(gate_data), expected_version)
            if result != "UPDATE 1":
                raise RepositoryConflict("version_conflict")
        return StoredRiskState(version, ledger, gate)

    async def reset_rolling(self, *, expected_version: int, reason: str, evidence: str, at: datetime) -> StoredRiskState:
        current = await self.load()
        if current is None or current.version != expected_version:
            raise RepositoryConflict("version_conflict")
        gate = reset_rolling_latch(current.gate, expected_revision=current.gate.revision,
                                  reason=reason, evidence=evidence, at=at)
        version = current.version + 1
        await self.connection.execute(
            "INSERT INTO trading.hrs_loss_gate_reset_audit "
            "(account_id,segment_id,version,reason,evidence,reset_at,before_state,after_state) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)", self.account_id, self.segment_id,
            version, gate.reset_reason, gate.reset_evidence, at, _encode(current.gate.to_dict()), _encode(gate.to_dict()))
        result = await self.connection.execute(
            "UPDATE trading.hrs_loss_gate_state SET version=$3,gate_state=$4::jsonb,updated_at=NOW() "
            "WHERE account_id=$1 AND segment_id=$2 AND version=$5", self.account_id, self.segment_id,
            version, _encode(gate.to_dict()), expected_version)
        if result != "UPDATE 1":
            raise RepositoryConflict("version_conflict")
        return StoredRiskState(version, current.ledger, gate)
