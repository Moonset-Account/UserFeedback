"""Compressed arbitrary-precision rank index for a three-table cyclic join.

Public API overview::

    from rank_index import RankIndex, r_row, s_row, t_row

    db = RankIndex(
        R=[r_row("r1", 1, 2, 10_000)],
        S=[s_row("s1", 2, 3, 10_000)],
        T=[t_row("t1", 3, 1, 10_000)],
    )
    snap = db.current
    assert snap.total == 10**12
    last = snap.at(snap.total - 1)
    assert snap.rank_of(last) == snap.total - 1

"""

from .database import RankIndex, Transaction
from .mapping import (
    AffineSegment,
    BlockMapping,
    IntervalPattern,
    LazyRangeSet,
    RankMapping,
    RankTranslation,
    RangeCollection,
)
from .model import (
    AddedRankError,
    ConcurrentUpdateError,
    Copy,
    CopyNotFoundError,
    DeletedRankError,
    DuplicateRowError,
    InvalidRankError,
    InvalidRowError,
    RankIndexError,
    Row,
    RowNotFoundError,
    RRow,
    SRow,
    TRow,
    r_row,
    s_row,
    t_row,
)
from .snapshot import Cursor, Snapshot

__all__ = [
    "RankIndex",
    "Transaction",
    "Snapshot",
    "Cursor",
    "RankMapping",
    "RankTranslation",
    "AffineSegment",
    "BlockMapping",
    "IntervalPattern",
    "LazyRangeSet",
    "RangeCollection",
    "Row",
    "Copy",
    "r_row",
    "s_row",
    "t_row",
    "RRow",
    "SRow",
    "TRow",
    "RankIndexError",
    "InvalidRowError",
    "DuplicateRowError",
    "RowNotFoundError",
    "InvalidRankError",
    "CopyNotFoundError",
    "DeletedRankError",
    "AddedRankError",
    "ConcurrentUpdateError",
]

__version__ = "1.0.0"
