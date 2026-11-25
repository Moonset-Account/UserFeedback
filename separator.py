"""Compatibility module for callers that prefer ``import separator``.

The implementation lives in the :mod:`btree_separator` package.
"""

from btree_separator import *  # noqa: F403
from btree_separator import (  # noqa: F401
    BTree,
    BTreeFinder,
    NEG_INFINITY,
    ColumnSpec,
    EncodedColumn,
    KeyOrderError,
    KeySchema,
    Leaf,
    LookupResult,
    Separator,
    SeparatorEncoder,
    TextPrefix,
    choose_separator,
    compare_keys,
    encode_separator,
    find_leaf,
    prefix_candidates,
    valid_separators,
)

__all__ = [
    "BTree",
    "BTreeFinder",
    "NEG_INFINITY",
    "ColumnSpec",
    "EncodedColumn",
    "KeyOrderError",
    "KeySchema",
    "Leaf",
    "LookupResult",
    "Separator",
    "SeparatorEncoder",
    "TextPrefix",
    "choose_separator",
    "compare_keys",
    "encode_separator",
    "find_leaf",
    "prefix_candidates",
    "valid_separators",
]
