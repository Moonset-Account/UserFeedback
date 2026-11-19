"""Local certificate-revocation state store.

Public surface::

    from revocation_store import RevocationStore, Table, TableEntry, Reason

    with RevocationStore("revocations.sqlite3") as store:
        store.ingest(Table.full(...))
        store.lookup(issuer="Example CA", scope="root", serial="0A", at=now)
"""

from .jsonio import table_from_dict, table_to_dict, tables_from_json
from .model import (
    ConflictingTableError,
    Evidence,
    IngestResult,
    QueryResult,
    Reason,
    Status,
    Table,
    TableEntry,
    TableKind,
    TableValidationError,
)
from .store import RevocationStore

__all__ = [
    "RevocationStore",
    "Table",
    "TableEntry",
    "TableKind",
    "Reason",
    "Status",
    "QueryResult",
    "IngestResult",
    "Evidence",
    "TableValidationError",
    "ConflictingTableError",
    "table_from_dict",
    "table_to_dict",
    "tables_from_json",
]

__version__ = "1.0.0"
