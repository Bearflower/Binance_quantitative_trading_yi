"""Durable decision handoff for the separate order-grid executor.

Migration is explicit. The signal producer and executor refuse incompatible schemas.
"""

import argparse
import fcntl
import hashlib
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

from strategies.grid.market_state import MarketState


SCHEMA_VERSION = 5


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    return conn


def migrate(path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row and row[0] != str(SCHEMA_VERSION):
            raise RuntimeError("execution schema version mismatch")
        conn.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('version',?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            calculated_at_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS decisions_time ON decisions(calculated_at_ms,decision_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS execution_cursor (
            id INTEGER PRIMARY KEY CHECK(id=1), decision_id TEXT REFERENCES decisions(decision_id)
        )""")
        conn.execute("INSERT OR IGNORE INTO execution_cursor(id,decision_id) VALUES(1,NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS grid_epoch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            levels_json TEXT,
            stop_loss_low TEXT,
            stop_loss_high TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at_ms INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS order_intent (
            client_order_id TEXT PRIMARY KEY,
            epoch_id INTEGER NOT NULL REFERENCES grid_epoch(id),
            level_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            side TEXT NOT NULL,
            price TEXT NOT NULL,
            quantity TEXT NOT NULL,
            status TEXT NOT NULL,
            exchange_order_id TEXT,
            executed_qty TEXT NOT NULL DEFAULT '0',
            created_at_ms INTEGER NOT NULL
        )""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_grid_level
            ON order_intent(epoch_id,level_index,role)
            WHERE status IN ('PREPARED','SENT','NEW','PARTIALLY_FILLED','UNKNOWN')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fill (
            exchange_order_id TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL REFERENCES order_intent(client_order_id),
            quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            PRIMARY KEY(exchange_order_id,trade_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pair (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch_id INTEGER NOT NULL REFERENCES grid_epoch(id),
            source_client_order_id TEXT NOT NULL,
            reverse_client_order_id TEXT NOT NULL UNIQUE,
            quantity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PREPARED'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS protection (
            client_algo_id TEXT PRIMARY KEY,
            epoch_id INTEGER NOT NULL REFERENCES grid_epoch(id),
            side TEXT NOT NULL,
            trigger_price TEXT NOT NULL,
            status TEXT NOT NULL,
            exchange_algo_id TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS engine_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            state TEXT NOT NULL,
            lock_reason TEXT,
            updated_at_ms INTEGER NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO engine_state VALUES(1,'FLAT',NULL,?)",
                     (int(time.time() * 1000),))
        conn.execute("""CREATE TABLE IF NOT EXISTS equity_checkpoint (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_beijing TEXT NOT NULL,
            baseline_equity TEXT NOT NULL,
            baseline_verified INTEGER NOT NULL,
            equity TEXT NOT NULL,
            peak_equity TEXT NOT NULL,
            transfer_fingerprint TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        )""")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _validate(payload: Mapping) -> None:
    if payload.get("symbol") != "ETHUSDT":
        raise ValueError("decision symbol must be ETHUSDT")
    state = payload.get("state")
    if state not in MarketState.__members__:
        raise ValueError("invalid decision state")
    if not isinstance(payload.get("calculated_at_ms"), int) or payload["calculated_at_ms"] <= 0:
        raise ValueError("invalid decision timestamp")
    times = payload.get("last_closed_kline_ms")
    if not isinstance(times, dict) or set(times) != {"15m", "1h", "4h"}:
        raise ValueError("missing Kline close timestamps")
    if any(not isinstance(value, int) or value < 0 for value in times.values()):
        raise ValueError("invalid Kline close timestamp")


class DecisionStore:
    def __init__(self, path):
        self.conn = _connect(Path(path))
        try:
            row = self.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if row is None or row[0] != str(SCHEMA_VERSION):
                raise RuntimeError("execution schema version mismatch")
            self.conn.execute("SELECT decision_id FROM decisions LIMIT 1")
        except sqlite3.Error as exc:
            self.conn.close()
            raise RuntimeError("execution schema missing; run migration") from exc
        except Exception:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def publish(self, payload: Mapping, config_hash: str) -> str:
        _validate(payload)
        if not config_hash:
            raise ValueError("config hash required")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256((canonical + config_hash).encode()).hexdigest()
        decision_id = "grid-" + digest[:32]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT MAX(calculated_at_ms) FROM decisions").fetchone()
            if row[0] is not None and payload["calculated_at_ms"] < row[0]:
                raise ValueError("decision timestamp moved backwards")
            self.conn.execute("""INSERT OR IGNORE INTO decisions
                (decision_id,calculated_at_ms,payload_json,config_hash,created_at_ms)
                VALUES(?,?,?,?,?)""", (decision_id, payload["calculated_at_ms"], canonical,
                                      config_hash, int(time.time() * 1000)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return decision_id

    def cursor(self) -> Optional[str]:
        row = self.conn.execute("SELECT decision_id FROM execution_cursor WHERE id=1").fetchone()
        return row[0]

    def next_decision(self) -> Optional[dict]:
        rows = self._pending_rows(limit=1)
        row = rows[0] if rows else None
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        _validate(payload)
        payload["decision_id"] = row["decision_id"]
        payload["config_hash"] = row["config_hash"]
        return payload

    def _pending_rows(self, limit=None):
        cursor = self.cursor()
        if cursor is None:
            cursor_rowid = 0
        else:
            current = self.conn.execute("SELECT rowid FROM decisions WHERE decision_id=?",
                                        (cursor,)).fetchone()
            if current is None:
                raise RuntimeError("execution cursor has no decision")
            cursor_rowid = current[0]
        sql = "SELECT * FROM decisions WHERE rowid>? ORDER BY rowid"
        if limit is not None:
            return self.conn.execute(sql + " LIMIT ?", (cursor_rowid, limit)).fetchall()
        return self.conn.execute(sql, (cursor_rowid,)).fetchall()

    def pending_decisions(self) -> list[dict]:
        result = []
        for row in self._pending_rows():
            payload = json.loads(row["payload_json"])
            _validate(payload)
            payload["decision_id"] = row["decision_id"]
            payload["config_hash"] = row["config_hash"]
            result.append(payload)
        return result

    def acknowledge_through(self, decision_id: str) -> None:
        """Advance past superseded signals only after caller made the account safe."""
        rows = self._pending_rows()
        if decision_id not in {row["decision_id"] for row in rows}:
            raise ValueError("decision is not pending")
        self.conn.execute("UPDATE execution_cursor SET decision_id=? WHERE id=1",
                          (decision_id,))

    def acknowledge(self, decision_id: str) -> None:
        next_item = self.next_decision()
        if next_item is None or next_item["decision_id"] != decision_id:
            raise ValueError("decision is not next")
        self.conn.execute("UPDATE execution_cursor SET decision_id=? WHERE id=1", (decision_id,))

    def create_epoch(self, decision_id: str, plan_digest: str = "", *,
                     levels=None, stop_loss_low=None, stop_loss_high=None) -> int:
        if not decision_id:
            raise ValueError("decision ID required")
        row = self.conn.execute("""INSERT INTO grid_epoch
            (decision_id,plan_digest,levels_json,stop_loss_low,stop_loss_high,
             created_at_ms) VALUES(?,?,?,?,?,?)""",
            (decision_id, plan_digest,
             json.dumps([str(level) for level in levels]) if levels else None,
             str(stop_loss_low) if stop_loss_low is not None else None,
             str(stop_loss_high) if stop_loss_high is not None else None,
             int(time.time() * 1000)))
        return row.lastrowid

    def active_epoch_id(self) -> Optional[int]:
        rows = self.conn.execute("""SELECT id FROM grid_epoch
            WHERE status='ACTIVE' ORDER BY id DESC""").fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple active grid epochs")
        return rows[0][0] if rows else None

    def close_epoch(self, epoch_id: int) -> None:
        row = self.conn.execute("""UPDATE grid_epoch SET status='CLOSED'
            WHERE id=? AND status='ACTIVE'""", (epoch_id,))
        if row.rowcount != 1:
            raise ValueError("epoch not active")

    def epoch(self, epoch_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM grid_epoch WHERE id=?", (epoch_id,)).fetchone()
        return dict(row) if row else None

    def prepare_order(self, epoch_id: int, level_index: int, role: str,
                      side: str, price, quantity, client_order_id: str) -> dict:
        if not client_order_id.startswith("eg-") or side not in ("BUY", "SELL"):
            raise ValueError("invalid order intent")
        if Decimal(str(quantity)) <= 0 or Decimal(str(price)) <= 0:
            raise ValueError("invalid order amount")
        try:
            self.conn.execute("""INSERT INTO order_intent
                (client_order_id,epoch_id,level_index,role,side,price,quantity,status,created_at_ms)
                VALUES(?,?,?,?,?,?,?,'PREPARED',?)""",
                (client_order_id, epoch_id, level_index, role, side, str(price),
                 str(quantity), int(time.time() * 1000)))
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate active order intent") from exc
        return self.get_order(client_order_id)

    def get_order(self, client_order_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM order_intent WHERE client_order_id=?",
                                (client_order_id,)).fetchone()
        return dict(row) if row else None

    def active_orders(self) -> list:
        rows = self.conn.execute("""SELECT * FROM order_intent
            WHERE status IN ('PREPARED','SENT','NEW','PARTIALLY_FILLED','UNKNOWN')""").fetchall()
        return [dict(row) for row in rows]

    def all_orders(self) -> list:
        rows = self.conn.execute("SELECT * FROM order_intent ORDER BY created_at_ms,rowid").fetchall()
        return [dict(row) for row in rows]

    def record_order(self, client_order_id: str, exchange_order_id: str,
                     status: str, executed_qty) -> None:
        if status not in ("PREPARED", "SENT", "NEW", "PARTIALLY_FILLED",
                          "FILLED", "CANCELED", "EXPIRED", "UNKNOWN"):
            raise ValueError("invalid order status")
        if Decimal(str(executed_qty)) < 0:
            raise ValueError("negative executed quantity")
        row = self.conn.execute("""UPDATE order_intent SET exchange_order_id=?,
            status=?,executed_qty=? WHERE client_order_id=?""",
            (str(exchange_order_id), status, str(executed_qty), client_order_id))
        if row.rowcount != 1:
            raise ValueError("unknown order intent")

    def record_fill(self, client_order_id: str, exchange_order_id: str,
                    trade_id: str, quantity, price) -> bool:
        if Decimal(str(quantity)) <= 0 or Decimal(str(price)) <= 0:
            raise ValueError("invalid fill")
        row = self.conn.execute("""INSERT OR IGNORE INTO fill
            (exchange_order_id,trade_id,client_order_id,quantity,price,created_at_ms)
            VALUES(?,?,?,?,?,?)""", (str(exchange_order_id), str(trade_id),
                client_order_id, str(quantity), str(price), int(time.time() * 1000)))
        return row.rowcount == 1

    def fills(self, client_order_id: str) -> list:
        rows = self.conn.execute("SELECT * FROM fill WHERE client_order_id=?",
                                 (client_order_id,)).fetchall()
        return [dict(row) for row in rows]

    def all_fills(self) -> list:
        rows = self.conn.execute("""SELECT fill.*,order_intent.side
            FROM fill JOIN order_intent USING(client_order_id)""").fetchall()
        return [dict(row) for row in rows]

    def engine_state(self) -> dict:
        return dict(self.conn.execute("SELECT * FROM engine_state WHERE id=1").fetchone())

    def set_engine_state(self, state: str, reason: Optional[str] = None) -> None:
        if state not in ("FLAT", "ACTIVE", "PAUSED", "EXITING", "LOCKED"):
            raise ValueError("invalid engine state")
        current = self.engine_state()
        if current["state"] == "LOCKED" and state != "LOCKED":
            raise RuntimeError("locked state requires explicit operator unlock")
        self.conn.execute("""UPDATE engine_state SET state=?,lock_reason=?,updated_at_ms=?
            WHERE id=1""", (state, reason, int(time.time() * 1000)))

    def active_protections(self) -> list:
        rows = self.conn.execute("""SELECT * FROM protection
            WHERE status IN ('PREPARED','NEW','UNKNOWN')""").fetchall()
        return [dict(row) for row in rows]

    def record_protection(self, client_algo_id: str, status: str,
                          exchange_algo_id: Optional[str] = None) -> None:
        if status not in ("PREPARED", "NEW", "CANCELED", "TRIGGERED", "UNKNOWN"):
            raise ValueError("invalid protection status")
        row = self.conn.execute("""UPDATE protection SET status=?,exchange_algo_id=?
            WHERE client_algo_id=?""", (status, exchange_algo_id, client_algo_id))
        if row.rowcount != 1:
            raise ValueError("unknown protection intent")

    def latest_equity_checkpoint(self, day_beijing: str) -> Optional[dict]:
        row = self.conn.execute("""SELECT * FROM equity_checkpoint
            WHERE day_beijing=? ORDER BY id DESC LIMIT 1""", (day_beijing,)).fetchone()
        return dict(row) if row else None

    def record_equity_checkpoint(self, day_beijing: str, baseline_equity,
                                 equity, peak_equity, observed_at_ms: int,
                                 *, verified: bool, transfer_fingerprint: str) -> None:
        for value in (baseline_equity, equity, peak_equity):
            if Decimal(str(value)) <= 0:
                raise ValueError("invalid equity checkpoint")
        self.conn.execute("""INSERT INTO equity_checkpoint
            (day_beijing,baseline_equity,baseline_verified,equity,peak_equity,
             transfer_fingerprint,created_at_ms) VALUES(?,?,?,?,?,?,?)""",
            (day_beijing, str(baseline_equity), int(verified), str(equity),
             str(peak_equity), transfer_fingerprint, observed_at_ms))


class ExecutionLock:
    """OS file lock held for the executor's entire lifetime."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("execution lock is held") from exc
        return self

    def __exit__(self, *_):
        if self.file is not None:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    args = parser.parse_args()
    migrate(args.db_path)
