"""Binding cache for prepared statements with bare (unqualified) table names.

A prepared statement references tables by bare names; the concrete object
number (OID) those names denote depends on the session's search path and on
the catalog version the executing transaction pinned when it started.

The cache key is the triple ``(table name, search path, statement text)``.
For every key it keeps the *history* of bindings instead of only the newest
one: each entry records which OID was bound at which catalog version.  A
transaction whose snapshot version is at least as new as some retained
binding can reuse that binding exactly when nothing relevant changed in
between.  This is what lets an old transaction keep hitting the object it
bound originally while a new transaction re-binds to a shadowing table
created in an earlier namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .catalog import (
    CatalogEvent,
    CatalogSnapshot,
    RelationNotFound,
    Resolution,
)

# --------------------------------------------------------------------- reasons
# Hit (no re-bind happened).
HIT_FRESH = "hit_fresh"
"""Reused a binding made at this exact catalog version."""

HIT_NO_RELEVANT_CHANGE = "hit_no_relevant_change"
"""Catalog changed, but every event was outside this search path."""

# Re-bind reasons.
REBIND_FIRST_USE = "rebind_first_use"
"""No cached binding exists for this name/path/statement key."""

REBIND_SHADOWED = "rebind_shadowed"
"""A same-named table now appears in an earlier namespace on the path."""

REBIND_DROPPED = "rebind_dropped"
"""The previously bound relation was dropped."""

REBIND_RESOLUTION_CHANGED = "rebind_resolution_changed"
"""Re-resolution yields a different object for another reason."""

# Supplementary reason attached by the session layer.
SEARCH_PATH_CHANGED = "search_path_changed"
"""The statement's search path changed since its previous lookup."""

#: Every reason constant defined by this module, useful for assertions.
BIND_REASONS = frozenset(
    {
        HIT_FRESH,
        HIT_NO_RELEVANT_CHANGE,
        REBIND_FIRST_USE,
        REBIND_SHADOWED,
        REBIND_DROPPED,
        REBIND_RESOLUTION_CHANGED,
        SEARCH_PATH_CHANGED,
    }
)


@dataclass(frozen=True)
class Binding:
    """One historical binding: an OID pinned at a catalog version."""

    oid: int
    namespace: str
    position: int
    catalog_version: int


@dataclass(frozen=True)
class BindResult:
    """Outcome of a cache lookup.

    ``hit`` is ``True`` when a cached binding was reused unchanged; it is
    ``False`` when a fresh resolution (re-bind) was performed.  ``reason``
    carries one of the module-level reason constants and ``previous_binding``
    exposes the binding that was replaced, when there was one.
    """

    hit: bool
    reason: str
    binding: Binding
    previous_binding: Optional[Binding] = None
    events: Tuple[CatalogEvent, ...] = ()

    @property
    def rebound(self) -> bool:
        return not self.hit

    @property
    def oid(self) -> int:
        return self.binding.oid


@dataclass
class _CacheEntry:
    key: Tuple[Any, ...]
    history: List[Binding]


class BindingCache:
    """Snapshot-aware binding cache shared across transactions/sessions."""

    def __init__(self) -> None:
        self._entries: Dict[Tuple[Any, ...], _CacheEntry] = {}

    # --------------------------------------------------------------- helpers
    @staticmethod
    def make_key(table_name: str, search_path: Tuple[str, ...],
                 statement_text: str) -> Tuple[Any, ...]:
        return statement_text, table_name, tuple(search_path)

    def history(self, table_name: str, search_path: Tuple[str, ...],
                statement_text: str) -> Tuple[Binding, ...]:
        """Expose the retained binding history for one key (for inspection)."""
        entry = self._entries.get(
            self.make_key(table_name, search_path, statement_text)
        )
        return tuple(entry.history) if entry is not None else ()

    def clear(self) -> None:
        self._entries.clear()

    # ----------------------------------------------------------------- lookup
    def lookup(self, table_name: str, search_path: Tuple[str, ...],
               statement_text: str,
               snapshot: CatalogSnapshot) -> BindResult:
        """Return a stable OID binding for ``table_name``.

        The newest retained binding whose catalog version is not newer than
        the transaction's snapshot version is reused when none of the catalog
        events between the binding version and the snapshot version could
        alter the resolution of this bare name along this search path.
        Bindings made at later catalog versions (by concurrent transactions
        that started later) are skipped, not treated as invalidations.
        Otherwise the name is re-resolved against the snapshot and the new
        binding is appended to the entry's version-ordered history.
        """
        key = self.make_key(table_name, search_path, statement_text)
        entry = self._entries.get(key)
        snapshot_version = snapshot.version

        if entry is not None and entry.history:
            # Newest retained binding that is already visible to this
            # transaction's snapshot.  History may hold bindings newer than
            # this snapshot (made by concurrent, later transactions); those
            # belong to the future from this transaction's point of view and
            # must be skipped, not treated as invalidations.
            visible = [
                b for b in entry.history
                if b.catalog_version <= snapshot_version
            ]
            if visible:
                latest = max(visible, key=lambda b: b.catalog_version)
                events = self._relevant_events(
                    latest, table_name, search_path, snapshot
                )
                if not events:
                    reason = (
                        HIT_FRESH
                        if latest.catalog_version == snapshot_version
                        else HIT_NO_RELEVANT_CHANGE
                    )
                    return BindResult(
                        hit=True,
                        reason=reason,
                        binding=latest,
                        events=(),
                    )
                # A relevant event occurred: re-resolve and explain why.
                return self._rebind(
                    entry, latest, events, table_name, search_path, snapshot
                )

        # First lookup for this key, or no binding visible to this snapshot.
        return self._bind_first(entry, key, table_name, search_path, snapshot)

    # ------------------------------------------------------------- internals
    def _bind_first(self, entry: Optional[_CacheEntry],
                    key: Tuple[Any, ...], table_name: str,
                    search_path: Tuple[str, ...],
                    snapshot: CatalogSnapshot) -> BindResult:
        resolution = snapshot.resolve(table_name, search_path)
        binding = self._make_binding(resolution, snapshot.version)
        if entry is None:
            self._entries[key] = _CacheEntry(key=key, history=[binding])
        else:
            # Older transactions may populate the same key out of version
            # order; keep the history ordered by catalog version.
            entry.history.append(binding)
            entry.history.sort(key=lambda b: b.catalog_version)
        return BindResult(
            hit=False,
            reason=REBIND_FIRST_USE,
            binding=binding,
            events=(),
        )

    def _rebind(self, entry: _CacheEntry, previous: Binding,
                events: Tuple[CatalogEvent, ...], table_name: str,
                search_path: Tuple[str, ...],
                snapshot: CatalogSnapshot) -> BindResult:
        try:
            resolution = snapshot.resolve(table_name, search_path)
        except RelationNotFound as exc:
            # Re-resolution failed: annotate with the drop that explains it.
            drop = next(
                (e for e in events
                 if e.kind == "drop_table"
                 and e.relation_oid == previous.oid),
                None,
            )
            exc.drop_event = drop
            raise

        binding = self._make_binding(resolution, snapshot.version)
        reason = self._explain_rebind(previous, binding, events)
        entry.history.append(binding)
        entry.history.sort(key=lambda b: b.catalog_version)
        return BindResult(
            hit=False,
            reason=reason,
            binding=binding,
            previous_binding=previous,
            events=events,
        )

    @staticmethod
    def _make_binding(resolution: Resolution,
                      catalog_version: int) -> Binding:
        return Binding(
            oid=resolution.oid,
            namespace=resolution.namespace,
            position=resolution.position,
            catalog_version=catalog_version,
        )

    @staticmethod
    def _explain_rebind(previous: Binding, binding: Binding,
                        events: Tuple[CatalogEvent, ...]) -> str:
        if binding.oid == previous.oid:
            # The bound object did not change; something relevant nevertheless
            # happened (e.g. a shadow was created and later removed).
            return REBIND_RESOLUTION_CHANGED
        if any(
            e.kind == "drop_table" and e.relation_oid == previous.oid
            for e in events
        ):
            return REBIND_DROPPED
        # A same-named table resolving in a namespace earlier on the search
        # path is the classic "shadowing" case; any other OID change is a
        # generic resolution change.
        if binding.position < previous.position:
            return REBIND_SHADOWED
        return REBIND_RESOLUTION_CHANGED

    def _relevant_events(self, binding: Binding, table_name: str,
                         search_path: Tuple[str, ...],
                         snapshot: CatalogSnapshot) -> Tuple[CatalogEvent, ...]:
        """Events that could change name resolution for this key.

        Only events touching a table with the same bare name and a namespace
        that is *on this search path* count.  DDL in namespaces outside the
        path is invisible to the lookup by definition, so the cached plan may
        keep running unchanged.
        """
        on_path = set(search_path)
        result = []
        for event in snapshot._catalog.events_between(  # noqa: SLF001
            binding.catalog_version, snapshot.version
        ):
            if event.relation_oid is None:
                continue  # namespace-only DDL never moves a table binding
            if event.name != table_name:
                continue
            if event.namespace not in on_path:
                continue
            result.append(event)
        return tuple(result)
