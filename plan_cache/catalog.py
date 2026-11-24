"""Versioned object catalog and fixed-version catalog snapshots.

The catalog is the in-memory analogue of a database system catalog.  Every
catalog-changing DDL statement (:meth:`Catalog.create_namespace`,
:meth:`Catalog.create_table`, :meth:`Catalog.drop_table`) advances a global
monotonic *catalog version* and appends an immutable event to a versioned
event log.  A :class:`Transaction` pins the catalog version it saw at start;
all name resolution for that transaction goes through a
:class:`CatalogSnapshot` of exactly that version, so later DDL is invisible
to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CatalogEvent:
    """One immutable catalog change.

    ``relation_oid`` is ``None`` for namespace-only events.  For a table
    creation, ``namespace``/``name`` describe where the table was created;
    for a drop they describe the location the table was dropped from.
    """

    version: int
    kind: str  # "create_namespace" | "create_table" | "drop_table"
    namespace: Optional[str]
    name: Optional[str]
    relation_oid: Optional[int]


@dataclass(frozen=True)
class Resolution:
    """Result of resolving a bare table name along a search path."""

    oid: int
    name: str
    namespace: str
    position: int  # index of the winning namespace in the search path


class RelationNotFound(Exception):
    """Raised when a bare table name cannot be resolved.

    ``drop_event`` is set when the failure is explained by a drop event that
    is visible inside the current snapshot (a table a previous binding pointed
    at disappeared); otherwise it is a plain "no such table" failure.
    """

    def __init__(self, table_name: str, search_path: Tuple[str, ...],
                 drop_event: Optional[CatalogEvent] = None) -> None:
        self.table_name = table_name
        self.search_path = search_path
        self.drop_event = drop_event
        if drop_event is not None:
            msg = (f"table {table_name!r} was dropped at catalog version "
                   f"{drop_event.version} (oid={drop_event.relation_oid}); "
                   f"no replacement visible on search path {list(search_path)}")
        else:
            msg = (f"table {table_name!r} is not visible on search path "
                   f"{list(search_path)} at this catalog version")
        super().__init__(msg)


class NamespaceNotFound(Exception):
    """Raised when a search path names a namespace that does not exist."""


@dataclass
class _RelationRecord:
    oid: int
    name: str
    namespace: str
    created_at: int
    dropped_at: Optional[int] = None  # catalog version of the drop, if gone

    def visible_at(self, version: int) -> bool:
        return self.created_at <= version and (
            self.dropped_at is None or self.dropped_at > version
        )


class Catalog:
    """A versioned object catalog (namespaces + relations/tables)."""

    def __init__(self) -> None:
        self._version = 0
        self._next_oid = 1
        self._namespaces: Dict[str, int] = {}
        self._relations: List[_RelationRecord] = []
        self._events: List[CatalogEvent] = []

    # ------------------------------------------------------------------ state
    @property
    def current_version(self) -> int:
        """Latest catalog version visible to a newly started transaction."""
        return self._version

    @property
    def next_version(self) -> int:
        """Version the next DDL statement will produce."""
        return self._version + 1

    def events(self) -> Tuple[CatalogEvent, ...]:
        """Immutable copy of the full event log (for inspection/tests)."""
        return tuple(self._events)

    def events_between(self, after_version: int,
                       upto_version: int) -> Tuple[CatalogEvent, ...]:
        """Events with ``after_version < event.version <= upto_version``."""
        return tuple(
            e for e in self._events
            if after_version < e.version <= upto_version
        )

    def has_namespace(self, namespace: str, at_version: Optional[int] = None) -> bool:
        """Whether the namespace exists (currently or at ``at_version``)."""
        if at_version is None:
            return namespace in self._namespaces
        return any(
            e.kind == "create_namespace"
            and e.namespace == namespace
            and e.version <= at_version
            for e in self._events
        )

    def namespaces(self) -> Tuple[str, ...]:
        return tuple(self._namespaces.keys())

    # -------------------------------------------------------------------- DDL
    def create_namespace(self, namespace: str) -> CatalogEvent:
        if namespace in self._namespaces:
            raise ValueError(f"namespace {namespace!r} already exists")
        self._version += 1
        self._namespaces[namespace] = self._version
        event = CatalogEvent(
            version=self._version,
            kind="create_namespace",
            namespace=namespace,
            name=None,
            relation_oid=None,
        )
        self._events.append(event)
        return event

    def create_table(self, namespace: str, table_name: str) -> CatalogEvent:
        if namespace not in self._namespaces:
            raise NamespaceNotFound(f"namespace {namespace!r} does not exist")
        if any(
            r.namespace == namespace
            and r.name == table_name
            and r.dropped_at is None
            for r in self._relations
        ):
            raise ValueError(
                f"table {table_name!r} already exists in namespace "
                f"{namespace!r}"
            )
        self._version += 1
        oid = self._next_oid
        self._next_oid += 1
        self._relations.append(
            _RelationRecord(
                oid=oid,
                name=table_name,
                namespace=namespace,
                created_at=self._version,
            )
        )
        event = CatalogEvent(
            version=self._version,
            kind="create_table",
            namespace=namespace,
            name=table_name,
            relation_oid=oid,
        )
        self._events.append(event)
        return event

    def drop_table(self, namespace: str, table_name: str) -> CatalogEvent:
        record = next(
            (r for r in self._relations
             if r.namespace == namespace and r.name == table_name
             and r.dropped_at is None),
            None,
        )
        if record is None:
            raise RelationNotFound(table_name, (namespace,))
        self._version += 1
        record.dropped_at = self._version
        event = CatalogEvent(
            version=self._version,
            kind="drop_table",
            namespace=namespace,
            name=table_name,
            relation_oid=record.oid,
        )
        self._events.append(event)
        return event

    # --------------------------------------------------------------- snapshots
    def snapshot(self, version: int) -> "CatalogSnapshot":
        """Return a read-only view of the catalog frozen at ``version``."""
        if version < 0 or version > self._version:
            raise ValueError(
                f"catalog version {version} is out of range "
                f"(0..{self._version})"
            )
        return CatalogSnapshot(self, version)

    # ------------------------------------------------------------- live lookup
    def live_oid(self, namespace: str, table_name: str) -> Optional[int]:
        """OID of the currently live table at ``(namespace, name)``."""
        record = next(
            (r for r in self._relations
             if r.namespace == namespace and r.name == table_name
             and r.dropped_at is None),
            None,
        )
        return None if record is None else record.oid


class CatalogSnapshot:
    """Read-only catalog view fixed at one catalog version."""

    def __init__(self, catalog: Catalog, version: int) -> None:
        self._catalog = catalog
        self._version = version

    @property
    def version(self) -> int:
        return self._version

    def has_namespace(self, namespace: str) -> bool:
        return self._catalog.has_namespace(namespace, at_version=self._version)

    def resolve(self, table_name: str,
                search_path: Tuple[str, ...]) -> Resolution:
        """Resolve a bare table name: first hit along the search path wins.

        Namespaces missing at this snapshot version are skipped, matching the
        behaviour of a search path entry that points at a not-yet-created
        schema.
        """
        for position, namespace in enumerate(search_path):
            if not self.has_namespace(namespace):
                continue
            record = next(
                (r for r in self._catalog._relations  # noqa: SLF001
                 if r.name == table_name
                 and r.namespace == namespace
                 and r.visible_at(self._version)),
                None,
            )
            if record is not None:
                return Resolution(
                    oid=record.oid,
                    name=table_name,
                    namespace=namespace,
                    position=position,
                )
        raise RelationNotFound(table_name, tuple(search_path))

    def find_oid(self, oid: int) -> Optional[_RelationRecord]:
        """Return the record for ``oid`` if it is live at this version."""
        record = next(
            (r for r in self._catalog._relations  # noqa: SLF001
             if r.oid == oid),
            None,
        )
        if record is not None and record.visible_at(self._version):
            return record
        return None
