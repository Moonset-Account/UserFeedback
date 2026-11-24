"""Dictionary-encoded equi-inner join for partitioned columnar tables.

The public objects are:

* :class:`Partition` -- one partition's dictionary, text codes and int keys.
* :class:`JoinPlan` -- prepared equivalence classes and ordered right groups.
* :class:`JoinCursor` / :class:`JoinBatch` -- resumable batch API.
* :func:`prepare_join`, :func:`join_all`, :func:`iter_join_batches`.
* :func:`find_undefined_code` and :class:`UndefinedCode` /
  :class:`UndefinedDictionaryCodeError`.

The module uses only the Python standard library.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Hashable, NamedTuple

__all__ = [
    "TextComparison",
    "Partition",
    "RowLocation",
    "Match",
    "DictionaryRef",
    "TextEquivalenceClass",
    "UndefinedCode",
    "JoinCursor",
    "JoinBatch",
    "JoinPlan",
    "InvalidPartitionError",
    "UndefinedDictionaryCodeError",
    "InvalidCursorError",
    "prepare_join",
    "try_prepare_join",
    "find_undefined_code",
    "join_all",
    "iter_join_batches",
    "join_batch",
    "inner_join",
    "equi_inner_join",
    "ColumnarEquiInnerJoin",
]


class TextComparison(str, Enum):
    """Supported text comparison rules."""

    EXACT = "exact"
    CASEFOLD = "casefold"

    # Descriptive aliases.  Enum aliases intentionally share values.
    CHARACTER = "exact"
    CODEPOINT = "exact"
    ORDINAL = "exact"
    UNICODE_CASEFOLD = "casefold"
    CASE_INSENSITIVE = "casefold"


_DEFAULT_CODE_NULL_VALUES: tuple[Any, ...] = (None, -1)
_DEFAULT_INTEGER_NULL_VALUES: tuple[Any, ...] = (None,)
_DEFAULT_BATCH_SIZE = 1000
_CURSOR_PREFIX = "columnar-join-cursor-v1."


class InvalidPartitionError(ValueError):
    """Raised when a partition does not have the required shape/types."""


class UndefinedDictionaryCodeError(ValueError):
    """Raised when a non-null code is absent from its partition dictionary."""

    def __init__(self, details: "UndefinedCode") -> None:
        self.details = details
        super().__init__(str(details))

    @property
    def side(self) -> str:
        return self.details.side

    @property
    def partition_index(self) -> int:
        return self.details.partition_index

    @property
    def row_index(self) -> int:
        return self.details.row_index

    @property
    def code(self) -> Any:
        return self.details.code


class InvalidCursorError(ValueError):
    """Raised when a cursor token is malformed or no longer usable."""


@dataclass(frozen=True)
class Partition:
    """One input partition.

    ``dictionary`` is either a sequence (codes are zero-based indexes) or a
    mapping from code to text.  ``codes`` and ``integer_keys`` must have the
    same length.
    """

    dictionary: Sequence[str] | Mapping[Hashable, str]
    codes: Sequence[Any]
    integer_keys: Sequence[Any]
    partition_id: Any = None


@dataclass(frozen=True)
class UndefinedCode:
    """Deterministic location of an undefined dictionary code."""

    side: str
    partition_index: int
    row_index: int
    code: Any
    partition_id: Any = None

    @property
    def partition(self) -> int:
        """Zero-based partition index."""
        return self.partition_index

    @property
    def row(self) -> int:
        """Zero-based row index."""
        return self.row_index

    @property
    def partition_number(self) -> int:
        """One-based partition number for human-facing messages."""
        return self.partition_index + 1

    @property
    def row_number(self) -> int:
        """One-based row number for human-facing messages."""
        return self.row_index + 1

    def as_tuple(self) -> tuple[str, int, int, Any]:
        return (self.side, self.partition_index, self.row_index, self.code)

    def __str__(self) -> str:
        partition_label = (
            f"partition_id={self.partition_id!r}, "
            if self.partition_id is not None
            else ""
        )
        return (
            f"undefined dictionary code {self.code!r} on {self.side} side: "
            f"{partition_label}partition_index={self.partition_index} "
            f"(partition #{self.partition_number}), "
            f"row_index={self.row_index} (row #{self.row_number})"
        )


class RowLocation(tuple):
    """Canonical row location.

    The tuple value is the zero-based coordinate
    ``(partition_index, row_index)``, so it compares equal to and unpacks like
    a plain coordinate pair.  Rich metadata is available through attributes.
    """

    def __new__(
        cls,
        side: str,
        partition_index: int,
        row_index: int,
        global_index: int,
        partition_id: Any = None,
    ) -> "RowLocation":
        obj = super().__new__(cls, (partition_index, row_index))
        for name, value in (
            ("side", side),
            ("partition_index", partition_index),
            ("row_index", row_index),
            ("global_index", global_index),
            ("partition_id", partition_id),
        ):
            object.__setattr__(obj, name, value)
        return obj

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("RowLocation is immutable")

    def __getnewargs__(self) -> tuple[str, int, int, int, Any]:
        return (
            self.side,
            self.partition_index,
            self.row_index,
            self.global_index,
            self.partition_id,
        )

    @property
    def partition(self) -> int:
        return self.partition_index

    @property
    def row(self) -> int:
        return self.row_index

    @property
    def coordinate(self) -> tuple[int, int]:
        """Zero-based ``(partition_index, row_index)`` coordinate."""
        return (self.partition_index, self.row_index)

    @property
    def partition_number(self) -> int:
        return self.partition_index + 1

    @property
    def row_number(self) -> int:
        return self.row_index + 1

    def __repr__(self) -> str:
        return (
            "RowLocation("
            f"side={self.side!r}, partition_index={self.partition_index}, "
            f"row_index={self.row_index}, global_index={self.global_index}, "
            f"partition_id={self.partition_id!r})"
        )


class Match(NamedTuple):
    """One inner-join match: ``(left_location, right_location)``."""

    left: RowLocation
    right: RowLocation

    @property
    def coordinates(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (self.left.coordinate, self.right.coordinate)

    @property
    def global_indices(self) -> tuple[int, int]:
        return (self.left.global_index, self.right.global_index)


@dataclass(frozen=True)
class DictionaryRef:
    """A dictionary entry's origin and equivalence class."""

    side: str
    partition_index: int
    code: Hashable
    value: str
    class_id: int
    partition_id: Any = None


@dataclass(frozen=True)
class TextEquivalenceClass:
    """A set of dictionary entries equivalent under the selected text rule."""

    class_id: int
    normalized_key: str
    entries: tuple[DictionaryRef, ...]

    @property
    def raw_values(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.value for entry in self.entries))

    @property
    def references(self) -> tuple[tuple[str, int, Hashable], ...]:
        return tuple((e.side, e.partition_index, e.code) for e in self.entries)


@dataclass(frozen=True)
class _FlatRow:
    location: RowLocation
    active: bool
    text_class: int | None
    integer_key: int | None


@dataclass(frozen=True)
class JoinCursor:
    """Immutable resume position.

    ``left_index`` is the next/current flattened left row.  When a batch ends
    inside one left row's right-hand fan-out, ``right_offset`` is the next
    right match for that left row.
    """

    batch_size: int
    left_index: int = 0
    right_offset: int = 0
    emitted_count: int = 0
    batch_index: int = 0
    done: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.batch_size, int) or isinstance(self.batch_size, bool):
            raise InvalidCursorError("batch_size must be a positive integer")
        if self.batch_size <= 0:
            raise InvalidCursorError("batch_size must be a positive integer")
        for name in ("left_index", "right_offset", "emitted_count", "batch_index"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidCursorError(f"{name} must be a non-negative integer")

    @property
    def offset(self) -> int:
        """Number of matches already emitted."""
        return self.emitted_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "left_index": self.left_index,
            "right_offset": self.right_offset,
            "emitted_count": self.emitted_count,
            "batch_index": self.batch_index,
            "done": self.done,
        }

    @property
    def token(self) -> str:
        payload = json.dumps(
            self.to_dict(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return _CURSOR_PREFIX + encoded

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JoinCursor":
        try:
            return cls(
                batch_size=data["batch_size"],
                left_index=data.get("left_index", 0),
                right_offset=data.get("right_offset", 0),
                emitted_count=data.get("emitted_count", 0),
                batch_index=data.get("batch_index", 0),
                done=bool(data.get("done", False)),
            )
        except (KeyError, TypeError, InvalidCursorError) as exc:
            raise InvalidCursorError(f"invalid cursor payload: {data!r}") from exc

    @classmethod
    def from_token(cls, token: str) -> "JoinCursor":
        if not isinstance(token, str) or not token.startswith(_CURSOR_PREFIX):
            raise InvalidCursorError("cursor token has an unsupported format")
        encoded = token[len(_CURSOR_PREFIX) :]
        padding = "=" * (-len(encoded) % 4)
        try:
            raw = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("cursor token is malformed") from exc
        if not isinstance(data, Mapping):
            raise InvalidCursorError("cursor token payload must be an object")
        return cls.from_dict(data)


@dataclass(frozen=True)
class JoinBatch:
    """One batch of matches and the cursor needed to fetch the next batch."""

    pairs: tuple[Match, ...]
    next_cursor: JoinCursor
    done: bool

    def __iter__(self) -> Iterator[Match]:
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, index: int) -> Match:
        return self.pairs[index]

    @property
    def cursor(self) -> JoinCursor:
        """Alias for ``next_cursor``."""
        return self.next_cursor


def _normalize_text_comparison(value: TextComparison | str) -> str:
    if isinstance(value, TextComparison):
        return value.value
    if not isinstance(value, str):
        raise InvalidPartitionError("text_comparison must be a string or TextComparison")
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "exact": TextComparison.EXACT.value,
        "character": TextComparison.EXACT.value,
        "codepoint": TextComparison.EXACT.value,
        "code_point": TextComparison.EXACT.value,
        "ordinal": TextComparison.EXACT.value,
        "casefold": TextComparison.CASEFOLD.value,
        "case_fold": TextComparison.CASEFOLD.value,
        "unicode_casefold": TextComparison.CASEFOLD.value,
        "unicode_case_fold": TextComparison.CASEFOLD.value,
        "case_insensitive": TextComparison.CASEFOLD.value,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise InvalidPartitionError(
            f"unsupported text_comparison {value!r}; expected 'exact' or 'casefold'"
        ) from exc


def _null_values(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        return (value,)
    return tuple(value)


def _is_null(value: Any, null_values: tuple[Any, ...]) -> bool:
    for candidate in null_values:
        if value is candidate:
            return True
        try:
            comparison = value == candidate
        except Exception:  # pragma: no cover - defensive for weird user objects
            continue
        if comparison is True:
            return True
    return False


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "dictionary": (
        "dictionary",
        "text_dictionary",
        "dict",
        "values",
        "text_values",
        "lexicon",
    ),
    "codes": ("codes", "row_codes", "text_codes", "encoded_values", "code_values"),
    "integer_keys": (
        "integer_keys",
        "int_keys",
        "second_keys",
        "second_key",
        "integers",
        "integer_values",
        "int_values",
        "keys2",
    ),
    "partition_id": ("partition_id", "id", "name", "partition_name"),
}


def _mapping_field(data: Mapping[str, Any], canonical: str) -> Any:
    for key in _FIELD_ALIASES[canonical]:
        if key in data:
            return data[key]
    raise InvalidPartitionError(
        f"partition mapping is missing {canonical!r} (accepted keys: "
        f"{', '.join(_FIELD_ALIASES[canonical])})"
    )


def _coerce_partition(value: Any, index: int, side: str) -> Partition:
    if isinstance(value, Partition):
        partition = value
    elif isinstance(value, Mapping):
        partition = Partition(
            dictionary=_mapping_field(value, "dictionary"),
            codes=_mapping_field(value, "codes"),
            integer_keys=_mapping_field(value, "integer_keys"),
            partition_id=_mapping_field(value, "partition_id")
            if any(k in value for k in _FIELD_ALIASES["partition_id"])
            else None,
        )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 3:
            dictionary, codes, integer_keys = value
            partition_id = None
        elif len(value) == 4:
            dictionary, codes, integer_keys, partition_id = value
        else:
            raise InvalidPartitionError(
                f"{side} partition #{index} tuple must have 3 or 4 elements: "
                "(dictionary, codes, integer_keys[, partition_id])"
            )
        partition = Partition(dictionary, codes, integer_keys, partition_id)
    else:
        raise InvalidPartitionError(
            f"{side} partition #{index} must be a Partition, mapping, or tuple"
        )

    dictionary = partition.dictionary
    if isinstance(dictionary, Mapping):
        pass
    elif isinstance(dictionary, (str, bytes)) or not isinstance(
        dictionary, Iterable
    ):
        raise InvalidPartitionError(
            f"{side} partition #{index} dictionary must be a sequence or mapping"
        )
    else:
        dictionary = tuple(dictionary)

    for field_name in ("codes", "integer_keys"):
        field_value = getattr(partition, field_name)
        if isinstance(field_value, (str, bytes)) or not isinstance(
            field_value, Iterable
        ):
            raise InvalidPartitionError(
                f"{side} partition #{index} {field_name} must be a sequence"
            )

    # Partition is frozen, so return a normalized copy rather than mutating it.
    codes = tuple(partition.codes)
    integer_keys = tuple(partition.integer_keys)
    if len(codes) != len(integer_keys):
        raise InvalidPartitionError(
            f"{side} partition #{index} has {len(codes)} codes but "
            f"{len(integer_keys)} integer keys; lengths must match"
        )

    return Partition(
        dictionary=dictionary,
        codes=codes,
        integer_keys=integer_keys,
        partition_id=partition.partition_id,
    )


def _looks_like_partition_tuple(value: tuple[Any, ...]) -> bool:
    if len(value) not in (3, 4):
        return False
    dictionary, codes, integer_keys = value[0], value[1], value[2]
    if isinstance(codes, (str, bytes)) or not isinstance(codes, Iterable):
        return False
    if isinstance(integer_keys, (str, bytes)) or not isinstance(
        integer_keys, Iterable
    ):
        return False
    if isinstance(dictionary, Mapping):
        return True
    return not isinstance(dictionary, (str, bytes)) and isinstance(
        dictionary, Iterable
    )


def _is_partition_mapping(value: Mapping[Any, Any]) -> bool:
    has_dictionary = any(key in value for key in _FIELD_ALIASES["dictionary"])
    has_codes = any(key in value for key in _FIELD_ALIASES["codes"])
    has_integer_keys = any(
        key in value for key in _FIELD_ALIASES["integer_keys"]
    )
    return has_dictionary and has_codes and has_integer_keys


def _coerce_partitions(
    partitions: Any, side: str
) -> tuple[Partition, ...]:
    if isinstance(partitions, Partition):
        items: Iterable[Any] = (partitions,)
    elif isinstance(partitions, Mapping):
        if _is_partition_mapping(partitions):
            # A bare mapping containing the partition fields is one partition.
            items = (partitions,)
        else:
            # Also accept {partition_id: partition_like}.
            normalized = []
            for index, (partition_id, value) in enumerate(partitions.items()):
                partition = _coerce_partition(value, index, side)
                if partition.partition_id is None:
                    partition = replace(partition, partition_id=partition_id)
                normalized.append(partition)
            return tuple(normalized)
    elif isinstance(partitions, tuple) and _looks_like_partition_tuple(partitions):
        items = (partitions,)
    elif isinstance(partitions, (str, bytes)) or not isinstance(partitions, Iterable):
        raise InvalidPartitionError(f"{side} input must be an iterable of partitions")
    else:
        items = partitions
    return tuple(_coerce_partition(item, index, side) for index, item in enumerate(items))


class JoinPlan:
    """Prepared dictionary-equivalence join.

    Parameters
    ----------
    left, right:
        Partition lists, :class:`Partition` objects, mappings, or tuples.
    text_comparison:
        ``"exact"`` (default; code-point/character equality) or
        ``"casefold"`` / ``TextComparison.UNICODE_CASEFOLD``.
    code_null_values:
        Raw text-code values treated as SQL-style null.  By default both
        ``None`` and the conventional dictionary sentinel ``-1`` are null.
        Pass ``(None,)`` if ``-1`` must instead be reported as undefined.
    integer_null_values:
        Raw integer-key values treated as null.  Defaults to ``(None,)``;
        integer ``0`` and ``-1`` are ordinary values unless configured here.
    """

    def __init__(
        self,
        left: Any,
        right: Any,
        *,
        text_comparison: TextComparison | str = TextComparison.EXACT,
        code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
        integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
    ) -> None:
        self.text_comparison = _normalize_text_comparison(text_comparison)
        self.code_null_values = _null_values(code_null_values)
        self.integer_null_values = _null_values(integer_null_values)

        self.left_partitions = _coerce_partitions(left, "left")
        self.right_partitions = _coerce_partitions(right, "right")

        self.text_classes, self._class_by_ref = self._build_text_classes()
        self.left_rows = self._flatten_side("left", self.left_partitions)
        self.right_rows = self._flatten_side("right", self.right_partitions)

        self.right_groups: dict[tuple[int, int], list[int]] = {}
        for row in self.right_rows:
            if row.active:
                assert row.text_class is not None
                assert row.integer_key is not None
                key = (row.text_class, row.integer_key)
                self.right_groups.setdefault(key, []).append(
                    row.location.global_index
                )

        self.total_matches = 0
        for row in self.left_rows:
            if row.active:
                assert row.text_class is not None
                assert row.integer_key is not None
                self.total_matches += len(
                    self.right_groups.get(
                        (row.text_class, row.integer_key), ()
                    )
                )

    # ------------------------------------------------------------------
    # Dictionary equivalence
    # ------------------------------------------------------------------
    def _normalize_text(self, value: Any, side: str, partition_index: int, code: Any) -> str:
        if not isinstance(value, str):
            raise InvalidPartitionError(
                f"{side} partition #{partition_index} dictionary entry for code "
                f"{code!r} is {type(value).__name__}, not str"
            )
        if self.text_comparison == TextComparison.CASEFOLD.value:
            return value.casefold()
        return value

    def _iter_dictionary(self, partition: Partition) -> Iterator[tuple[Hashable, str]]:
        dictionary = partition.dictionary
        if isinstance(dictionary, Mapping):
            for code, value in dictionary.items():
                yield code, value
        else:
            for code, value in enumerate(dictionary):
                yield code, value

    def _build_text_classes(
        self,
    ) -> tuple[tuple[TextEquivalenceClass, ...], dict[tuple[str, int, Hashable], int]]:
        normalized_to_id: dict[str, int] = {}
        class_entries: dict[int, list[DictionaryRef]] = {}
        class_keys: dict[int, str] = {}
        by_ref: dict[tuple[str, int, Hashable], int] = {}
        next_id = 0

        for side, partitions in (
            ("left", self.left_partitions),
            ("right", self.right_partitions),
        ):
            for partition_index, partition in enumerate(partitions):
                for code, raw_value in self._iter_dictionary(partition):
                    normalized = self._normalize_text(
                        raw_value, side, partition_index, code
                    )
                    class_id = normalized_to_id.get(normalized)
                    if class_id is None:
                        class_id = next_id
                        next_id += 1
                        normalized_to_id[normalized] = class_id
                        class_keys[class_id] = normalized
                        class_entries[class_id] = []

                    ref = DictionaryRef(
                        side=side,
                        partition_index=partition_index,
                        code=code,
                        value=raw_value,
                        class_id=class_id,
                        partition_id=partition.partition_id,
                    )
                    class_entries[class_id].append(ref)
                    by_ref[(side, partition_index, code)] = class_id

        classes = tuple(
            TextEquivalenceClass(
                class_id=class_id,
                normalized_key=class_keys[class_id],
                entries=tuple(class_entries[class_id]),
            )
            for class_id in range(next_id)
        )
        return classes, by_ref

    # ------------------------------------------------------------------
    # Row validation/flattening
    # ------------------------------------------------------------------
    def _dictionary_class_for_code(
        self,
        side: str,
        partition: Partition,
        partition_index: int,
        row_index: int,
        code: Any,
    ) -> int:
        # Sequence dictionaries use strict zero-based indexes.  In particular,
        # Python's negative indexing is not accepted here unless the caller
        # explicitly configured that value as a null sentinel.
        dictionary = partition.dictionary
        defined = False
        if isinstance(dictionary, Mapping):
            try:
                defined = code in dictionary
            except TypeError:
                defined = False
        elif isinstance(code, int) and not isinstance(code, bool):
            defined = 0 <= code < len(dictionary)

        if not defined:
            details = UndefinedCode(
                side=side,
                partition_index=partition_index,
                row_index=row_index,
                code=code,
                partition_id=partition.partition_id,
            )
            raise UndefinedDictionaryCodeError(details)

        try:
            return self._class_by_ref[(side, partition_index, code)]
        except (KeyError, TypeError) as exc:
            # A sequence can match after bool/index rules above; a mapping may
            # contain a key whose hash lookup behaves unusually.
            details = UndefinedCode(
                side=side,
                partition_index=partition_index,
                row_index=row_index,
                code=code,
                partition_id=partition.partition_id,
            )
            raise UndefinedDictionaryCodeError(details) from exc

    def _flatten_side(
        self, side: str, partitions: tuple[Partition, ...]
    ) -> tuple[_FlatRow, ...]:
        rows: list[_FlatRow] = []
        global_index = 0

        for partition_index, partition in enumerate(partitions):
            if len(partition.codes) != len(partition.integer_keys):
                raise InvalidPartitionError(
                    f"{side} partition #{partition_index}: codes and integer_keys "
                    "must have equal lengths"
                )

            for row_index, (raw_code, raw_integer) in enumerate(
                zip(partition.codes, partition.integer_keys)
            ):
                code_is_null = _is_null(raw_code, self.code_null_values)
                integer_is_null = _is_null(
                    raw_integer, self.integer_null_values
                )

                text_class: int | None = None
                if not code_is_null:
                    # Validate encoding even when the other key is null: an
                    # undefined code is malformed input, while null only removes
                    # the row from matching output.
                    text_class = self._dictionary_class_for_code(
                        side,
                        partition,
                        partition_index,
                        row_index,
                        raw_code,
                    )

                if not integer_is_null and not isinstance(raw_integer, int):
                    raise InvalidPartitionError(
                        f"{side} partition #{partition_index} row #{row_index}: "
                        "integer key must be an int or configured null; got "
                        f"{type(raw_integer).__name__}"
                    )

                location = RowLocation(
                    side=side,
                    partition_index=partition_index,
                    row_index=row_index,
                    global_index=global_index,
                    partition_id=partition.partition_id,
                )

                if code_is_null or integer_is_null:
                    rows.append(_FlatRow(location, False, None, None))
                else:
                    rows.append(
                        _FlatRow(location, True, text_class, raw_integer)
                    )
                global_index += 1

        return tuple(rows)

    # ------------------------------------------------------------------
    # Cursor and batches
    # ------------------------------------------------------------------
    def cursor(self, batch_size: int = _DEFAULT_BATCH_SIZE) -> JoinCursor:
        """Create an immutable initial cursor with the requested batch size."""
        return JoinCursor(batch_size=batch_size)

    def _coerce_fetch_cursor(
        self, cursor: JoinCursor | str | None, batch_size: int | None
    ) -> JoinCursor:
        if cursor is None:
            effective_size = (
                batch_size if batch_size is not None else _DEFAULT_BATCH_SIZE
            )
            current = JoinCursor(batch_size=effective_size)
        elif isinstance(cursor, str):
            current = JoinCursor.from_token(cursor)
        elif isinstance(cursor, JoinCursor):
            current = cursor
        else:
            raise TypeError(
                "cursor must be a JoinCursor, token string, or None; got "
                f"{type(cursor).__name__}"
            )

        if batch_size is not None and batch_size != current.batch_size:
            if not isinstance(batch_size, int) or isinstance(batch_size, bool):
                raise ValueError("batch_size must be a positive integer")
            if batch_size <= 0:
                raise ValueError("batch_size must be a positive integer")
            current = replace(current, batch_size=batch_size)
        return current

    def fetch_batch(
        self,
        cursor: JoinCursor | str | None = None,
        *,
        batch_size: int | None = None,
    ) -> JoinBatch:
        """Return the next batch starting after ``cursor``.

        Pass the previous batch's :attr:`JoinBatch.next_cursor` (or its
        ``.token`` string) to resume.  Cursors are immutable, so the same
        cursor can safely be used by independent/interleaved readers.
        """
        current = self._coerce_fetch_cursor(cursor, batch_size)
        if current.done:
            return JoinBatch(pairs=(), next_cursor=current, done=True)

        pairs: list[Match] = []
        left_index = current.left_index
        right_offset = current.right_offset
        emitted_count = current.emitted_count

        while left_index < len(self.left_rows):
            left_row = self.left_rows[left_index]

            if left_row.active:
                assert left_row.text_class is not None
                assert left_row.integer_key is not None
                right_ids = self.right_groups.get(
                    (left_row.text_class, left_row.integer_key), ()
                )

                while right_offset < len(right_ids):
                    right_global_index = right_ids[right_offset]
                    right_row = self.right_rows[right_global_index]
                    pairs.append(Match(left_row.location, right_row.location))
                    right_offset += 1
                    emitted_count += 1

                    if len(pairs) == current.batch_size:
                        done = emitted_count >= self.total_matches
                        next_cursor = JoinCursor(
                            batch_size=current.batch_size,
                            left_index=left_index,
                            right_offset=right_offset,
                            emitted_count=emitted_count,
                            batch_index=current.batch_index + 1,
                            done=done,
                        )
                        return JoinBatch(
                            pairs=tuple(pairs),
                            next_cursor=next_cursor,
                            done=done,
                        )

            left_index += 1
            right_offset = 0

        next_cursor = JoinCursor(
            batch_size=current.batch_size,
            left_index=left_index,
            right_offset=0,
            emitted_count=emitted_count,
            batch_index=current.batch_index + 1,
            done=True,
        )
        return JoinBatch(pairs=tuple(pairs), next_cursor=next_cursor, done=True)

    def iter_batches(
        self, batch_size: int = _DEFAULT_BATCH_SIZE
    ) -> Iterator[JoinBatch]:
        """Yield every batch, advancing an internal cursor automatically."""
        cursor = self.cursor(batch_size)
        while True:
            batch = self.fetch_batch(cursor)
            yield batch
            cursor = batch.next_cursor
            if batch.done:
                return

    def fetch_all(self) -> tuple[Match, ...]:
        """Return all matches in canonical left/right order."""
        collected: list[Match] = []
        for batch in self.iter_batches(_DEFAULT_BATCH_SIZE):
            collected.extend(batch.pairs)
        return tuple(collected)


def prepare_join(
    left: Any,
    right: Any,
    *,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> JoinPlan:
    """Prepare a :class:`JoinPlan` from partitioned inputs."""
    return JoinPlan(
        left,
        right,
        text_comparison=text_comparison,
        code_null_values=code_null_values,
        integer_null_values=integer_null_values,
    )


def try_prepare_join(
    left: Any,
    right: Any,
    *,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> tuple[JoinPlan | None, UndefinedCode | None]:
    """Prepare a plan, returning ``(None, report)`` for an undefined code.

    Partition shape/type errors still raise :class:`InvalidPartitionError`.
    """
    try:
        return (
            prepare_join(
                left,
                right,
                text_comparison=text_comparison,
                code_null_values=code_null_values,
                integer_null_values=integer_null_values,
            ),
            None,
        )
    except UndefinedDictionaryCodeError as exc:
        return None, exc.details


def find_undefined_code(
    left: Any,
    right: Any,
    *,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> UndefinedCode | None:
    """Return the first undefined-code location, or ``None`` if all are defined.

    Locations are scanned left side first and then right side, each in
    partition order and row order.  Shape/type problems still raise
    :class:`InvalidPartitionError`.
    """
    _plan, report = try_prepare_join(
        left,
        right,
        text_comparison=text_comparison,
        code_null_values=code_null_values,
        integer_null_values=integer_null_values,
    )
    return report


def join_all(
    left: Any,
    right: Any,
    *,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> tuple[Match, ...]:
    """Convenience wrapper returning every match."""
    return prepare_join(
        left,
        right,
        text_comparison=text_comparison,
        code_null_values=code_null_values,
        integer_null_values=integer_null_values,
    ).fetch_all()


def iter_join_batches(
    left: Any,
    right: Any,
    batch_size: int,
    *,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> Iterator[JoinBatch]:
    """Prepare a join and yield batches of ``batch_size`` matches."""
    yield from prepare_join(
        left,
        right,
        text_comparison=text_comparison,
        code_null_values=code_null_values,
        integer_null_values=integer_null_values,
    ).iter_batches(batch_size)


def join_batch(
    left: Any,
    right: Any,
    cursor: JoinCursor | str | None = None,
    *,
    batch_size: int | None = None,
    text_comparison: TextComparison | str = TextComparison.EXACT,
    code_null_values: Any = _DEFAULT_CODE_NULL_VALUES,
    integer_null_values: Any = _DEFAULT_INTEGER_NULL_VALUES,
) -> JoinBatch:
    """Functional one-call wrapper around :meth:`JoinPlan.fetch_batch`.

    The inputs must remain the same when resuming from a token because this
    wrapper reprepares the plan on every call.  Reusing a :class:`JoinPlan`
    is slightly more efficient.
    """
    plan = prepare_join(
        left,
        right,
        text_comparison=text_comparison,
        code_null_values=code_null_values,
        integer_null_values=integer_null_values,
    )
    return plan.fetch_batch(cursor, batch_size=batch_size)


# Descriptive aliases for callers/tests using the problem's terminology.
inner_join = join_all
equi_inner_join = join_all
ColumnarEquiInnerJoin = JoinPlan
