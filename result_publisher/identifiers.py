"""Deterministic, normalized identifiers for queries.

A :class:`~result_publisher.models.Query` carries a *normalized identifier*
so that queries with the same semantic intent (same dataset, same approved
purpose, same metric, same filter shape) share one cache entry even when
the raw filter objects differ in key order or equivalent set contents.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _normalize(value: Any) -> Any:
    """Recursively convert ``value`` into a canonical JSON-friendly form.

    * mappings are sorted by key (recursively);
    * sets are turned into sorted lists of normalized elements;
    * tuples become lists;
    * everything else is assumed JSON serializable by the caller.
    """

    if isinstance(value, dict):
        return {
            str(key): _normalize(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_normalize(item) for item in value), key=json.dumps)
    if isinstance(value, (list, tuple)):
        # Order-independent: filter membership is a set semantics in this
        # domain, so ["b", "a"] and ("a", "b") normalize identically.
        normalized = [_normalize(item) for item in value]
        return sorted(normalized, key=json.dumps)
    return value


def canonical_query_id(
    *,
    dataset: str,
    purpose: str,
    kind: str,
    filters: Any,
    extra: Any = None,
) -> str:
    """Return the stable normalized identifier for a query.

    The identifier is a SHA-256 hex digest of a canonical JSON document,
    so equivalent inputs (different dict key order, set vs list, etc.)
    always produce the same identifier while distinct semantics do not.
    """

    if not isinstance(dataset, str) or not dataset:
        raise ValueError("dataset must be a non-empty string")
    if not isinstance(purpose, str) or not purpose:
        raise ValueError("purpose must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")

    payload = {
        "dataset": dataset,
        "purpose": purpose,
        "kind": kind,
        "filters": _normalize(filters),
        "extra": _normalize(extra),
    }
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
