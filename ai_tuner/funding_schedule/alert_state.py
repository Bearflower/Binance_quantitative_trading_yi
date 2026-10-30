"""Persistent, non-evidence cooldown for monitor database outage alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


QUERY_ALERT_STATE_PATH = Path("/app/logs/funding_schedule_query_alert.json")
QUERY_ALERT_COOLDOWN = timedelta(minutes=15)


class RunQueryAlertCooldown:
    def __init__(self, path: Path = QUERY_ALERT_STATE_PATH) -> None:
        self.path = path

    def should_alert(self, now: datetime) -> bool:
        """A malformed or unavailable state fails open to one alert attempt."""
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            last = datetime.fromtimestamp(float(state["at"]), tz=timezone.utc)
            elapsed = now - last
            return not timedelta(0) <= elapsed < QUERY_ALERT_COOLDOWN
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return True

    def mark(self, now: datetime) -> None:
        """Persist before sending so a restart cannot immediately duplicate it."""
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps({"at": now.timestamp()}), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
