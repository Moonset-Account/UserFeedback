"""B-tree separator-key encoder and leaf finder.

The public entry points are:

* :class:`btree_separator.KeySchema` and :class:`btree_separator.ColumnSpec`
* :func:`btree_separator.choose_separator`
* :class:`btree_separator.SeparatorEncoder`
* :class:`btree_separator.Leaf`, :class:`btree_separator.BTree`, and
  :class:`btree_separator.BTreeFinder`
"""

from .encoder import (
    TAG_INT64,
    TAG_NEG_INFINITY,
    TAG_NULL,
    TAG_ROWID,
    TAG_TEXT,
    EncodedColumn,
    Separator,
    SeparatorEncoder,
    choose_separator,
    encode_separator,
    prefix_candidates,
    separator_sort_key,
    valid_separators,
)
from .model import (
    NEG_INFINITY,
    ColumnSpec,
    KeyOrderError,
    KeySchema,
    TextPrefix,
    canonicalize_key,
    compare_keys,
    is_sorted,
    validate_sorted_keys,
)
from .tree import (
    BTree,
    BTreeFinder,
    Leaf,
    LookupResult,
    Node,
    build_adjacent_separators,
    find_leaf,
)

__all__ = [
    "NEG_INFINITY",
    "TAG_INT64",
    "TAG_NEG_INFINITY",
    "TAG_NULL",
    "TAG_ROWID",
    "TAG_TEXT",
    "BTree",
    "BTreeSeparator",
    "BTreeSeparatorEncoder",
    "BTreeFinder",
    "Column",
    "ColumnSpec",
    "Encoder",
    "Finder",
    "SeparatorFinder",
    "build_adjacent_separators",
    "EncodedColumn",
    "KeyOrderError",
    "KeySchema",
    "Leaf",
    "LookupResult",
    "Node",
    "Separator",
    "SeparatorEncoder",
    "TextPrefix",
    "canonicalize_key",
    "choose_separator",
    "compare_keys",
    "encode_separator",
    "find_leaf",
    "is_sorted",
    "prefix_candidates",
    "separator_sort_key",
    "validate_sorted_keys",
    "valid_separators",
]

__version__ = "1.0.0"

# Concise/descriptive aliases for callers.
Column = ColumnSpec
Encoder = SeparatorEncoder
Finder = BTreeFinder
SeparatorFinder = BTreeFinder
BTreeSeparator = BTree
BTreeSeparatorEncoder = SeparatorEncoder
