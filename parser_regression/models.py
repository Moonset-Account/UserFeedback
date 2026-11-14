"""Immutable data types shared by the loader, comparator and renderers."""

from dataclasses import dataclass
from typing import Optional, Tuple

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

STATUS_VALUES = (PASSED, FAILED, SKIPPED)


@dataclass(frozen=True)
class TestRecord:
    """One exported attempt of one sample in one mode/environment."""

    sample_name: str
    parse_mode: str
    environment: str
    build_id: str
    attempt: int
    status: str
    failure_category: Optional[str]
    content: Optional[str]
    content_hash: Optional[str]
    summary: str
    identity: str
    identity_basis: str


@dataclass(frozen=True)
class Report:
    """A single build export: one build identifier plus all its attempts.

    ``source`` is the export's own provenance note (or a fallback label when the
    export does not provide one); ``path`` is where it was read from, which is
    ``<inline>`` for documents parsed in memory.
    """

    build_id: str
    records: Tuple[TestRecord, ...]
    source: str
    path: str = "<inline>"

    def group_count(self) -> int:
        keys = {
            (record.parse_mode.strip().casefold(),
             record.environment.strip().casefold(),
             record.identity)
            for record in self.records
        }
        return len(keys)
