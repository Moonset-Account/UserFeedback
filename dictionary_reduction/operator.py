"""Compatibility module for callers looking for the reduction operator."""

from .reducer import (
    AggregateBatch,
    DictionaryReducer,
    EncodedBatch,
    GroupAggregate,
    ReductionResult,
    SourceRef,
    reduce_segments,
    reduce_to_tuples,
)

ReductionOperator = DictionaryReducer
DictionaryGroupBy = DictionaryReducer

__all__ = [
    "AggregateBatch",
    "DictionaryGroupBy",
    "DictionaryReducer",
    "EncodedBatch",
    "GroupAggregate",
    "ReductionOperator",
    "ReductionResult",
    "SourceRef",
    "reduce_segments",
    "reduce_to_tuples",
]
