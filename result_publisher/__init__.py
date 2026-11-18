"""Result publishing module.

Public surface:

* :class:`Query`, :class:`DataSnapshot`, :class:`PublishedResult`,
  :class:`CacheKey` -- value objects;
* :class:`ResultPublisher` -- the cached, quota-guarded, single-flight
  publication entry point;
* :class:`BudgetRegistry`, :class:`BudgetScope`, :class:`PrivacyBudget`,
  :class:`Reservation` -- privacy quota ledgers;
* :class:`NoiseAdapter`, :class:`LaplaceCountAdapter` -- local noise
  adapter contract and reference implementation;
* :class:`PublisherError`, :class:`QuotaExceededError`,
  :class:`UnknownScopeError`, :class:`PublicationError` -- exceptions;
* :func:`canonical_query_id` -- normalized query identifier helper.
"""

from .adapters import LaplaceCountAdapter, NoiseAdapter
from .budget import BudgetRegistry, BudgetScope, PrivacyBudget, Reservation
from .errors import (
    PublicationError,
    PublisherError,
    QuotaExceededError,
    UnknownScopeError,
)
from .identifiers import canonical_query_id
from .models import CacheKey, DataSnapshot, PublishedResult, Query
from .publisher import ResultPublisher

__all__ = [
    "LaplaceCountAdapter",
    "NoiseAdapter",
    "BudgetRegistry",
    "BudgetScope",
    "PrivacyBudget",
    "Reservation",
    "PublicationError",
    "PublisherError",
    "QuotaExceededError",
    "UnknownScopeError",
    "canonical_query_id",
    "CacheKey",
    "DataSnapshot",
    "PublishedResult",
    "Query",
    "ResultPublisher",
]
