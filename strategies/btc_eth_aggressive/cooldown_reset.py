"""Apply a sealed, one-time operator reset to one historical STOP cooldown."""

import hashlib
import json
import os
from pathlib import Path


def stop_cooldown_reset(account, strategy, symbol, last_fill_ms, now_ms, cooldown_ms):
    path = os.getenv("AGGRESSIVE_COOLDOWN_RESET_MANIFEST")
    pin = os.getenv("AGGRESSIVE_COOLDOWN_RESET_SHA256")
    if not path and not pin:
        return False
    if not path or not pin:
        raise ValueError("incomplete cooldown reset configuration")
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("cooldown reset checksum mismatch")
    reset = json.loads(raw)
    if (reset["account_id"], reset["strategy"], reset["symbol"], reset["last_fill_time_ms"]) != (
        account, strategy, symbol, last_fill_ms
    ):
        return False
    cutover = int(os.environ["BINANCE_CLASSIC_CUTOVER_MS"])
    approved = reset["approved_at_ms"]
    if (
        os.environ.get("BINANCE_ACCOUNT_MODE") != "classic"
        or reset["cutover_ms"] != cutover
        or type(approved) is not int
        or not last_fill_ms < cutover <= approved <= now_ms
        or not now_ms < last_fill_ms + cooldown_ms
        or reset["scope"] != "one_historical_stop_cooldown"
        or not reset["reason"]
    ):
        return False
    return True
