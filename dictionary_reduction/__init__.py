"""Dictionary-coded string grouping and integer reductions.

The public API is intentionally small:

* :class:`DictionarySegment` / :class:`Shard` describe one local dictionary;
* :class:`ComparableDomain` and :class:`OrderDeclaration` describe collation
  and ordering;
* :class:`StableDictionary` keeps output codes stable;
* :class:`DictionaryReducer` performs incremental sum/first-row reduction;
* :func:`reduce_segments` is the one-shot entry point.
"""

from .dictionary import (
    DictionarySnapshot,
    LocalDictionary,
    OutputDictionary,
    StableDictionary,
)
from .domains import (
    BINARY,
    CASE_INSENSITIVE,
    CaseInsensitiveDomain,
    Collation,
    ComparableDomain,
    ComparableValueDomain,
    ValueDomain,
)
from .errors import (
    CodeCollisionError,
    CodeOutOfRangeError,
    DictionaryReductionError,
    DictionaryStructureError,
    DomainMismatchError,
    DomainValidationError,
    DuplicateGlobalRowError,
    MeasureError,
    NullMarkerError,
    OrderDeclarationError,
    RowIdentifierError,
)
from .ordering import OrderDeclaration, Ordering, SegmentOrder, SortDeclaration
from .reducer import (
    AggregateBatch,
    DictionaryReducer,
    EncodedBatch,
    GroupAggregate,
    GroupBatch,
    ReductionResult,
    SourceRef,
    dictionary_reduce,
    group_by_dictionary,
    reduce_codes,
    reduce_dictionary_shards,
    reduce_segments,
    reduce_shards,
    reduce_to_tuples,
)
from .segments import (
    DictionarySegment,
    DictionaryShard,
    Segment,
    Shard,
    normalize_null_markers,
)

# Descriptive aliases for the reduction operator and ordering declarations.
ReductionOperator = DictionaryReducer
DictionaryGroupBy = DictionaryReducer
GroupedReduction = DictionaryReducer
SegmentOrderDeclaration = OrderDeclaration
ComparableRange = ComparableDomain
aggregate_dictionary = reduce_segments
run_reduction = reduce_segments

__version__ = "1.0.0"

__all__ = [
    "BINARY",
    "CASE_INSENSITIVE",
    "AggregateBatch",
    "CaseInsensitiveDomain",
    "CodeCollisionError",
    "CodeOutOfRangeError",
    "Collation",
    "ComparableDomain",
    "ComparableRange",
    "ComparableValueDomain",
    "DictionaryGroupBy",
    "DictionaryReducer",
    "DictionaryReductionError",
    "DictionarySegment",
    "DictionaryShard",
    "DictionarySnapshot",
    "DictionaryStructureError",
    "DomainMismatchError",
    "DomainValidationError",
    "DuplicateGlobalRowError",
    "EncodedBatch",
    "GroupAggregate",
    "GroupBatch",
    "GroupedReduction",
    "LocalDictionary",
    "MeasureError",
    "NullMarkerError",
    "OrderDeclaration",
    "OrderDeclarationError",
    "Ordering",
    "OutputDictionary",
    "ReductionResult",
    "ReductionOperator",
    "RowIdentifierError",
    "Segment",
    "SegmentOrder",
    "SegmentOrderDeclaration",
    "Shard",
    "SortDeclaration",
    "SourceRef",
    "StableDictionary",
    "ValueDomain",
    "__version__",
    "aggregate_dictionary",
    "dictionary_reduce",
    "group_by_dictionary",
    "normalize_null_markers",
    "reduce_codes",
    "reduce_dictionary_shards",
    "reduce_segments",
    "reduce_shards",
    "reduce_to_tuples",
    "run_reduction",
]
