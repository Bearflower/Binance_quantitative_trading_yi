"""Persist outstanding classic protection writes on each strategy's data mount."""

import json
import os
import sqlite3
import time
from contextlib import closing


class ClassicAlgoIntents:
    def __init__(self, path=None):
        self.path = path or os.environ.get("CLASSIC_ALGO_INTENT_DB", "/app/data/classic_algo_intents.sqlite3")

    def claim(self, account, strategy, params):
        """Return an outstanding client ID, or reserve the slot for this first POST."""
        if not account or not strategy:
            raise ValueError("经典条件单缺少账户或策略标识")
        family = "TAKE_PROFIT" if params["type"].startswith("TAKE_PROFIT") else "STOP"
        slot = (account, strategy, params["symbol"], family)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS pending (
                account TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, family TEXT NOT NULL,
                client_id TEXT NOT NULL UNIQUE, request TEXT NOT NULL, started_ms INTEGER NOT NULL,
                PRIMARY KEY(account, strategy, symbol, family)
            )""")
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT client_id FROM pending WHERE account=? AND strategy=? AND symbol=? AND family=?",
                slot,
            ).fetchone()
            if existing:
                return existing[0]
            db.execute("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (*slot, params["clientAlgoId"], json.dumps(params, sort_keys=True),
                        time.time_ns() // 1_000_000))
        return None

    def resolve(self, account, strategy, symbol, client_id):
        """Release exactly one recorded intent after its outcome and binding are known."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            changed = db.execute(
                "DELETE FROM pending WHERE account=? AND strategy=? AND symbol=? AND client_id=?",
                (account, strategy, symbol, client_id),
            ).rowcount
            if changed != 1:
                raise ValueError("经典条件单待核实记录不匹配")
