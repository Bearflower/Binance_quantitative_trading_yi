"""Fsynced local STOP intent journal on the strategy's persistent data volume.

An intent is written before the exchange POST. A resolved line is written only
after the matching exchange algo and database binding are confirmed. An
unresolved intent always blocks a fresh entry, including after process restart.
"""

import fcntl
import json
import os
from pathlib import Path
from decimal import Decimal, InvalidOperation
from uuid import uuid4


class StopJournalError(ValueError):
    """Journal cannot be trusted or durably written."""


class StopIntentJournal:
    def __init__(self, path):
        self.path = Path(path)

    def _append(self, event):
        data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        created = not self.path.exists()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise StopJournalError("STOP journal write incomplete")
                data = data[written:]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        if created:
            parent_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def probe(self):
        """Prove the persistent file can be written and synced before entry."""
        self._append({"kind": "probe", "id": uuid4().hex})
        self.pending()

    def prepare(self, *, account_id, strategy, symbol, episode_entry_order_id,
                stop_side, stop_quantity):
        try:
            quantity = Decimal(str(stop_quantity))
        except (InvalidOperation, TypeError, ValueError):
            quantity = None
        if (not account_id or not strategy or not symbol
                or (episode_entry_order_id is not None and (
                    not isinstance(episode_entry_order_id, int)
                    or isinstance(episode_entry_order_id, bool)
                    or episode_entry_order_id <= 0))
                or stop_side not in ("BUY", "SELL")
                or quantity is None or not quantity.is_finite() or quantity <= 0):
            raise StopJournalError("STOP intent identity invalid")
        client_algo_id = "sa" + uuid4().hex[:30]
        self._append({
            "kind": "intent", "client_algo_id": client_algo_id,
            "account_id": account_id, "strategy": strategy, "symbol": symbol,
            "episode_entry_order_id": episode_entry_order_id,
            "stop_side": stop_side, "stop_quantity": str(stop_quantity),
        })
        return client_algo_id

    def resolve(self, client_algo_id, algo_id):
        if not client_algo_id or not isinstance(algo_id, int) or algo_id <= 0:
            raise StopJournalError("STOP resolution invalid")
        self._append({"kind": "resolved", "client_algo_id": client_algo_id,
                      "algo_id": algo_id})

    def pending(self, symbol=None):
        """Return unresolved intents; malformed or conflicting lines fail closed."""
        if not self.path.exists():
            return []
        intents = {}
        resolved = {}
        with self.path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise StopJournalError("STOP journal line corrupted") from exc
                    kind = event.get("kind") if isinstance(event, dict) else None
                    if kind == "probe":
                        continue
                    client_id = event.get("client_algo_id")
                    if not isinstance(client_id, str) or not client_id:
                        raise StopJournalError("STOP journal client identity missing")
                    if kind == "intent":
                        if (not event.get("account_id") or not event.get("strategy")
                                or not event.get("symbol")
                                or (event.get("episode_entry_order_id") is not None and (
                                    not isinstance(event["episode_entry_order_id"], int)
                                    or isinstance(event["episode_entry_order_id"], bool)
                                    or event["episode_entry_order_id"] <= 0))
                                or event.get("stop_side") not in ("BUY", "SELL")):
                            raise StopJournalError("STOP journal intent invalid")
                        if client_id in intents and intents[client_id] != event:
                            raise StopJournalError("STOP journal intent conflict")
                        intents[client_id] = event
                    elif kind == "resolved":
                        if (not isinstance(event.get("algo_id"), int)
                                or isinstance(event["algo_id"], bool)
                                or event["algo_id"] <= 0):
                            raise StopJournalError("STOP journal resolution invalid")
                        if client_id in resolved and resolved[client_id] != event.get("algo_id"):
                            raise StopJournalError("STOP journal resolution conflict")
                        resolved[client_id] = event.get("algo_id")
                    else:
                        raise StopJournalError("STOP journal event type invalid")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if any(client_id not in intents for client_id in resolved):
            raise StopJournalError("STOP journal resolution has no intent")
        return [event for client_id, event in intents.items()
                if client_id not in resolved and (symbol is None or event["symbol"] == symbol)]
