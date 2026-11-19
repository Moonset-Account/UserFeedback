"""Outbound roster review.

The reviewer receives table cells in shards (pages), column names and logical
row boundaries.  It reassembles logical rows that may span pages, ignores
repeated header rows, and flags a hit only when **at least two different
designated columns** of the same outbound logical row point at one and the
same internal row code.

Repeated occurrences inside one column still count as a single column.  The
report never contains original values, only internal row codes, the matched
column names and outbound cell locations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .digest import TEST_KEY, digest_value, normalize_value
from .roster import InternalIndex

MIN_MATCHING_COLUMNS = 2


@dataclass(frozen=True)
class CellLocation:
    """Position of one outbound table cell."""

    page: int
    physical_row: int
    column: str
    cell_ref: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "page": self.page,
            "physical_row": self.physical_row,
            "column": self.column,
            "cell_ref": self.cell_ref,
        }


@dataclass(frozen=True)
class Hit:
    """One confirmed match between an outbound row and an internal row."""

    logical_row: str
    row_code: str
    matched_columns: Tuple[str, ...]
    cell_locations: Tuple[CellLocation, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "logical_row": self.logical_row,
            "row_code": self.row_code,
            "matched_columns": list(self.matched_columns),
            "cell_locations": [loc.to_dict() for loc in self.cell_locations],
        }


@dataclass
class ReviewReport:
    hits: List[Hit] = field(default_factory=list)
    reviewed_logical_rows: int = 0
    header_rows_skipped: int = 0
    cells_processed: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "reviewed_logical_rows": self.reviewed_logical_rows,
            "header_rows_skipped": self.header_rows_skipped,
            "cells_processed": self.cells_processed,
        }


class _ColumnEvidence:
    """Digested cells of one logical row for one column."""

    __slots__ = ("digest", "locations")

    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.locations: List[CellLocation] = []


class ReviewAssembler:
    """Collects sharded cells and evaluates matches on demand."""

    def __init__(
        self,
        index: InternalIndex,
        designated_columns: Optional[Sequence[str]] = None,
        key: bytes = TEST_KEY,
    ) -> None:
        self._index = index
        self._key = key
        if designated_columns is None:
            designated_columns = tuple(index.entries.keys())
        self._designated: Set[str] = set(designated_columns)
        # logical row -> column -> evidence
        self._rows: Dict[str, Dict[str, _ColumnEvidence]] = {}
        # logical row -> column -> set of seen cell refs (de-duplication)
        self._seen_refs: Dict[str, Dict[str, Set[str]]] = {}
        self.header_rows_skipped = 0
        self.cells_processed = 0

    # ------------------------------------------------------------------ shards
    def ingest_shard(self, shard: Mapping[str, Any]) -> None:
        """Ingest one shard.

        Shard shape::

            {"page": 1, "cells": [
                {"logical_row": "L1", "column": "name", "value": "...",
                 "physical_row": 3, "row_type": "data", "cell_ref": "B3"},
                ...
            ]}

        ``row_type`` may be ``"data"`` (default) or ``"header"``.  Header rows
        may also be detected heuristically when every cell value equals its
        own column name.
        """
        page = int(shard["page"])
        cells = list(shard["cells"])
        # Header rows can share a shard with data rows, so group cells by
        # physical row and classify each row independently.  Physical row
        # numbers are only unique within a page, so the page is part of key.
        rows_in_shard: Dict[Tuple[int, int], List[Mapping[str, Any]]] = {}
        order: List[Tuple[int, int]] = []
        for cell in cells:
            physical_row = int(cell.get("physical_row", 0))
            group_key = (page, physical_row)
            if group_key not in rows_in_shard:
                rows_in_shard[group_key] = []
                order.append(group_key)
            rows_in_shard[group_key].append(cell)
        for group_key in order:
            row_cells = rows_in_shard[group_key]
            if self._is_header_row(row_cells):
                self.header_rows_skipped += 1
                continue
            for cell in row_cells:
                self._ingest_cell(page, cell)

    def ingest_shards(self, shards: Iterable[Mapping[str, Any]]) -> None:
        for shard in shards:
            self.ingest_shard(shard)

    def _is_header_row(self, cells: Sequence[Mapping[str, Any]]) -> bool:
        if not cells:
            return False
        explicit = {str(c.get("row_type", "")).lower() for c in cells}
        if explicit == {"header"}:
            return True
        # "Repeated headers are not data" is a structural fact about the table,
        # so content detection wins over the default row_type marker: a header
        # row often arrives tagged as ordinary data.  Only an explicit
        # "header" marker forces the result above; otherwise classify by
        # whether every cell's value is literally its column label.
        for cell in cells:
            column = str(cell["column"])
            label = "" if cell.get("value") is None else str(cell["value"]).strip()
            if label.casefold() != column.casefold():
                return False
        return True

    def _ingest_cell(self, page: int, cell: Mapping[str, Any]) -> None:
        row_type = str(cell.get("row_type", "data")).lower()
        if row_type == "header":
            # A header cell mixed into a data shard still is not data.
            self.header_rows_skipped += 1
            return
        self.cells_processed += 1
        logical_row = str(cell["logical_row"])
        column = str(cell["column"])
        if column not in self._designated:
            return
        normalized = normalize_value(column, cell.get("value"))
        if normalized == "":
            # Empty cells can never match a roster value; ignore quietly.
            return
        digest = digest_value(column, cell.get("value"), self._key)
        physical_row = int(cell.get("physical_row", 0))
        cell_ref = str(cell.get("cell_ref") or f"p{page}:r{physical_row}:{column}")
        seen = self._seen_refs.setdefault(logical_row, {}).setdefault(column, set())
        if cell_ref in seen:
            return  # same outbound cell delivered twice
        seen.add(cell_ref)
        location = CellLocation(
            page=page,
            physical_row=physical_row,
            column=column,
            cell_ref=cell_ref,
        )
        row = self._rows.setdefault(logical_row, {})
        evidence = row.get(column)
        if evidence is None:
            evidence = _ColumnEvidence(digest)
            row[column] = evidence
        evidence.locations.append(location)

    # ----------------------------------------------------------------- review
    def review(self) -> ReviewReport:
        report = ReviewReport(
            reviewed_logical_rows=len(self._rows),
            header_rows_skipped=self.header_rows_skipped,
            cells_processed=self.cells_processed,
        )
        for logical_row in sorted(self._rows):
            report.hits.extend(self._review_row(logical_row, self._rows[logical_row]))
        return report

    def _review_row(
        self, logical_row: str, row: Mapping[str, _ColumnEvidence]
    ) -> List[Hit]:
        # Internal row code -> distinct columns that point at it.
        code_columns: Dict[str, Set[str]] = {}
        for column, evidence in row.items():
            for row_code in self._index.lookup(column, evidence.digest):
                code_columns.setdefault(row_code, set()).add(column)
        hits: List[Hit] = []
        for row_code in sorted(code_columns):
            columns = code_columns[row_code]
            if len(columns) < MIN_MATCHING_COLUMNS:
                continue  # one column alone, even repeated, never hits
            matched = tuple(sorted(columns))
            locations: List[CellLocation] = []
            for column in matched:
                locations.extend(row[column].locations)
            hits.append(
                Hit(
                    logical_row=logical_row,
                    row_code=row_code,
                    matched_columns=matched,
                    cell_locations=tuple(
                        sorted(
                            locations,
                            key=lambda loc: (
                                loc.page,
                                loc.physical_row,
                                loc.column,
                                loc.cell_ref,
                            ),
                        )
                    ),
                )
            )
        return hits


def run_review(
    index: InternalIndex,
    shards: Iterable[Mapping[str, Any]],
    designated_columns: Optional[Sequence[str]] = None,
    key: bytes = TEST_KEY,
) -> ReviewReport:
    """End-to-end convenience: ingest all shards and return the report."""
    assembler = ReviewAssembler(index, designated_columns, key=key)
    assembler.ingest_shards(shards)
    return assembler.review()
