"""Outbound review package: match outbound table shards against a preprocessed
internal roster without ever exposing original values."""

from .digest import (
    COLUMN_NORMALIZERS,
    TEST_KEY,
    digest_value,
    normalize_value,
)
from .review import (
    CellLocation,
    Hit,
    ReviewAssembler,
    ReviewReport,
    run_review,
)
from .roster import (
    InternalIndex,
    RosterEntry,
    load_preprocessed,
    preprocess_roster,
)

__all__ = [
    "COLUMN_NORMALIZERS",
    "TEST_KEY",
    "CellLocation",
    "Hit",
    "InternalIndex",
    "ReviewAssembler",
    "ReviewReport",
    "RosterEntry",
    "digest_value",
    "load_preprocessed",
    "normalize_value",
    "preprocess_roster",
    "run_review",
]
