"""Exact common-length packet planning for traffic-padding profiles.

The public entry point is :func:`plan_packets`.  See README.md for the full
semantic specification and runnable examples.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Any

__all__ = [
    "PaddingLimit",
    "SegmentPlan",
    "MaterialPlan",
    "PacketPlan",
    "PacketPlanner",
    "PlanningError",
    "InvalidInputError",
    "InfeasiblePlanError",
    "RecordDoesNotFitError",
    "PaddingBudgetExceeded",
    "plan_packets",
    "try_plan_packets",
]


class PlanningError(ValueError):
    """Base class for errors raised by the packet planner."""


class InvalidInputError(PlanningError):
    """Raised when the caller supplies malformed planning input."""


class InfeasiblePlanError(PlanningError):
    """Raised when no common length sequence satisfies the constraints."""


class RecordDoesNotFitError(InfeasiblePlanError):
    """Raised when a single record is larger than every allowed segment."""

    def __init__(
        self,
        message: str,
        *,
        material_id: Any = None,
        record_index: int | None = None,
        record_length: int | None = None,
        max_segment_length: int | None = None,
    ) -> None:
        super().__init__(message)
        self.material_id = material_id
        self.record_index = record_index
        self.record_length = record_length
        self.max_segment_length = max_segment_length


class PaddingBudgetExceeded(InfeasiblePlanError):
    """Raised when the minimum-padding plan exceeds the padding limit."""

    def __init__(self, message: str, *, plan: PacketPlan) -> None:
        super().__init__(message)
        self.plan = plan


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonnegative_int(value: Any, name: str) -> int:
    if not _is_int(value) or value < 0:
        raise InvalidInputError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if not _is_int(value) or value <= 0:
        raise InvalidInputError(f"{name} must be a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class PaddingLimit:
    """Padding overhead limits.

    Attributes:
        max_total: Maximum sum of padding bytes over all materials.
        max_per_material: Maximum padding bytes for any one material.
        max_overhead_ratio: Maximum ``total_padding / total_record_bytes``.
            When all records contain zero bytes, any positive padding violates
            a finite ratio.
        max_padding_fraction: Maximum
            ``total_padding / total_profile_bytes``.
    """

    max_total: int | None = None
    max_per_material: int | None = None
    max_overhead_ratio: float | None = None
    max_padding_fraction: float | None = None

    def __post_init__(self) -> None:
        if self.max_total is not None:
            _require_nonnegative_int(self.max_total, "max_total")
        if self.max_per_material is not None:
            _require_nonnegative_int(self.max_per_material, "max_per_material")

        def validate_ratio(value: Any, name: str) -> float:
            if isinstance(value, bool) or not isinstance(value, Real) or value < 0:
                raise InvalidInputError(
                    f"{name} must be a non-negative finite number, got {value!r}"
                )
            result = float(value)
            if result != result or result in (float("inf"), float("-inf")):
                raise InvalidInputError(f"{name} must be finite, got {value!r}")
            return result

        if self.max_overhead_ratio is not None:
            ratio = validate_ratio(self.max_overhead_ratio, "max_overhead_ratio")
            object.__setattr__(self, "max_overhead_ratio", ratio)
        if self.max_padding_fraction is not None:
            fraction = validate_ratio(self.max_padding_fraction, "max_padding_fraction")
            if fraction > 1:
                raise InvalidInputError(
                    "max_padding_fraction must be in [0, 1], got "
                    f"{self.max_padding_fraction!r}"
                )
            object.__setattr__(self, "max_padding_fraction", fraction)

    @classmethod
    def ratio(cls, value: float) -> PaddingLimit:
        """Limit padding bytes divided by original record bytes."""

        return cls(max_overhead_ratio=value)

    @classmethod
    def padding_share(cls, value: float) -> PaddingLimit:
        """Limit padding bytes divided by final profile bytes."""

        return cls(max_padding_fraction=value)

    @classmethod
    def total_bytes(cls, value: int) -> PaddingLimit:
        return cls(max_total=value)

    @classmethod
    def per_material_bytes(cls, value: int) -> PaddingLimit:
        return cls(max_per_material=value)


def _coerce_padding_limit(
    padding_limit: int | float | PaddingLimit | Mapping[str, Any] | None,
    *,
    per_material: int | None = None,
    overhead_ratio: float | None = None,
) -> PaddingLimit | None:
    limit: PaddingLimit | None
    if padding_limit is None:
        limit = None
    elif isinstance(padding_limit, PaddingLimit):
        limit = padding_limit
    elif isinstance(padding_limit, Mapping):
        known = {
            "max_total",
            "max_per_material",
            "max_overhead_ratio",
            "max_padding_fraction",
        }
        unknown = set(padding_limit) - known
        if unknown:
            raise InvalidInputError(f"unknown padding limit keys: {sorted(unknown)!r}")
        limit = PaddingLimit(
            max_total=padding_limit.get("max_total"),
            max_per_material=padding_limit.get("max_per_material"),
            max_overhead_ratio=padding_limit.get("max_overhead_ratio"),
            max_padding_fraction=padding_limit.get("max_padding_fraction"),
        )
    elif _is_int(padding_limit):
        limit = PaddingLimit(max_total=padding_limit)  # type: ignore[arg-type]
    elif isinstance(padding_limit, Real) and not isinstance(padding_limit, bool):
        limit = PaddingLimit(max_overhead_ratio=float(padding_limit))
    else:
        raise InvalidInputError(
            "padding_limit must be None, an int byte budget, a float overhead "
            f"ratio, PaddingLimit, or mapping; got {padding_limit!r}"
        )

    if per_material is not None or overhead_ratio is not None:
        extra = PaddingLimit(
            max_per_material=per_material,
            max_overhead_ratio=overhead_ratio,
        )
        if limit is None:
            limit = extra
        else:
            if extra.max_per_material is not None and limit.max_per_material is not None:
                raise InvalidInputError("duplicate max_per_material padding limit")
            if extra.max_overhead_ratio is not None and limit.max_overhead_ratio is not None:
                raise InvalidInputError("duplicate max_overhead_ratio padding limit")
            limit = PaddingLimit(
                max_total=limit.max_total,
                max_per_material=(
                    extra.max_per_material
                    if extra.max_per_material is not None
                    else limit.max_per_material
                ),
                max_overhead_ratio=(
                    extra.max_overhead_ratio
                    if extra.max_overhead_ratio is not None
                    else limit.max_overhead_ratio
                ),
                max_padding_fraction=limit.max_padding_fraction,
            )
    return limit


@dataclass(frozen=True)
class SegmentPlan:
    """One planned segment for one material."""

    index: int
    length: int
    record_indices: tuple[int, ...]
    record_lengths: tuple[int, ...]
    padding: int

    @property
    def is_full_padding(self) -> bool:
        return not self.record_indices

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "length": self.length,
            "record_indices": list(self.record_indices),
            "record_lengths": list(self.record_lengths),
            "padding": self.padding,
            "is_full_padding": self.is_full_padding,
        }


@dataclass(frozen=True)
class MaterialPlan:
    """The ordered segment assignment for one material."""

    material_id: Any
    record_lengths: tuple[int, ...]
    segments: tuple[SegmentPlan, ...]
    record_bytes: int
    padding: int

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "record_lengths": list(self.record_lengths),
            "record_bytes": self.record_bytes,
            "padding": self.padding,
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(frozen=True)
class PacketPlan:
    """An exact common-length profile and every material's arrangement."""

    common_lengths: tuple[int, ...]
    materials: tuple[MaterialPlan, ...]
    total_record_bytes: int
    total_profile_bytes: int
    total_padding: int
    padding_limit: PaddingLimit | None = None

    @property
    def segment_count(self) -> int:
        return len(self.common_lengths)

    @property
    def padding_by_material(self) -> tuple[int, ...]:
        return tuple(material.padding for material in self.materials)

    def material(self, material_id: Any) -> MaterialPlan:
        for material in self.materials:
            if material.material_id == material_id:
                return material
        raise KeyError(material_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "common_lengths": list(self.common_lengths),
            "segment_count": self.segment_count,
            "total_record_bytes": self.total_record_bytes,
            "total_profile_bytes": self.total_profile_bytes,
            "total_padding": self.total_padding,
            "padding_by_material": list(self.padding_by_material),
            "padding_limit": (
                {
                    "max_total": self.padding_limit.max_total,
                    "max_per_material": self.padding_limit.max_per_material,
                    "max_overhead_ratio": self.padding_limit.max_overhead_ratio,
                    "max_padding_fraction": self.padding_limit.max_padding_fraction,
                }
                if self.padding_limit is not None
                else None
            ),
            "materials": [material.to_dict() for material in self.materials],
        }


@dataclass(frozen=True)
class _Label:
    cost: int
    lengths: tuple[int, ...]


def _normalize_materials(
    record_lists: Sequence[Any] | Mapping[Any, Sequence[int]],
) -> list[tuple[Any, tuple[int, ...]]]:
    if isinstance(record_lists, Mapping):
        materials: list[tuple[Any, tuple[int, ...]]] = []
        for material_id, records in record_lists.items():
            materials.append((material_id, _normalize_record_tuple(records, material_id)))
        if not materials:
            raise InvalidInputError("record_lists must contain at least one material")
        return materials

    if isinstance(record_lists, (str, bytes)) or not isinstance(record_lists, Sequence):
        raise InvalidInputError(
            "record_lists must be a sequence of record-length lists, a flat "
            "record-length list, or a mapping; got "
            f"{type(record_lists).__name__}"
        )

    if len(record_lists) == 0:
        raise InvalidInputError(
            "record_lists must contain at least one material; pass [[]] for "
            "one material with no records"
        )

    # Convenience: a flat sequence of integers describes one material.
    if all(_is_int(value) for value in record_lists):
        records = _normalize_record_tuple(record_lists, 0)
        return [(0, records)]

    materials = []
    for index, records in enumerate(record_lists):
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise InvalidInputError(
                f"material at index {index} must be a sequence of integer lengths"
            )
        materials.append((index, _normalize_record_tuple(records, index)))

    if not materials:  # pragma: no cover - guarded above
        raise InvalidInputError("record_lists must contain at least one material")
    return materials


def _normalize_record_tuple(records: Sequence[Any], material_id: Any) -> tuple[int, ...]:
    normalized: list[int] = []
    for index, value in enumerate(records):
        if not _is_int(value):
            raise InvalidInputError(
                f"record length at material {material_id!r}, index {index} must "
                f"be an integer, got {value!r}"
            )
        if value < 0:
            raise InvalidInputError(
                f"record length at material {material_id!r}, index {index} must "
                f"be non-negative, got {value}"
            )
        normalized.append(value)
    return tuple(normalized)


def _normalize_allowed_lengths(allowed_lengths: Sequence[Any]) -> tuple[int, ...]:
    if isinstance(allowed_lengths, (str, bytes)) or not isinstance(
        allowed_lengths, Sequence
    ):
        raise InvalidInputError("allowed_lengths must be a sequence of positive integers")
    sizes: set[int] = set()
    for index, value in enumerate(allowed_lengths):
        _require_positive_int(value, f"allowed_lengths[{index}]")
        sizes.add(value)
    if not sizes:
        raise InvalidInputError("allowed_lengths must contain at least one length")
    return tuple(sorted(sizes))


def _label_sort_key(label: _Label) -> tuple[int, int, tuple[int, ...]]:
    return label.cost, len(label.lengths), label.lengths


def _same_depth_key(label: _Label) -> tuple[int, tuple[int, ...]]:
    return label.cost, label.lengths


def _optimal_common_lengths(
    materials: Sequence[tuple[Any, tuple[int, ...]]],
    sizes: tuple[int, ...],
    max_segments: int,
) -> tuple[int, ...] | None:
    """Find the minimum-total-length profile using a state-vector DP.

    A state stores how many records have been consumed in each material.  For a
    state and a chosen segment length, the canonical transition consumes the
    longest prefix that fits.  Consuming more records can never hurt a later
    fixed sequence of segment lengths: any records removed from the beginning
    of a feasible tail assignment only make that tail easier.
    """

    prefixes: list[list[int]] = []
    advances: list[list[list[int]]] = []

    for _, records in materials:
        prefix = [0]
        total = 0
        for length in records:
            total += length
            prefix.append(total)
        prefixes.append(prefix)

        material_advances: list[list[int]] = []
        for start in range(len(records) + 1):
            start_advances: list[int] = []
            target_base = prefix[start]
            for size in sizes:
                end = bisect_right(prefix, target_base + size) - 1
                # bisect_right can never move backwards for positive sizes.
                if end < start:  # pragma: no cover - defensive invariant
                    end = start
                start_advances.append(end)
            material_advances.append(start_advances)
        advances.append(material_advances)

    record_counts = tuple(len(records) for _, records in materials)
    zero_state = tuple(0 for _ in materials)
    goal_state = record_counts

    best: _Label | None = None
    if zero_state == goal_state:
        best = _Label(0, ())

    current: dict[tuple[int, ...], _Label] = {zero_state: _Label(0, ())}

    for _depth in range(1, max_segments + 1):
        next_states: dict[tuple[int, ...], _Label] = {}

        for state, label in current.items():
            # Every remaining allowed length is positive, so extending a label
            # whose cost is already not below ``best`` cannot improve it.
            if best is not None and label.cost >= best.cost:
                continue

            for size_index, size in enumerate(sizes):
                next_state = tuple(
                    advances[d][state[d]][size_index] for d in range(len(materials))
                )

                # A segment which consumes no record in any material is pure
                # padding for everybody.  Deleting it preserves feasibility and
                # lowers cost and segment count.
                if next_state == state:
                    continue

                candidate = _Label(
                    label.cost + size,
                    label.lengths + (size,),
                )

                old = next_states.get(next_state)
                if old is None or _same_depth_key(candidate) < _same_depth_key(old):
                    next_states[next_state] = candidate

                if next_state == goal_state and (
                    best is None or _label_sort_key(candidate) < _label_sort_key(best)
                ):
                    best = candidate

        current = next_states
        if not current:
            break

    return None if best is None else best.lengths


def _build_plan(
    materials: Sequence[tuple[Any, tuple[int, ...]]],
    common_lengths: tuple[int, ...],
    total_record_bytes: int,
    limit: PaddingLimit | None,
) -> PacketPlan:
    material_plans: list[MaterialPlan] = []
    padding_values: list[int] = []

    for material_id, records in materials:
        prefix = [0]
        total = 0
        for length in records:
            total += length
            prefix.append(total)

        segments: list[SegmentPlan] = []
        start = 0
        material_padding = 0

        for segment_index, segment_length in enumerate(common_lengths):
            end = bisect_right(prefix, prefix[start] + segment_length) - 1
            if end < start:  # pragma: no cover - defensive invariant
                end = start

            record_indices = tuple(range(start, end))
            record_lengths = records[start:end]
            used_bytes = prefix[end] - prefix[start]
            padding = segment_length - used_bytes
            if padding < 0:
                raise InfeasiblePlanError(
                    "internal error: record assignment exceeds common segment length"
                )

            segments.append(
                SegmentPlan(
                    index=segment_index,
                    length=segment_length,
                    record_indices=record_indices,
                    record_lengths=record_lengths,
                    padding=padding,
                )
            )
            material_padding += padding
            start = end

        if start != len(records):
            raise InfeasiblePlanError(
                f"common profile {common_lengths!r} is not feasible for material "
                f"{material_id!r}; unassigned records remain"
            )

        if material_padding != sum(common_lengths) - total:
            raise InfeasiblePlanError(
                "internal error: padding accounting does not match profile length"
            )

        padding_values.append(material_padding)
        material_plans.append(
            MaterialPlan(
                material_id=material_id,
                record_lengths=records,
                segments=tuple(segments),
                record_bytes=total,
                padding=material_padding,
            )
        )

    total_profile_bytes = len(materials) * sum(common_lengths)
    total_padding = sum(padding_values)
    if total_padding != total_profile_bytes - total_record_bytes:
        raise InfeasiblePlanError("internal error: aggregate padding accounting failed")

    plan = PacketPlan(
        common_lengths=common_lengths,
        materials=tuple(material_plans),
        total_record_bytes=total_record_bytes,
        total_profile_bytes=total_profile_bytes,
        total_padding=total_padding,
        padding_limit=limit,
    )
    _check_padding_limit(plan, limit)
    return plan


def _check_padding_limit(plan: PacketPlan, limit: PaddingLimit | None) -> None:
    if limit is None:
        return

    violations: list[str] = []
    if limit.max_total is not None and plan.total_padding > limit.max_total:
        violations.append(
            f"total padding {plan.total_padding} exceeds max_total "
            f"{limit.max_total}"
        )

    if limit.max_per_material is not None:
        for material in plan.materials:
            if material.padding > limit.max_per_material:
                violations.append(
                    f"material {material.material_id!r} padding "
                    f"{material.padding} exceeds max_per_material "
                    f"{limit.max_per_material}"
                )

    if limit.max_overhead_ratio is not None:
        denominator = plan.total_record_bytes
        if denominator == 0:
            if plan.total_padding != 0:
                violations.append(
                    "overhead ratio is undefined for zero record bytes and "
                    f"plan has {plan.total_padding} padding bytes"
                )
        elif plan.total_padding > limit.max_overhead_ratio * denominator:
            actual = plan.total_padding / denominator
            violations.append(
                f"total overhead ratio {actual:.6g} exceeds "
                f"{limit.max_overhead_ratio:.6g}"
            )

    if limit.max_padding_fraction is not None:
        profile_bytes = plan.total_profile_bytes
        if profile_bytes == 0:
            if plan.total_padding != 0:
                violations.append("padding fraction check encountered zero profile bytes")
        elif plan.total_padding > limit.max_padding_fraction * profile_bytes:
            actual_fraction = plan.total_padding / profile_bytes
            violations.append(
                f"total padding fraction {actual_fraction:.6g} exceeds "
                f"{limit.max_padding_fraction:.6g}"
            )

    if violations:
        raise PaddingBudgetExceeded("; ".join(violations), plan=plan)


def _oversized_record_error(
    materials: Sequence[tuple[Any, tuple[int, ...]]],
    max_allowed_length: int,
) -> RecordDoesNotFitError | None:
    for material_id, records in materials:
        for index, length in enumerate(records):
            if length > max_allowed_length:
                return RecordDoesNotFitError(
                    f"record {index} in material {material_id!r} has length "
                    f"{length}, larger than every allowed segment length "
                    f"(max {max_allowed_length})",
                    material_id=material_id,
                    record_index=index,
                    record_length=length,
                    max_segment_length=max_allowed_length,
                )
    return None


def plan_packets(
    record_lists: Sequence[Any] | Mapping[Any, Sequence[int]],
    allowed_lengths: Sequence[int],
    max_segments: int,
    padding_limit: int | float | PaddingLimit | Mapping[str, Any] | None = None,
    *,
    max_padding_per_material: int | None = None,
    max_overhead_ratio: float | None = None,
    allow_empty_profile: bool = True,
    raise_on_infeasible: bool = True,
) -> PacketPlan | None:
    """Plan a minimum-padding common segment-length sequence.

    Parameters are documented in README.md.  When ``raise_on_infeasible`` is
    false, :class:`InfeasiblePlanError` and :class:`InvalidInputError` are not
    swallowed: malformed input still raises.  Feasibility/budget failures then
    return ``None``.
    """

    materials = _normalize_materials(record_lists)
    sizes = _normalize_allowed_lengths(allowed_lengths)
    max_segments = _require_positive_int(max_segments, "max_segments")
    limit = _coerce_padding_limit(
        padding_limit,
        per_material=max_padding_per_material,
        overhead_ratio=max_overhead_ratio,
    )

    common_lengths = _optimal_common_lengths(materials, sizes, max_segments)

    if common_lengths is not None and not common_lengths and not allow_empty_profile:
        common_lengths = (sizes[0],)

    if common_lengths is None:
        oversized = _oversized_record_error(materials, sizes[-1])
        if oversized is not None:
            if raise_on_infeasible:
                raise oversized
            return None
        message = (
            f"no common sequence of at most {max_segments} allowed segments "
            f"{sizes!r} preserves record order and keeps every record intact"
        )
        error = InfeasiblePlanError(message)
        if raise_on_infeasible:
            raise error
        return None

    total_record_bytes = sum(length for _, records in materials for length in records)
    try:
        return _build_plan(materials, common_lengths, total_record_bytes, limit)
    except PaddingBudgetExceeded:
        if raise_on_infeasible:
            raise
        return None


def try_plan_packets(*args: Any, **kwargs: Any) -> PacketPlan | None:
    """Return the best plan, or ``None`` when no feasible/budgeted plan exists."""

    kwargs["raise_on_infeasible"] = False
    try:
        return plan_packets(*args, **kwargs)
    except InvalidInputError:
        raise


class PacketPlanner:
    """Stateful wrapper retaining channel constraints."""

    def __init__(
        self,
        allowed_lengths: Sequence[int],
        max_segments: int,
        padding_limit: int | float | PaddingLimit | Mapping[str, Any] | None = None,
        *,
        max_padding_per_material: int | None = None,
        max_overhead_ratio: float | None = None,
        allow_empty_profile: bool = True,
    ) -> None:
        self.allowed_lengths = _normalize_allowed_lengths(allowed_lengths)
        self.max_segments = _require_positive_int(max_segments, "max_segments")
        self.padding_limit = _coerce_padding_limit(
            padding_limit,
            per_material=max_padding_per_material,
            overhead_ratio=max_overhead_ratio,
        )
        self.allow_empty_profile = allow_empty_profile

    def plan(
        self,
        record_lists: Sequence[Any] | Mapping[Any, Sequence[int]],
        *,
        raise_on_infeasible: bool = True,
    ) -> PacketPlan | None:
        return plan_packets(
            record_lists,
            self.allowed_lengths,
            self.max_segments,
            self.padding_limit,
            allow_empty_profile=self.allow_empty_profile,
            raise_on_infeasible=raise_on_infeasible,
        )
