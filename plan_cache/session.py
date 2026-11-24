"""Sessions, transactions and prepared statements on top of the binding cache.

* :class:`Transaction` pins the catalog version visible at ``BEGIN`` time and
  keeps that snapshot for its whole lifetime, however many DDL changes happen
  in the meantime.
* :class:`PreparedStatement` remembers the bare table name and the search
  path it was prepared with.  Executing it under a changed path looks the new
  cache key up and annotates the result with :data:`SEARCH_PATH_CHANGED`.
* :class:`Session` owns the current search path and hands every lookup to one
  shared :class:`~plan_cache.cache.BindingCache`, so bindings prepared in one
  transaction are visible to the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .cache import (
    SEARCH_PATH_CHANGED,
    BindResult,
    BindingCache,
)
from .catalog import Catalog, CatalogSnapshot, NamespaceNotFound


class Transaction:
    """A transaction pinned to one catalog version."""

    def __init__(self, catalog: Catalog,
                 snapshot_version: Optional[int] = None) -> None:
        self._catalog = catalog
        self._snapshot_version = (
            catalog.current_version
            if snapshot_version is None
            else snapshot_version
        )
        self._snapshot = catalog.snapshot(self._snapshot_version)
        self._closed = False

    @property
    def snapshot_version(self) -> int:
        return self._snapshot_version

    @property
    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    @property
    def closed(self) -> bool:
        return self._closed

    def commit(self) -> None:
        self._closed = True

    # Alias matching the common SQL verb.
    rollback = commit


class PreparedStatement:
    """A statement text with one bare table reference and a fixed path."""

    def __init__(self, statement_text: str, table_name: str,
                 search_path: Tuple[str, ...]) -> None:
        self.statement_text = statement_text
        self.table_name = table_name
        self.search_path = tuple(search_path)
        self._last_key_path: Tuple[str, ...] = self.search_path

    def rebased(self, search_path: Tuple[str, ...]) -> "PreparedStatement":
        """Return a copy of the statement prepared under another path."""
        return PreparedStatement(
            self.statement_text, self.table_name, tuple(search_path)
        )


@dataclass(frozen=True)
class ExecutionResult:
    """What an execute/prepare-lookup returned, including path-change info."""

    result: BindResult
    search_path_changed: bool

    @property
    def hit(self) -> bool:
        return self.result.hit

    @property
    def rebound(self) -> bool:
        return self.result.rebound

    @property
    def reason(self) -> str:
        if self.search_path_changed:
            return f"{self.result.reason}+{SEARCH_PATH_CHANGED}"
        return self.result.reason

    @property
    def oid(self) -> int:
        return self.result.oid

    @property
    def binding(self):
        return self.result.binding

    @property
    def previous_binding(self):
        return self.result.previous_binding


class Session:
    """A database session: search path + prepared statements + transactions."""

    def __init__(self, catalog: Catalog, cache: Optional[BindingCache] = None,
                 search_path: Tuple[str, ...] = ()) -> None:
        self.catalog = catalog
        self.cache = cache if cache is not None else BindingCache()
        self._search_path = tuple(search_path)
        self._transaction: Optional[Transaction] = None
        self._prepared: List[PreparedStatement] = []

    # ------------------------------------------------------------- search path
    @property
    def search_path(self) -> Tuple[str, ...]:
        return self._search_path

    def set_search_path(self, *namespaces: str) -> None:
        path = tuple(namespaces)
        for namespace in path:
            if not self.catalog.has_namespace(namespace):
                raise NamespaceNotFound(
                    f"namespace {namespace!r} does not exist"
                )
        self._search_path = path

    # ------------------------------------------------------------ transaction
    def begin(self, snapshot_version: Optional[int] = None) -> Transaction:
        """Start a transaction pinned to the catalog version visible now."""
        if self._transaction is not None and not self._transaction.closed:
            raise RuntimeError("a transaction is already open")
        txn = Transaction(self.catalog, snapshot_version)
        self._transaction = txn
        return txn

    def commit(self) -> None:
        if self._transaction is None or self._transaction.closed:
            raise RuntimeError("no open transaction")
        self._transaction.commit()
        self._transaction = None

    @property
    def transaction(self) -> Optional[Transaction]:
        return self._transaction

    def _require_transaction(self) -> Transaction:
        if self._transaction is None or self._transaction.closed:
            raise RuntimeError("no open transaction; call begin() first")
        return self._transaction

    # ------------------------------------------------------- prepare / execute
    def prepare(self, statement_text: str, table_name: str,
                bind_now: bool = False) -> PreparedStatement:
        """Register a prepared statement.

        With ``bind_now=True`` the binding lookup runs immediately inside the
        current transaction (eager prepare); otherwise binding happens on the
        first :meth:`execute`.
        """
        stmt = PreparedStatement(
            statement_text, table_name, self._search_path
        )
        self._prepared.append(stmt)
        if bind_now:
            self.execute(stmt)
        return stmt

    def execute(self, stmt: PreparedStatement) -> ExecutionResult:
        txn = self._require_transaction()
        path = self._search_path
        path_changed = path != stmt._last_key_path  # noqa: SLF001
        result = self.cache.lookup(
            table_name=stmt.table_name,
            search_path=path,
            statement_text=stmt.statement_text,
            snapshot=txn.snapshot,
        )
        stmt._last_key_path = path  # noqa: SLF001
        return ExecutionResult(
            result=result,
            search_path_changed=path_changed,
        )
