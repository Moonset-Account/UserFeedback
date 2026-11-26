"""Comparable value domains and grouping collations.

A domain separates three concerns:

* validation of values;
* :meth:`ComparableDomain.group_key`, which decides whether two physical
  values belong to the same group;
* :meth:`ComparableDomain.sort_key` / :meth:`compare`, which decides their
  order under a collation.

The default domain is binary/exact: two strings or byte strings are equal only
when their values are exactly equal.  A case-insensitive domain demonstrates
another grouping rule for the same physical bytes.
"""

from __future__ import annotations

from typing import Any, Callable, Hashable

from .errors import DomainValidationError

KeyFunc = Callable[[Any], Hashable]
CompareFunc = Callable[[Any, Any], int]

_UNSET = object()


def _safe_call(func: Callable[..., Any], *args: Any) -> Any:
    try:
        return func(*args)
    except DomainValidationError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrapper
        raise DomainValidationError(
            f"domain key/comparison failed for {args!r}: {exc}"
        ) from exc


def case_insensitive_key(value: Any) -> Hashable:
    """Unicode case-fold strings and ASCII-lower byte strings."""

    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).lower()
    if isinstance(value, memoryview):
        return value.tobytes().lower()
    raise DomainValidationError(
        "case-insensitive domain expects str or bytes-like values, "
        f"got {type(value).__name__}"
    )


class ComparableDomain:
    """A comparable, optionally bounded value/collation domain.

    Parameters
    ----------
    name:
        Stable identity used to reject accidental mixing of domains.
    group_key:
        Maps a physical value to its equality/grouping identity.  It must be
        hashable and equal values must map to equal keys.
    sort_key:
        Maps a value to an ordered key.  Defaults to ``group_key``.
    comparator:
        Optional three-way comparator.  When supplied it takes precedence
        over ``sort_key``.
    lower, upper:
        Optional inclusive bounds for the comparable value range.
    allowed_types:
        Optional runtime type restriction.
    """

    def __init__(
        self,
        name: str = "binary",
        *,
        group_key: KeyFunc | None = None,
        sort_key: KeyFunc | None = None,
        comparator: CompareFunc | None = None,
        lower: Any = _UNSET,
        upper: Any = _UNSET,
        allowed_types: tuple[type, ...] | type | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("domain name must be a non-empty string")
        self.name = name
        self.identity: Hashable = name
        self._group_key = group_key
        self._sort_key = sort_key
        self._comparator = comparator
        self.lower = None if lower is _UNSET else lower
        self.upper = None if upper is _UNSET else upper
        if allowed_types is None:
            self.allowed_types: tuple[type, ...] | None = None
        elif isinstance(allowed_types, type):
            self.allowed_types = (allowed_types,)
        else:
            self.allowed_types = tuple(allowed_types)

    @classmethod
    def binary(cls) -> "ComparableDomain":
        """Exact string/bytes equality and natural lexicographic order."""

        return BINARY

    @classmethod
    def case_insensitive(cls) -> "ComparableDomain":
        """Case-insensitive equality/order for strings and byte strings."""

        return CASE_INSENSITIVE

    def contains(self, value: Any) -> bool:
        """Return whether *value* satisfies type and bound constraints."""

        if self.allowed_types is not None and not isinstance(
            value, self.allowed_types
        ):
            return False
        try:
            if self.lower is not None and self.compare(value, self.lower) < 0:
                return False
            if self.upper is not None and self.compare(value, self.upper) > 0:
                return False
        except Exception:
            return False
        return True

    def _check_type(self, value: Any) -> None:
        if self.allowed_types is not None and not isinstance(
            value, self.allowed_types
        ):
            raise DomainValidationError(
                f"value {value!r} has type {type(value).__name__}; "
                f"expected one of {self.allowed_types!r}"
            )

    def check_value(self, value: Any) -> None:
        self._check_type(value)
        if self.lower is not None:
            c = self.compare(value, self.lower)
            if c < 0:
                raise DomainValidationError(
                    f"value {value!r} is below lower bound {self.lower!r}"
                )
        if self.upper is not None:
            c = self.compare(value, self.upper)
            if c > 0:
                raise DomainValidationError(
                    f"value {value!r} is above upper bound {self.upper!r}"
                )

    def group_key(self, value: Any) -> Hashable:
        self.check_value(value)
        if self._group_key is None:
            try:
                hash(value)
            except TypeError as exc:
                raise DomainValidationError(
                    f"value {value!r} is not hashable"
                ) from exc
            return value
        key = _safe_call(self._group_key, value)
        try:
            hash(key)
        except TypeError as exc:
            raise DomainValidationError(
                f"group key {key!r} for value {value!r} is not hashable"
            ) from exc
        return key

    def sort_key(self, value: Any) -> Any:
        self.check_value(value)
        return self._sort_key_unchecked(value)

    def _sort_key_unchecked(self, value: Any) -> Any:
        if self._sort_key is not None:
            return _safe_call(self._sort_key, value)
        if self._group_key is not None:
            return _safe_call(self._group_key, value)
        return value

    def compare(self, left: Any, right: Any) -> int:
        if self._comparator is not None:
            result = _safe_call(self._comparator, left, right)
            try:
                return (result > 0) - (result < 0)
            except TypeError as exc:
                raise DomainValidationError(
                    f"comparator returned non-orderable result {result!r}"
                ) from exc

        left_key = self._sort_key_unchecked(left)
        right_key = self._sort_key_unchecked(right)
        try:
            if left_key == right_key:
                return 0
            return -1 if left_key < right_key else 1
        except TypeError as exc:
            raise DomainValidationError(
                "sort keys are not comparable: "
                f"{left_key!r} vs {right_key!r}"
            ) from exc

    def __repr__(self) -> str:
        return f"ComparableDomain(name={self.name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComparableDomain):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        return hash(self.identity)


class BinaryDomain(ComparableDomain):
    """Exact string/bytes equality and natural lexicographic order."""

    def __init__(self) -> None:
        super().__init__("binary")


class CaseInsensitiveDomain(ComparableDomain):
    """Case-insensitive equality/order for strings and byte strings."""

    def __init__(self, name: str = "case_insensitive") -> None:
        super().__init__(name, group_key=case_insensitive_key)


BINARY = BinaryDomain()
CASE_INSENSITIVE = CaseInsensitiveDomain()

# Vocabulary aliases for callers describing collations/value domains.
Collation = ComparableDomain
ValueDomain = ComparableDomain
ComparableValueDomain = ComparableDomain
