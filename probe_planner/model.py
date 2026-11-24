"""Domain objects and observation constants for probe placement."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Hashable, Iterable

# A site outage gives no receipt.  The monitoring caller still observes that
# the expected receipt is missing, so this is a first-class signature value.
MISSING = "missing"
FAILED = "failed"
OK = "ok"

VALID_STATUSES = frozenset({MISSING, FAILED, OK})
Number = int | float | Decimal


def _as_hashable_tuple(value: Any, *, field_name: str) -> tuple[Hashable, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)  # A string is one identifier, not a sequence of chars.
    if not hasattr(value, "__iter__"):
        items = (value,)
    else:
        try:
            items = tuple(value)
        except TypeError as exc:
            raise ValueError(
                f"{field_name} must be one identifier or an iterable"
            ) from exc
    for item in items:
        if not isinstance(item, Hashable):
            raise ValueError(f"{field_name} entries must be hashable: {item!r}")
    return items


def validate_cost(value: Any, *, field_name: str = "cost") -> Number:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field_name} must be a number, got {value!r}")
    if isinstance(value, float) and value in (float("inf"), float("-inf")):
        raise ValueError(f"{field_name} must be finite")
    if isinstance(value, float) and value != value:  # NaN
        raise ValueError(f"{field_name} must not be NaN")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value!r}")
    return value


@dataclass(frozen=True)
class Probe:
    """One synthetic request launched from ``site`` toward ``entry``.

    ``dependencies`` contains dependency/resource identifiers traversed by the
    request.  The target ``entry`` itself is also treated as a failed resource
    when matching a :class:`Fault` with ``failed_resources``.
    """

    site: Hashable
    entry: Hashable
    dependencies: tuple[Hashable, ...] = ()
    cost: Number | None = None
    id: Hashable | None = None

    def __post_init__(self) -> None:
        if self.site is None:
            raise ValueError("probe site must not be None")
        if self.entry is None:
            raise ValueError("probe entry must not be None")
        if not isinstance(self.site, Hashable):
            raise ValueError(f"probe site must be hashable: {self.site!r}")
        if not isinstance(self.entry, Hashable):
            raise ValueError(f"probe entry must be hashable: {self.entry!r}")
        deps = _as_hashable_tuple(self.dependencies, field_name="dependencies")
        object.__setattr__(self, "dependencies", deps)
        if self.cost is not None:
            validate_cost(self.cost, field_name="probe cost")

    @property
    def path_resources(self) -> frozenset[Hashable]:
        """Dependencies plus the target entry."""
        return frozenset(self.dependencies) | {self.entry}


@dataclass(frozen=True)
class Fault:
    """A single-fault scenario.

    ``unavailable_sites`` models patrol-site loss and produces
    :data:`MISSING` for that site's probes.  ``failed_resources`` models a
    failed dependency/service/entry and produces :data:`FAILED` for probes
    whose path or target contains one of those resources.
    """

    id: Hashable
    unavailable_sites: frozenset[Hashable] = field(default_factory=frozenset)
    failed_resources: frozenset[Hashable] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.id is None:
            raise ValueError("fault id must not be None")
        if not isinstance(self.id, Hashable):
            raise ValueError(f"fault id must be hashable: {self.id!r}")
        sites = frozenset(
            _as_hashable_tuple(self.unavailable_sites, field_name="unavailable_sites")
        )
        resources = frozenset(
            _as_hashable_tuple(self.failed_resources, field_name="failed_resources")
        )
        object.__setattr__(self, "unavailable_sites", sites)
        object.__setattr__(self, "failed_resources", resources)

    @classmethod
    def site_outage(cls, site: Hashable, id: Hashable | None = None) -> "Fault":
        if id is None:
            id = f"site:{site}"
        return cls(id=id, unavailable_sites=frozenset({site}))

    @classmethod
    def resource_failure(
        cls, resource: Hashable, id: Hashable | None = None
    ) -> "Fault":
        if id is None:
            id = f"resource:{resource}"
        return cls(id=id, failed_resources=frozenset({resource}))

    # Reliability/domain-language aliases.
    dependency_failure = resource_failure
    entry_failure = resource_failure
    business_failure = resource_failure
