"""Read-only virtual-address reading library for segmented memory images."""

from .core import (
    ByteRead,
    ConflictCandidate,
    ConflictSegment,
    FieldStatus,
    MapEntry,
    PageFragment,
    SnapshotStore,
    SourceInterval,
)
from .layout import ArrayField, BytesField, PointerField, StructDesc, UIntField
from .reader import (
    Budgets,
    FieldResult,
    ObjectReader,
    ObjectResult,
    TraversalResult,
)

__all__ = [
    "ArrayField",
    "Budgets",
    "ByteRead",
    "BytesField",
    "ConflictCandidate",
    "ConflictSegment",
    "FieldResult",
    "FieldStatus",
    "MapEntry",
    "ObjectReader",
    "ObjectResult",
    "PageFragment",
    "PointerField",
    "SnapshotStore",
    "SourceInterval",
    "StructDesc",
    "TraversalResult",
    "UIntField",
]
