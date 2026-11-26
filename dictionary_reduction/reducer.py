"""Streaming dictionary-coded grouped reduction."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, Hashable, Iterable

from .dictionary import DictionarySnapshot, StableDictionary
from .domains import BINARY, ComparableDomain
from .errors import (
    CodeOutOfRangeError,
    DomainMismatchError,
    DomainValidationError,
    DuplicateGlobalRowError,
)
from .ordering import OrderDeclaration, validate_order_declaration
from .segments import DictionarySegment


@dataclass(frozen=True)
class SourceRef:
    """Identifies one source row."""

    row_id: int
    row_index: int
    segment_id: str | None = None
    shard_id: str | None = None


@dataclass
class GroupAggregate:
    """Aggregate state for one group."""

    group_code: int
    key: Hashable | None
    is_null: bool = False
    sum: int = 0
    count: int = 0
    first_row: int | None = None
    first_source: SourceRef | None = None
    first_value: Any = None
    first_output_code: int | None = None
    output_codes: set[int] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.sum

    @property
    def first(self) -> int | None:
        return self.first_row

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_code": self.group_code,
            "key": self.key,
            "is_null": self.is_null,
            "sum": self.sum,
            "count": self.count,
            "first_row": self.first_row,
            "first_source": None
            if self.first_source is None
            else {
                "row_id": self.first_source.row_id,
                "row_index": self.first_source.row_index,
                "segment_id": self.first_source.segment_id,
                "shard_id": self.first_source.shard_id,
            },
            "first_value": self.first_value,
            "first_output_code": self.first_output_code,
            "output_codes": sorted(self.output_codes),
        }


@dataclass(frozen=True)
class EncodedBatch:
    """A segment re-encoded against a stable output dictionary."""

    segment_id: str | None
    shard_id: str | None
    codes: tuple[int | None, ...]
    indices: tuple[int, ...]
    nulls: tuple[bool, ...]
    row_ids: tuple[int, ...]
    dictionary_snapshot: DictionarySnapshot

    def decode_codes(self) -> tuple[Any, ...]:
        return self.dictionary_snapshot.decode(self.indices)

    # Convenience alias.
    def decoded_values(self) -> tuple[Any, ...]:
        return self.decode_codes()


@dataclass(frozen=True)
class AggregateBatch:
    """Dictionary-coded partial/final group output.

    The numeric fields are copied at emission time.  Later segments may add
    groups, but they never mutate this batch or renumber its group codes.
    """

    group_codes: tuple[int, ...]
    keys: tuple[Hashable | None, ...]
    sums: tuple[int, ...]
    counts: tuple[int, ...]
    first_rows: tuple[int | None, ...]
    dictionary_snapshot: DictionarySnapshot

    def decoded_keys(self) -> tuple[Any, ...]:
        return self.dictionary_snapshot.decode(self.group_codes)

    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "group_code": code,
                "key": key,
                "sum": total,
                "count": count,
                "first_row": first_row,
            }
            for code, key, total, count, first_row in zip(
                self.group_codes,
                self.keys,
                self.sums,
                self.counts,
                self.first_rows,
            )
        )


# Vocabulary alias.
GroupBatch = AggregateBatch


@dataclass
class ReductionResult:
    """Final aggregates, stable dictionary snapshot, and emitted batches."""

    domain: ComparableDomain
    groups: list[GroupAggregate]
    by_key: dict[Hashable | None, GroupAggregate]
    dictionary: DictionarySnapshot
    output_dictionary: StableDictionary
    batches: tuple[EncodedBatch, ...]
    row_count: int
    strict_row_ids: bool

    def sums(self) -> dict[Hashable | None, int]:
        return {key: group.sum for key, group in self.by_key.items()}

    def first_rows(self) -> dict[Hashable | None, int | None]:
        return {key: group.first_row for key, group in self.by_key.items()}

    def tuples(self) -> dict[Hashable | None, tuple[int, int | None]]:
        return {
            key: (group.sum, group.first_row)
            for key, group in self.by_key.items()
        }

    def ordered_groups(
        self, ordering: OrderDeclaration | str | None = None
    ) -> list[GroupAggregate]:
        if ordering is None:
            ordering = OrderDeclaration.ascending()
        elif isinstance(ordering, str):
            ordering = OrderDeclaration(ordering)
        elif not isinstance(ordering, OrderDeclaration):
            raise TypeError("ordering must be an OrderDeclaration or string")

        if ordering.direction == "unordered":
            return list(self.groups)

        null_groups = [group for group in self.groups if group.is_null]
        value_groups = [group for group in self.groups if not group.is_null]

        domain = self.domain
        direction = ordering.multiplier

        def compare_groups(left: GroupAggregate, right: GroupAggregate) -> int:
            if left.first_value is None or right.first_value is None:
                # Defensive; null groups are handled separately.
                return (left.first_row or 0) - (right.first_row or 0)
            cmp_value = domain.compare(left.first_value, right.first_value)
            if cmp_value != 0:
                return cmp_value * direction
            if left.first_row != right.first_row:
                assert left.first_row is not None and right.first_row is not None
                return -1 if left.first_row < right.first_row else 1
            return left.group_code - right.group_code

        value_groups = sorted(value_groups, key=cmp_to_key(compare_groups))
        null_groups = sorted(
            null_groups,
            key=lambda g: (g.first_row is None, g.first_row, g.group_code),
        )

        if ordering.nulls == "first":
            return null_groups + value_groups
        return value_groups + null_groups

    def to_dict(self) -> dict[Hashable | None, dict[str, Any]]:
        return {key: group.to_dict() for key, group in self.by_key.items()}


class DictionaryReducer:
    """Incremental grouped sum/first-row reduction on dictionary codes."""

    def __init__(
        self,
        domain: ComparableDomain | None = None,
        *,
        output_dictionary: StableDictionary | None = None,
        selected_dictionary: Any = None,
        null_group: bool = True,
        strict_row_ids: bool = True,
        verify_declarations: bool = False,
    ) -> None:
        self.domain = domain or BINARY
        if output_dictionary is not None and selected_dictionary is not None:
            raise ValueError(
                "pass either output_dictionary or selected_dictionary, not both"
            )
        if output_dictionary is not None:
            self.output_dictionary = output_dictionary
        else:
            self.output_dictionary = StableDictionary(selected_dictionary)
        self.null_group = null_group
        self.strict_row_ids = strict_row_ids
        self.verify_declarations = verify_declarations

        self._groups: dict[tuple[Any, ...], GroupAggregate] = {}
        self._insertion_order: list[tuple[Any, ...]] = []
        self._row_owners: dict[int, SourceRef] = {}
        self._batches: list[EncodedBatch] = []
        self._row_count = 0

    def add_shard(self, shard: DictionarySegment | None = None, **kwargs: Any) -> EncodedBatch:
        """Add a segment, constructing one from keyword arguments if needed."""

        if shard is None:
            shard = DictionarySegment(**kwargs)
        elif kwargs:
            raise TypeError("do not pass keyword arguments with a segment object")
        return self.add_segment(shard)

    def reduce(
        self,
        segments: Iterable[DictionarySegment] | None = None,
        *,
        order: OrderDeclaration | str | None = None,
    ) -> ReductionResult:
        """Add zero or more segments and return the current result."""

        if segments is not None:
            for segment in segments:
                self.add_segment(segment)
        return self.result(order=order)

    __call__ = reduce

    def add_segment(self, segment: DictionarySegment) -> EncodedBatch:
        if not isinstance(segment, DictionarySegment):
            raise TypeError("add_segment expects a DictionarySegment")

        if segment.domain is not None and segment.domain != self.domain:
            raise DomainMismatchError(
                f"segment domain {segment.domain!r} does not match reducer "
                f"domain {self.domain!r}"
            )

        declaration = segment.ordering
        if declaration is not None and self.verify_declarations:
            # Decode only when an order declaration must be checked.  Normal
            # reduction resolves each distinct local code once below.
            decoded = segment.decoded_values(self.domain)
            validate_order_declaration(
                values=decoded,
                nulls=segment.nulls,
                declaration=declaration,
                domain=self.domain,
                row_ids=segment.row_ids,
            )

        # Resolve each local code at most once.  The binding is kept local to
        # this segment: the same integer code in another segment may have a
        # completely different value.
        bindings: dict[int, tuple[Any, int, tuple[Any, ...], Hashable | None]] = {}
        planned_rows: list[dict[str, Any]] = []
        seen_rows_in_segment: set[int] = set()

        for i in range(segment.row_count):
            row_id = segment.row_ids[i]
            ref = SourceRef(
                row_id=row_id,
                row_index=i,
                segment_id=segment.segment_id,
                shard_id=segment.shard_id,
            )

            if self.strict_row_ids:
                previous = self._row_owners.get(row_id)
                if previous is not None:
                    raise DuplicateGlobalRowError(
                        f"global row {row_id} was already consumed at {previous}; "
                        f"new occurrence is {ref}"
                    )
                if row_id in seen_rows_in_segment:
                    raise DuplicateGlobalRowError(
                        f"global row id {row_id} is duplicated in segment "
                        f"{segment.segment_id!r}"
                    )
                seen_rows_in_segment.add(row_id)

            is_null = segment.nulls[i]
            if is_null:
                if self.null_group:
                    internal_key = ("null", self.domain.identity)
                    exposed_key: Hashable | None = None
                else:
                    internal_key = ("skip-null", self.domain.identity, row_id, i)
                    exposed_key = None
                planned_rows.append(
                    {
                        "index": i,
                        "is_null": True,
                        "value": None,
                        "output_code": None,
                        "internal_key": internal_key,
                        "exposed_key": exposed_key,
                        "ref": ref,
                        "measure": segment.measures[i],
                    }
                )
                continue

            code = segment.codes[i]
            if code not in segment.dictionary:
                raise CodeOutOfRangeError(
                    f"segment {segment.segment_id!r} row {i} references missing "
                    f"local dictionary code {code}"
                )

            if code not in bindings:
                value = segment.dictionary[code]
                self.domain.check_value(value)
                group_key = self.domain.group_key(value)
                if group_key is None:
                    raise DomainValidationError(
                        "non-null values cannot use None as their group key; "
                        "None is reserved for the null group"
                    )
                internal_key = ("value", self.domain.identity, group_key)
                bindings[code] = (value, -1, internal_key, group_key)

            value, _placeholder, internal_key, group_key = bindings[code]
            planned_rows.append(
                {
                    "index": i,
                    "is_null": False,
                    "value": value,
                    # Filled after all validation so a failed segment does not
                    # append dictionary entries prematurely.
                    "output_code": None,
                    "internal_key": internal_key,
                    "exposed_key": group_key,
                    "ref": ref,
                    "measure": segment.measures[i],
                    "local_code": code,
                }
            )

        # Validation succeeded; now intern distinct physical values and fill
        # stable output codes in the local-code binding cache.
        for code, binding in list(bindings.items()):
            value, _, internal_key, group_key = binding
            output_code = self.output_dictionary.intern(value)
            bindings[code] = (value, output_code, internal_key, group_key)

        output_codes: list[int | None] = []
        indices: list[int] = []
        for planned in planned_rows:
            if planned["is_null"]:
                output_code = None
                output_codes.append(None)
                indices.append(-1)
            else:
                local_code = planned["local_code"]
                output_code = bindings[local_code][1]
                planned["output_code"] = output_code
                output_codes.append(output_code)
                indices.append(output_code)

        snapshot = self.output_dictionary.snapshot()
        batch = EncodedBatch(
            segment_id=segment.segment_id,
            shard_id=segment.shard_id,
            codes=tuple(output_codes),
            indices=tuple(indices),
            nulls=segment.nulls,
            row_ids=segment.row_ids,
            dictionary_snapshot=snapshot,
        )

        # Commit row-id ownership and aggregates only after all validation.
        if self.strict_row_ids:
            for planned in planned_rows:
                ref = planned["ref"]
                self._row_owners[ref.row_id] = ref

        for planned in planned_rows:
            if planned["is_null"] and not self.null_group:
                continue

            internal_key = planned["internal_key"]
            group = self._groups.get(internal_key)
            if group is None:
                group = GroupAggregate(
                    group_code=len(self._insertion_order),
                    key=planned["exposed_key"],
                    is_null=planned["is_null"],
                )
                self._groups[internal_key] = group
                self._insertion_order.append(internal_key)

            measure = planned["measure"]
            if measure is not None:
                group.sum += measure
            group.count += 1

            if not planned["is_null"]:
                output_code = planned["output_code"]
                assert output_code is not None
                group.output_codes.add(output_code)

            ref = planned["ref"]
            if group.first_row is None or ref.row_id < group.first_row:
                group.first_row = ref.row_id
                group.first_source = ref
                group.first_value = planned["value"]
                group.first_output_code = planned["output_code"]

        self._batches.append(batch)
        self._row_count += segment.row_count
        return batch

    def group_snapshot(self) -> DictionarySnapshot:
        """Return the immutable stable group-code dictionary."""

        entries = {
            self._groups[key].group_code: self._groups[key].key
            for key in self._insertion_order
        }
        return DictionarySnapshot.from_entries(
            entries,
            version=len(self._insertion_order),
            next_code=len(self._insertion_order),
        )

    def aggregate_batch(
        self, *, order: OrderDeclaration | str | None = None
    ) -> AggregateBatch:
        """Emit current aggregates as a stable dictionary-coded batch."""

        current = self.result(order=order)
        groups = current.groups
        snapshot = self.group_snapshot()
        return AggregateBatch(
            group_codes=tuple(group.group_code for group in groups),
            keys=tuple(group.key for group in groups),
            sums=tuple(group.sum for group in groups),
            counts=tuple(group.count for group in groups),
            first_rows=tuple(group.first_row for group in groups),
            dictionary_snapshot=snapshot,
        )

    def result(self, *, order: OrderDeclaration | str | None = None) -> ReductionResult:
        groups = [self._groups[key] for key in self._insertion_order]
        by_key: dict[Hashable | None, GroupAggregate] = {}
        for group in groups:
            # With one fixed domain, exposed keys are unique.  Null uses None.
            by_key[group.key] = group

        if order is not None:
            ordered = ReductionResult(
                domain=self.domain,
                groups=groups,
                by_key=by_key,
                dictionary=self.output_dictionary.snapshot(),
                output_dictionary=self.output_dictionary,
                batches=tuple(self._batches),
                row_count=self._row_count,
                strict_row_ids=self.strict_row_ids,
            ).ordered_groups(order)
            groups = ordered

        return ReductionResult(
            domain=self.domain,
            groups=groups,
            by_key=by_key,
            dictionary=self.output_dictionary.snapshot(),
            output_dictionary=self.output_dictionary,
            batches=tuple(self._batches),
            row_count=self._row_count,
            strict_row_ids=self.strict_row_ids,
        )


def reduce_segments(
    segments: list[DictionarySegment] | tuple[DictionarySegment, ...],
    *,
    domain: ComparableDomain | None = None,
    output_dictionary: StableDictionary | None = None,
    selected_dictionary: Any = None,
    null_group: bool = True,
    strict_row_ids: bool = True,
    verify_declarations: bool = False,
    order: OrderDeclaration | str | None = None,
) -> ReductionResult:
    """Reduce all segments in the supplied processing order."""

    reducer = DictionaryReducer(
        domain=domain,
        output_dictionary=output_dictionary,
        selected_dictionary=selected_dictionary,
        null_group=null_group,
        strict_row_ids=strict_row_ids,
        verify_declarations=verify_declarations,
    )
    for segment in segments:
        reducer.add_segment(segment)
    return reducer.result(order=order)


# Descriptive aliases for callers using "shards" vocabulary.
reduce_dictionary_shards = reduce_segments
reduce_shards = reduce_segments
dictionary_reduce = reduce_segments
reduce_codes = reduce_segments
group_by_dictionary = reduce_segments


def reduce_to_tuples(
    segments: list[DictionarySegment] | tuple[DictionarySegment, ...],
    **kwargs: Any,
) -> dict[Hashable | None, tuple[int, int | None]]:
    """Return ``{group_key: (integer_sum, first_global_row)}``."""

    return reduce_segments(segments, **kwargs).tuples()
