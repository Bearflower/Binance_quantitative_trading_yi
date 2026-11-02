"""Non-evidence, persistent delivery state for the funding collector's alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


ALERT_STATE_PATH = Path("/app/logs/funding_schedule_query_alert.json")
ATTEMPT_COOLDOWN = timedelta(seconds=60)
DELIVERED_FAILURE_COOLDOWN = timedelta(minutes=15)
MISSING_SLOT_RETENTION = timedelta(hours=24)


class AlertStateUnavailable(RuntimeError):
    """Persistent alert delivery state cannot be trusted; never emit blindly."""

    def __init__(self) -> None:
        super().__init__("funding schedule alert state unavailable")


class PersistentAlertState:
    """Separate attempted sends from confirmed delivery across process restarts."""

    def __init__(self, path: Path = ALERT_STATE_PATH) -> None:
        self.path = path

    def _read(self) -> dict:
        # Absence of the file is normal only inside a writable, dedicated
        # state directory. Otherwise a successful no-gap DB check could
        # refresh health while alert deduplication is impossible.
        if (not self.path.parent.is_dir() or self.path.parent.is_symlink()
                or not os.access(self.path.parent, os.W_OK | os.X_OK)):
            raise AlertStateUnavailable()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise AlertStateUnavailable()
            # Prior revision stored only an attempt, not delivery proof.
            if "at" in payload:
                return {"RUN_QUERY_FAILED": {"attempted_at": payload["at"]}}
            return payload
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AlertStateUnavailable() from None

    def should_attempt(self, key: str, now: datetime, *, delivered_cooldown: timedelta | None) -> bool:
        entry = self._read().get(key)
        if not isinstance(entry, dict):
            if entry is None:
                return True
            raise AlertStateUnavailable()
        try:
            delivered = entry.get("delivered_at")
            if delivered is not None:
                if delivered_cooldown is None:
                    return False
                elapsed = now - datetime.fromtimestamp(float(delivered), tz=timezone.utc)
                if timedelta(0) <= elapsed < delivered_cooldown:
                    return False
            attempted = entry.get("attempted_at")
            if attempted is not None:
                elapsed = now - datetime.fromtimestamp(float(attempted), tz=timezone.utc)
                if timedelta(0) <= elapsed < ATTEMPT_COOLDOWN:
                    return False
        except (ValueError, TypeError, OverflowError):
            raise AlertStateUnavailable() from None
        return True

    def mark_attempt(self, key: str, now: datetime) -> None:
        state = self._read()
        self._prune_missing_slots(state, now)
        entry = state.get(key)
        if not isinstance(entry, dict):
            entry = {}
        entry["attempted_at"] = now.timestamp()
        state[key] = entry
        self._write(state)

    def mark_delivered(self, key: str, now: datetime) -> None:
        state = self._read()
        self._prune_missing_slots(state, now)
        entry = state.get(key)
        if not isinstance(entry, dict):
            entry = {}
        entry["delivered_at"] = now.timestamp()
        state[key] = entry
        self._write(state)

    def clear(self, key: str) -> None:
        state = self._read()
        if key in state:
            del state[key]
            try:
                if state:
                    self._write(state)
                else:
                    self.path.unlink(missing_ok=True)
            except OSError:
                raise AlertStateUnavailable() from None

    def _write(self, state: dict) -> None:
        temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            raise AlertStateUnavailable() from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _prune_missing_slots(state: dict, now: datetime) -> None:
        for key, entry in list(state.items()):
            if not key.startswith("MISSING_SCHEDULE_SLOT:"):
                continue
            try:
                stamp = entry.get("delivered_at", entry.get("attempted_at"))
                recorded = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
                if now - recorded >= MISSING_SLOT_RETENTION:
                    del state[key]
            except (AttributeError, ValueError, TypeError, OverflowError):
                del state[key]
