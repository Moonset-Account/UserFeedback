"""Exact largest-remainder allocation and allocation-diff helpers."""

from __future__ import annotations

import math
from collections.abc import Hashable, Iterable, Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any

from .errors import (
    InvalidAmountError,
    InvalidWeightsError,
    NoAllocatableWeightError,
)

Number = int | Fraction | Decimal | float | str


def normalize_amount(value: Any) -> int:
    """Convert an integral charge amount to :class:`int`.

    The ledger allocates *integer units*.  Decimal values are accepted only when
    they are already integral; callers dealing with major currency units should
    convert to minor units first.
    """

    if isinstance(value, bool):
        raise InvalidAmountError("boolean is not a valid amount")

    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidAmountError("amount must be finite")
        if value != value.to_integral_value():
            raise InvalidAmountError("amount must be an integral number of units")
        result = int(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidAmountError("amount must be finite")
        if not value.is_integer():
            raise InvalidAmountError("amount must be an integral number of units")
        result = int(value)
    elif isinstance(value, str):
        try:
            decimal_value = Decimal(value.strip())
        except Exception as exc:  # InvalidOperation etc.
            raise InvalidAmountError(f"invalid amount: {value!r}") from exc
        return normalize_amount(decimal_value)
    else:
        raise InvalidAmountError(f"unsupported amount type: {type(value).__name__}")

    if result < 0:
        raise InvalidAmountError("amount must not be negative")
    return result


def weight_to_fraction(value: Any) -> Fraction:
    """Convert an ``int``/``Decimal``/string/``Fraction`` weight exactly."""

    if isinstance(value, bool):
        raise InvalidWeightsError("boolean is not a valid weight")
    if isinstance(value, Fraction):
        fraction = value
    elif isinstance(value, int):
        fraction = Fraction(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidWeightsError("weights must be finite")
        fraction = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidWeightsError("weights must be finite")
        # str(float) keeps the caller-visible decimal value such as 0.1.
        fraction = Fraction(Decimal(str(value)))
    elif isinstance(value, str):
        try:
            fraction = Fraction(Decimal(value.strip()))
        except Exception as exc:
            raise InvalidWeightsError(f"invalid weight: {value!r}") from exc
    else:
        raise InvalidWeightsError(f"unsupported weight type: {type(value).__name__}")

    if fraction < 0:
        raise InvalidWeightsError("weights must not be negative")
    return fraction


def validate_identifier(incident_id: Any) -> Hashable:
    if isinstance(incident_id, bool) or incident_id is None:
        raise InvalidWeightsError("incident id must be a non-empty string or integer")
    if not isinstance(incident_id, Hashable):
        raise InvalidWeightsError("incident id must be hashable")
    if isinstance(incident_id, str) and not incident_id.strip():
        raise InvalidWeightsError("incident id must not be empty")
    return incident_id


def normalize_weights(
    weights: Mapping[Any, Number] | Iterable[tuple[Any, Number]] | None,
) -> dict[Hashable, Fraction]:
    """Normalize weights to exact nonnegative :class:`Fraction` values.

    A mapping is used directly.  An iterable of ``(incident_id, weight)`` pairs
    may also be supplied; repeated incident ids are summed.
    """

    if weights is None:
        return {}

    if isinstance(weights, Mapping):
        raw_items: Iterable[tuple[Any, Any]] = weights.items()
    elif isinstance(weights, (str, bytes)):
        raise InvalidWeightsError("weights must be a mapping or iterable of pairs")
    elif isinstance(weights, Iterable):
        raw_items = weights
    else:
        raise InvalidWeightsError("weights must be a mapping or iterable of pairs")

    result: dict[Hashable, Fraction] = {}
    for index, item in enumerate(raw_items):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            incident_id, raw_weight = item
        else:
            # A plain sequence of weights is accepted as incident ids 0, 1, ...
            incident_id, raw_weight = index, item
        incident_id = validate_identifier(incident_id)
        weight = weight_to_fraction(raw_weight)
        result[incident_id] = result.get(incident_id, Fraction(0)) + weight

    return result


def sorted_identifiers(ids: Iterable[Hashable]) -> list[Hashable]:
    """Sort identifiers naturally when possible, otherwise by stable string key."""

    ids = list(ids)
    try:
        sorted(ids)
    except TypeError:
        return sorted(ids, key=lambda value: str(value))
    return sorted(ids)


def largest_remainder_allocation(
    total: Any,
    weights: Mapping[Any, Number] | Iterable[tuple[Any, Number]] | None,
) -> dict[Hashable, int]:
    """Allocate exactly ``total`` integer units by the Hamilton/largest-remainder rule.

    Floors are assigned first.  Remaining units go to the largest fractional
    parts of the exact quotas; equal fractional parts are broken by ascending
    incident id.
    """

    units = normalize_amount(total)
    normalized = normalize_weights(weights)

    if units == 0:
        return {incident_id: 0 for incident_id in normalized}

    positive_weights = {
        incident_id: weight
        for incident_id, weight in normalized.items()
        if weight > 0
    }
    if not positive_weights:
        raise NoAllocatableWeightError("at least one positive weight is required")

    weight_sum = sum(positive_weights.values(), Fraction(0))
    quotas = {
        incident_id: Fraction(units) * weight / weight_sum
        for incident_id, weight in positive_weights.items()
    }
    floors = {
        incident_id: quota.numerator // quota.denominator
        for incident_id, quota in quotas.items()
    }
    remainders = {
        incident_id: quotas[incident_id] - floors[incident_id]
        for incident_id in positive_weights
    }

    remaining_units = units - sum(floors.values())
    if not 0 <= remaining_units <= len(positive_weights):
        raise AssertionError("largest-remainder seat count is out of range")

    ordered = sorted_identifiers(positive_weights.keys())
    ordered.sort(key=lambda incident_id: (-remainders[incident_id], _tie_key(incident_id)))

    for incident_id in ordered[:remaining_units]:
        floors[incident_id] += 1

    return {
        incident_id: floors.get(incident_id, 0)
        for incident_id in sorted_identifiers(normalized.keys())
    }


def _tie_key(incident_id: Hashable) -> tuple[int, int, str]:
    # All components have fixed comparable types.  Homogeneous integer ids use
    # numeric order; strings use lexicographic order.  Mixed id families have a
    # deterministic family order rather than raising during a tie-break.
    if isinstance(incident_id, int) and not isinstance(incident_id, bool):
        return (0, incident_id, "")
    if isinstance(incident_id, str):
        return (1, 0, incident_id)
    return (2, 0, str(incident_id))


def allocate_with_pending(
    total: Any,
    weights: Mapping[Any, Number] | Iterable[tuple[Any, Number]] | None,
) -> tuple[dict[Hashable, int], int]:
    """Allocate by weights, or return all units as pending when unattributable."""

    units = normalize_amount(total)
    normalized = normalize_weights(weights)
    try:
        return largest_remainder_allocation(units, normalized), 0
    except NoAllocatableWeightError:
        return {incident_id: 0 for incident_id in normalized}, units


def diff_allocations(
    old: Mapping[Hashable, int] | None,
    new: Mapping[Hashable, int] | None,
) -> dict[Hashable, int]:
    """Return non-zero per-incident ``new - old`` differences."""

    old = old or {}
    new = new or {}
    keys = set(old) | set(new)
    diff: dict[Hashable, int] = {}
    for incident_id in sorted_identifiers(keys):
        old_value = normalize_amount(old.get(incident_id, 0))
        new_value = normalize_amount(new.get(incident_id, 0))
        delta = new_value - old_value
        if delta:
            diff[incident_id] = delta
    return diff


# Concise domain aliases.
allocate = largest_remainder_allocation
allocation_diff = diff_allocations
