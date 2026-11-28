"""Display timestamps for grid notifications independently of the container timezone."""
from datetime import datetime, timedelta, timezone


_BEIJING = timezone(timedelta(hours=8))


def beijing_time(timestamp_ms=None):
    moment = (datetime.now(_BEIJING) if timestamp_ms is None
              else datetime.fromtimestamp(timestamp_ms / 1000, _BEIJING))
    return moment.strftime("%Y-%m-%d %H:%M:%S")
