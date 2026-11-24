"""Plan binding cache with versioned-catalog snapshots.

Public API:

* :class:`Catalog`, :class:`CatalogSnapshot`, :class:`CatalogEvent`,
  :class:`Resolution`, :class:`RelationNotFound`, :class:`NamespaceNotFound`
* :class:`BindingCache`, :class:`Binding`, :class:`BindResult` and the reason
  constants (``HIT_*`` / ``REBIND_*`` / ``SEARCH_PATH_CHANGED``)
* :class:`Session`, :class:`Transaction`, :class:`PreparedStatement`,
  :class:`ExecutionResult`
"""

from .cache import (
    BIND_REASONS,
    HIT_FRESH,
    HIT_NO_RELEVANT_CHANGE,
    REBIND_DROPPED,
    REBIND_FIRST_USE,
    REBIND_RESOLUTION_CHANGED,
    REBIND_SHADOWED,
    SEARCH_PATH_CHANGED,
    BindResult,
    Binding,
    BindingCache,
)
from .catalog import (
    Catalog,
    CatalogEvent,
    CatalogSnapshot,
    NamespaceNotFound,
    RelationNotFound,
    Resolution,
)
from .session import (
    ExecutionResult,
    PreparedStatement,
    Session,
    Transaction,
)

__all__ = [
    "Catalog",
    "CatalogEvent",
    "CatalogSnapshot",
    "Resolution",
    "RelationNotFound",
    "NamespaceNotFound",
    "BindingCache",
    "Binding",
    "BindResult",
    "HIT_FRESH",
    "HIT_NO_RELEVANT_CHANGE",
    "REBIND_FIRST_USE",
    "REBIND_SHADOWED",
    "REBIND_DROPPED",
    "REBIND_RESOLUTION_CHANGED",
    "SEARCH_PATH_CHANGED",
    "BIND_REASONS",
    "Session",
    "Transaction",
    "PreparedStatement",
    "ExecutionResult",
]
