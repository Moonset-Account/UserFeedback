"""Shared fixtures for the revocation store acceptance tests."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    """Timestamp ``hours`` after the fixed test base instant."""
    return BASE + timedelta(hours=hours)


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)
