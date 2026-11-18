"""Core data models for the result publishing module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .identifiers import canonical_query_id


@dataclass(frozen=True)
class Query:
    """A query with a normalized identity.

    Two queries describe the same semantics when their
    :attr:`canonical_id` values are equal: the dataset, approved purpose,
    metric kind and normalized filters all match.

    ``filters`` / ``extra`` must be JSON-serializable after normalization
    (mappings, lists, tuples, sets and primitive values are supported).
    """

    dataset: str
    purpose: str
    kind: str
    filters: Any = None
    extra: Any = None
    canonical_id: str = field(init=False, repr=True)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_id",
            canonical_query_id(
                dataset=self.dataset,
                purpose=self.purpose,
                kind=self.kind,
                filters=self.filters,
                extra=self.extra,
            ),
        )


@dataclass(frozen=True)
class DataSnapshot:
    """An immutable point-in-time view of a dataset.

    A new snapshot id invalidates previously cached publications: the
    same query against a new snapshot consumes quota again and produces a
    new result. The optional ``payload`` holds the rows/aggregate input
    handed to the local noise adapter.
    """

    dataset: str
    snapshot_id: str
    payload: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, str) or not self.dataset:
            raise ValueError("dataset must be a non-empty string")
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")


@dataclass(frozen=True)
class CacheKey:
    """The cache identity of a publication.

    A cached result is shared only within the same snapshot *and* the same
    authorization scope (dataset + approved purpose) and the same query.
    """

    dataset: str
    purpose: str
    snapshot_id: str
    query_id: str

    @classmethod
    def from_parts(cls, query: Query, snapshot: DataSnapshot) -> "CacheKey":
        if snapshot.dataset != query.dataset:
            raise ValueError(
                "snapshot dataset does not match query dataset: "
                f"{snapshot.dataset!r} != {query.dataset!r}"
            )
        return cls(
            dataset=query.dataset,
            purpose=query.purpose,
            snapshot_id=snapshot.snapshot_id,
            query_id=query.canonical_id,
        )


@dataclass(frozen=True)
class PublishedResult:
    """A published, cached query result.

    The same object is returned on every cache hit, so callers can use
    identity (``is``) checks in tests to prove that two requests were
    served by a single publication.
    """

    key: CacheKey
    value: Any
    cost: float
    attempts: int
