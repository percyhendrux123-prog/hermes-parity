"""Fake Hermes clock (the driver pins ``now``)."""
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)
