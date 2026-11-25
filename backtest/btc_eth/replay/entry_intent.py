"""Attribution labels only; never derive a trading route from market regime."""

INTENTS = ("trend", "mean_reversion", "unknown")


def normalize_intent(value):
    return value if isinstance(value, str) and value in INTENTS else "unknown"


def event_intent(event):
    snapshot = event.get("feature_snapshot")
    return normalize_intent(snapshot.get("entry_intent")) if isinstance(snapshot, dict) else "unknown"
