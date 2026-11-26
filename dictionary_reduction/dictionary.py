"""Stable output dictionaries and immutable snapshots.

The dictionary is append-only.  Interning an already-seen value reuses its
code; a new value receives a fresh code larger than every previously assigned
code.  Consequently adding values never moves or renumbers an existing code,
which is the stability property required by already emitted batches.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import operator
from types import MappingProxyType
from typing import Any, Callable, Hashable, Iterable

from .errors import CodeCollisionError, CodeOutOfRangeError, DictionaryStructureError


def code_mapping(dictionary: Any) -> dict[int, Any]:
    """Normalize a sequence/mapping/local dictionary to ``{code: value}``."""

    if dictionary is None:
        return {}

    if isinstance(dictionary, Mapping):
        raw_items = dictionary.items()
    elif hasattr(dictionary, "items") and callable(dictionary.items):
        # StableDictionary and DictionarySnapshot expose items() as a method
        # returning a tuple of (code, value) pairs rather than a Mapping.
        raw_items = dictionary.items()
    else:
        raw_items = None

    if raw_items is not None:
        result: dict[int, Any] = {}
        try:
            iterator = iter(raw_items)
        except TypeError:
            iterator = iter(raw_items())
        for item in iterator:
            raw_code, value = item
            try:
                code = operator.index(raw_code)
            except TypeError as exc:
                raise DictionaryStructureError(
                    f"dictionary code must be an integer, got {raw_code!r}"
                ) from exc
            if isinstance(raw_code, bool):
                raise DictionaryStructureError("boolean is not a dictionary code")
            result[code] = value
        return result

    if isinstance(dictionary, (str, bytes, bytearray, memoryview)):
        raise DictionaryStructureError(
            "a string/bytes value is not itself a dictionary; pass a sequence "
            "of dictionary values or a {code: value} mapping"
        )

    try:
        return {code: value for code, value in enumerate(dictionary)}
    except TypeError as exc:
        raise DictionaryStructureError(
            "dictionary must be a sequence or mapping of values"
        ) from exc


@dataclass(frozen=True)
class DictionarySnapshot:
    """Immutable point-in-time view of a :class:`StableDictionary`."""

    version: int
    entries: Mapping[int, Any]
    next_code: int

    @classmethod
    def _make(
        cls, version: int, entries: dict[int, Any], next_code: int
    ) -> "DictionarySnapshot":
        return cls(version, MappingProxyType(dict(entries)), next_code)

    @classmethod
    def from_entries(
        cls,
        entries: Mapping[int, Any] | Iterable[tuple[int, Any]],
        *,
        version: int = 0,
        next_code: int | None = None,
    ) -> "DictionarySnapshot":
        materialized = dict(entries)
        if next_code is None:
            next_code = max(materialized, default=-1) + 1
        return cls._make(version, materialized, next_code)

    def value_at(self, code: int) -> Any:
        try:
            return self.entries[code]
        except KeyError as exc:
            raise CodeOutOfRangeError(
                f"snapshot version {self.version} has no code {code}"
            ) from exc

    def has_code(self, code: int) -> bool:
        return code in self.entries

    def codes(self) -> tuple[int, ...]:
        return tuple(sorted(self.entries))

    def items(self) -> tuple[tuple[int, Any], ...]:
        return tuple((code, self.entries[code]) for code in sorted(self.entries))

    def values(self) -> tuple[Any, ...]:
        """Return all code-addressable values in increasing code order."""

        return tuple(value for _, value in self.items())

    def decode(self, indices: Iterable[int | None]) -> tuple[Any, ...]:
        result: list[Any] = []
        for code in indices:
            if code is None or code < 0:
                result.append(None)
            else:
                result.append(self.value_at(code))
        return tuple(result)

    def __len__(self) -> int:
        return len(self.entries)


class StableDictionary:
    """Append-only dictionary with stable integer codes."""

    def __init__(
        self,
        initial: Any = None,
        *,
        name: str = "output",
        key_func: Callable[[Any], Hashable] | None = None,
    ) -> None:
        self.name = name
        self._key_func = key_func
        self._entries: dict[int, Any] = {}
        self._keys: dict[int, Hashable] = {}
        self._key_to_canonical_code: dict[Hashable, int] = {}
        self._next_code = 0
        self._version = 0

        if initial is not None:
            self.adopt(initial)

    def _key(self, value: Any) -> Hashable:
        key = value if self._key_func is None else self._key_func(value)
        try:
            hash(key)
        except TypeError as exc:
            raise DictionaryStructureError(
                f"dictionary value/key {key!r} is not hashable"
            ) from exc
        return key

    def adopt(self, dictionary: Any) -> "StableDictionary":
        """Adopt existing code->value bindings.

        This is used when selecting a shard dictionary as the initial output
        dictionary.  Existing codes are preserved.  If two adopted codes have
        the same key, the first/lowest code becomes the canonical code returned
        by :meth:`intern`; the later alias remains addressable for old batches.
        """

        mapping = code_mapping(dictionary)
        for code in sorted(mapping):
            value = mapping[code]
            key = self._key(value)
            if code in self._entries:
                if self._keys[code] != key:
                    raise CodeCollisionError(
                        f"output code {code} is already {self._entries[code]!r}; "
                        f"cannot adopt {value!r}"
                    )
                continue

            if code < 0:
                raise CodeOutOfRangeError(f"output dictionary codes cannot be negative: {code}")
            self._entries[code] = value
            self._keys[code] = key
            self._next_code = max(self._next_code, code + 1)
            self._version += 1
            if key not in self._key_to_canonical_code:
                self._key_to_canonical_code[key] = code
        return self

    def intern(self, value: Any) -> int:
        """Return the stable code for *value*, appending only if new."""

        key = self._key(value)
        existing = self._key_to_canonical_code.get(key)
        if existing is not None:
            return existing

        code = self._next_code
        # Defensive: _next_code is normally beyond every adopted code.
        while code in self._entries:  # pragma: no cover - trivial guard
            code += 1
        self._entries[code] = value
        self._keys[code] = key
        self._key_to_canonical_code[key] = code
        self._next_code = code + 1
        self._version += 1
        return code

    def reuse_or_intern(self, value: Any) -> int:
        """Vocabulary alias for :meth:`intern`."""

        return self.intern(value)

    def code_for(self, value: Any) -> int | None:
        key = self._key(value)
        return self._key_to_canonical_code.get(key)

    def value_at(self, code: int) -> Any:
        try:
            return self._entries[code]
        except KeyError as exc:
            raise CodeOutOfRangeError(f"unknown output dictionary code {code}") from exc

    def canonical_values(self) -> tuple[Any, ...]:
        """Return one value for each canonical code in code order."""

        canonical_codes = set(self._key_to_canonical_code.values())
        return tuple(self._entries[code] for code in sorted(canonical_codes))

    def items(self) -> tuple[tuple[int, Any], ...]:
        return tuple((code, self._entries[code]) for code in sorted(self._entries))

    def values(self) -> tuple[Any, ...]:
        return tuple(value for _, value in self.items())

    def snapshot(self) -> DictionarySnapshot:
        return DictionarySnapshot._make(
            self._version, self._entries, self._next_code
        )

    @property
    def version(self) -> int:
        return self._version

    def __contains__(self, value: Any) -> bool:
        return self.code_for(value) is not None

    def __len__(self) -> int:
        return len(self._key_to_canonical_code)

    def __repr__(self) -> str:
        return f"StableDictionary(name={self.name!r}, entries={len(self._entries)})"


OutputDictionary = StableDictionary


class LocalDictionary:
    """Named local-dictionary input with integer code-to-value bindings."""

    def __init__(self, values: Any = None, *, entries: dict[int, Any] | None = None) -> None:
        if entries is not None and values is not None:
            raise ValueError("pass either values or entries, not both")
        self._entries = code_mapping(entries if entries is not None else values)

    @classmethod
    def from_values(cls, values: Any) -> "LocalDictionary":
        return cls(values)

    @property
    def entries(self) -> Mapping[int, Any]:
        return MappingProxyType(self._entries)

    def get(self, code: int) -> Any:
        return self._entries[code]

    def __getitem__(self, code: int) -> Any:
        return self._entries[code]

    def __contains__(self, code: object) -> bool:
        return code in self._entries

    def codes(self) -> tuple[int, ...]:
        return tuple(sorted(self._entries))

    def values(self) -> tuple[Any, ...]:
        return tuple(self._entries[code] for code in sorted(self._entries))

    def items(self) -> tuple[tuple[int, Any], ...]:
        return tuple((code, self._entries[code]) for code in sorted(self._entries))

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"LocalDictionary(entries={self._entries!r})"
