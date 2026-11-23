"""Dual-version Kubernetes-style backend resource store.

The package models one resource (a load-balancer backend) through two API
versions that share a single canonical state:

* a **spec** view with stable backend ids and full attributes
  (``name``, ``address``, ``weight``, ``drain``);
* a **legacy** view that only sees ``name`` and ``address`` and edits items by
  presenting per-item *credentials* returned by reads.

Both views write through the same transaction boundary, so a write from either
side is atomic, conflict-checked and visible to the other.

Quick start::

    from dualversion import BackendStore, WriteConflict

    store = BackendStore()
    created = store.spec_add("api", "10.0.0.1:80", weight=50)
    backend_id = created["id"]

    legacy = store.legacy_get()
    credential = legacy["items"][0]["credential"]
    store.legacy_write(credential, {"name": "api-v2"})
    store.spec_get(backend_id)["items"][0]["weight"]  # still 50
"""

from .errors import DualVersionError, ValidationError, WriteConflict
from .store import (
    DEFAULT_WEIGHT,
    LEGACY_FIELDS,
    MAX_WEIGHT,
    MIN_WEIGHT,
    STABLE_FIELDS,
    BackendStore,
)

__all__ = [
    "BackendStore",
    "DualVersionError",
    "ValidationError",
    "WriteConflict",
    "STABLE_FIELDS",
    "LEGACY_FIELDS",
    "DEFAULT_WEIGHT",
    "MIN_WEIGHT",
    "MAX_WEIGHT",
]
