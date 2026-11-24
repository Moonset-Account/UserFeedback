"""The decomposition tableau: one row per sub-table, symbols per attribute.

For a sub-table ``R_i`` the seed row carries the *distinguished* symbol
``a:A`` in every column ``A`` that belongs to ``R_i`` and a fresh *null*
symbol ``n:i:A`` in every other column.  The classical chase characterisation
of lossless join says: the decomposition is lossless for all instances that
satisfy the dependencies iff the chase of this tableau derives a row in which
every column holds its own distinguished symbol.
"""

from __future__ import annotations

from typing import Sequence

from .model import Schema, SubTable

DISTINGUISHED_PREFIX = "a:"
NULL_PREFIX = "n:"


def distinguished(column: str) -> str:
    return DISTINGUISHED_PREFIX + column


def null_symbol(seed_index: int, column: str) -> str:
    return f"{NULL_PREFIX}{seed_index}:{column}"


def is_distinguished(symbol: str) -> bool:
    return isinstance(symbol, str) and symbol.startswith(DISTINGUISHED_PREFIX)


def build_seeds(
    schema: Schema, subtables: Sequence[SubTable]
) -> tuple[list[dict], list[dict]]:
    """Build the seed tableau.

    Returns ``(seeds, meta)`` where ``meta[i]`` describes the sub-table that
    produced ``seeds[i]``.  ``subtables`` must already be canonically sorted.
    """
    seeds: list[dict] = []
    meta: list[dict] = []
    for index, sub in enumerate(subtables):
        row = {
            col: (distinguished(col) if col in sub.columns else null_symbol(index, col))
            for col in schema.columns
        }
        seeds.append(row)
        meta.append(
            {
                "seed_row_id": index,
                "subtable": sub.name,
                "columns": list(sub.columns),
                "row": dict(row),
            }
        )
    return seeds, meta
