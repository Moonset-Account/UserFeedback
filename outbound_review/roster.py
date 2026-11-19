"""Preprocessed internal roster and its column/digest inverted index."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Set

from .digest import TEST_KEY, digest_value


@dataclass(frozen=True)
class RosterEntry:
    """One preprocessed internal roster row.

    ``row_code`` is the non-sensitive internal identifier used in review
    output.  ``digests`` maps column names to the normalized, keyed digest of
    that column's value.  Original values are never retained.
    """

    row_code: str
    digests: Mapping[str, str]

    @classmethod
    def from_raw(
        cls,
        row_code: str,
        values: Mapping[str, object],
        key: bytes = TEST_KEY,
    ) -> "RosterEntry":
        digests = {
            column: digest_value(column, value, key)
            for column, value in values.items()
            if value is not None and str(value) != ""
        }
        return cls(row_code=row_code, digests=dict(digests))


@dataclass
class InternalIndex:
    """Inverted index from (column, digest) to the set of internal row codes.

    Because several people can share a surname or a department, one
    (column, digest) pair may legitimately point at more than one row code.
    """

    # column -> digest -> row codes
    entries: Dict[str, Dict[str, Set[str]]] = field(default_factory=dict)
    row_codes: Set[str] = field(default_factory=set)

    @classmethod
    def build(
        cls,
        raw_rows: Iterable[Mapping[str, object]],
        key: bytes = TEST_KEY,
        code_field: str = "row_code",
    ) -> "InternalIndex":
        index = cls()
        for raw in raw_rows:
            if code_field not in raw:
                raise ValueError(f"roster row is missing {code_field!r}")
            row_code = str(raw[code_field])
            if row_code in index.row_codes:
                raise ValueError(f"duplicate internal row code: {row_code!r}")
            index.row_codes.add(row_code)
            values = {k: v for k, v in raw.items() if k != code_field}
            entry = RosterEntry.from_raw(row_code, values, key)
            for column, digest in entry.digests.items():
                index.entries.setdefault(column, {}).setdefault(digest, set()).add(
                    row_code
                )
        return index

    @classmethod
    def from_preprocessed(
        cls, entries: Iterable[Mapping[str, object]]
    ) -> "InternalIndex":
        """Rebuild an index from already-digested roster entries.

        Accepts either :class:`RosterEntry` objects or mappings shaped like
        ``{"row_code": ..., "digests": {column: digest}}``.
        """
        index = cls()
        for item in entries:
            if isinstance(item, RosterEntry):
                row_code, digests = item.row_code, dict(item.digests)
            else:
                if "row_code" not in item or "digests" not in item:
                    raise ValueError(
                        "preprocessed entry needs 'row_code' and 'digests'"
                    )
                row_code = str(item["row_code"])
                digests = dict(item["digests"])
            if row_code in index.row_codes:
                raise ValueError(f"duplicate internal row code: {row_code!r}")
            index.row_codes.add(row_code)
            for column, digest in digests.items():
                index.entries.setdefault(column, {}).setdefault(
                    str(digest), set()
                ).add(row_code)
        return index

    def lookup(self, column: str, digest: str) -> Set[str]:
        """Return the row codes a (column, digest) pair points at."""
        return self.entries.get(column, {}).get(digest, set())

    def to_dict(self) -> Dict[str, object]:
        """Serialize without ever exposing original values."""
        return {
            "entries": [
                {
                    "row_code": row_code,
                    "digests": {
                        column: digest
                        for column, bucket in self.entries.items()
                        for digest, codes in bucket.items()
                        if row_code in codes
                    },
                }
                for row_code in sorted(self.row_codes)
            ]
        }


def preprocess_roster(
    raw_rows: Iterable[Mapping[str, object]],
    key: bytes = TEST_KEY,
    code_field: str = "row_code",
) -> List[RosterEntry]:
    """Convenience wrapper: raw roster rows -> preprocessed entries."""
    result: List[RosterEntry] = []
    for raw in raw_rows:
        row_code = str(raw[code_field])
        values = {k: v for k, v in raw.items() if k != code_field}
        result.append(RosterEntry.from_raw(row_code, values, key))
    return result


def load_preprocessed(
    payload: Mapping[str, object],
) -> InternalIndex:
    """Load the JSON shape produced by :meth:`InternalIndex.to_dict`."""
    raw_entries: Optional[Iterable[Mapping[str, object]]] = payload.get("entries")  # type: ignore[assignment]
    if raw_entries is None:
        raise ValueError("preprocessed roster payload needs an 'entries' list")
    return InternalIndex.from_preprocessed(raw_entries)
