"""Acceptance tests for outbound roster review.

Cross-constraint under test (from the problem statement):

* a hit requires *at least two different designated columns* of the same
  outbound logical row to point at the *same* internal row code;
* repeated occurrences inside one column count as a single column;
* shared surnames / shared departments must not produce false hits;
* logical rows may span pages (shards) and repeated headers are not data;
* the output must never contain original values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

# Make the local package importable even under "python -I" (isolated mode
# ignores PYTHONPATH and does not prepend the working directory).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from outbound_review import (
    TEST_KEY,
    InternalIndex,
    ReviewAssembler,
    digest_value,
    normalize_value,
    preprocess_roster,
    run_review,
)
from outbound_review.roster import load_preprocessed

RAW_ROSTER = [
    {
        "row_code": "R-1001",
        "name": "Zhang Wei",
        "email": "zhang.wei@example.com",
        "department": "Engineering",
        "employee_id": "EMP-001",
        "phone": "13800000001",
    },
    {
        "row_code": "R-1002",
        "name": "Zhang Na",  # same surname as R-1001
        "email": "zhang.na@example.com",
        "department": "Engineering",  # same department as R-1001
        "employee_id": "EMP-002",
        "phone": "13800000002",
    },
    {
        "row_code": "R-1003",
        "name": "Li Qiang",
        "email": "li.qiang@example.com",
        "department": "Sales",
        "employee_id": "EMP-003",
        "phone": "13800000003",
    },
]


def build_index() -> InternalIndex:
    return InternalIndex.build(RAW_ROSTER, key=TEST_KEY)


def cell(
    logical_row: str,
    column: str,
    value: object,
    page: int,
    physical_row: int,
    cell_ref: str,
    row_type: str = "data",
) -> dict:
    return {
        "logical_row": logical_row,
        "column": column,
        "value": value,
        "page": page,
        "physical_row": physical_row,
        "cell_ref": cell_ref,
        "row_type": row_type,
    }


class DigestAndRosterTests(unittest.TestCase):
    def test_normalization_rules_are_column_specific(self) -> None:
        self.assertEqual(normalize_value("name", "  Zhang   Wei "), "zhang wei")
        self.assertEqual(
            normalize_value("email", " Zhang.Wei@Example.COM "),
            "zhang.wei@example.com",
        )
        self.assertEqual(normalize_value("phone", "+86 138-0000-0001"), "8613800000001")
        self.assertEqual(
            normalize_value("employee_id", "emp 001"),
            "EMP001",
        )
        self.assertEqual(normalize_value("department", " Engineering "), "engineering")
        self.assertEqual(normalize_value("name", None), "")

    def test_digest_is_column_bound_and_stable(self) -> None:
        first = digest_value("name", "Zhang Wei", TEST_KEY)
        second = digest_value("name", "  zhang   wei ", TEST_KEY)
        self.assertEqual(first, second, "normalized equivalents must share a digest")
        # Same normalized text, different column -> different digest.
        other = digest_value("department", "Zhang Wei", TEST_KEY)
        self.assertNotEqual(first, other)

    def test_preprocessed_roster_roundtrip_keeps_no_raw_values(self) -> None:
        entries = preprocess_roster(RAW_ROSTER, key=TEST_KEY)
        serialized = json.dumps([e.__dict__ for e in entries])
        for raw in RAW_ROSTER:
            self.assertNotIn(raw["name"], serialized)
            self.assertNotIn(raw["email"], serialized)
        index = InternalIndex.from_preprocessed(entries)
        reloaded = load_preprocessed(
            {"entries": [{"row_code": e.row_code, "digests": e.digests} for e in entries]}
        )
        self.assertEqual(index.lookup("email", digest_value("email", "li.qiang@example.com", TEST_KEY)),
                         reloaded.lookup("email", digest_value("email", "li.qiang@example.com", TEST_KEY)))


class ReviewMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = build_index()

    def test_accepted_hit_two_designated_columns_same_internal_row(self) -> None:
        """Acceptable result: name + email both resolve to R-1001."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L1", "name", "Zhang Wei", 1, 2, "A2"),
                    cell("L1", "email", "zhang.wei@example.com", 1, 2, "B2"),
                    cell("L1", "department", "Engineering", 1, 2, "C2"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        self.assertEqual(len(report.hits), 1)
        hit = report.hits[0]
        self.assertEqual(hit.row_code, "R-1001")
        self.assertEqual(hit.logical_row, "L1")
        self.assertEqual(set(hit.matched_columns), {"name", "email", "department"})
        refs = {loc.cell_ref for loc in hit.cell_locations}
        self.assertEqual(refs, {"A2", "B2", "C2"})

    def test_blocked_single_column_even_with_repeated_cells(self) -> None:
        """Rejected result: only the name column matches, repeated cells still
        count as one column, so no hit may be emitted."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L9", "name", "Zhang Wei", 1, 4, "A4"),
                    cell("L9", "name", "ZHANG WEI", 1, 5, "A5"),
                    cell("L9", "name", "ZhangWei", 1, 6, "A6"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        self.assertEqual(report.hits, [])
        self.assertEqual(report.reviewed_logical_rows, 1)

    def test_shared_surname_and_department_do_not_falsely_hit(self) -> None:
        """Boundary/interleaving: two internal people share surname and
        department.  name points at both rows, department points at both rows,
        but email disambiguates to exactly one row code."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L2", "name", "Zhang Na", 1, 3, "A3"),
                    cell("L2", "department", "Engineering", 1, 3, "C3"),
                    cell("L2", "email", "zhang.na@example.com", 1, 3, "B3"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        self.assertEqual(len(report.hits), 1)
        self.assertEqual(report.hits[0].row_code, "R-1002")
        # The shared columns alone must never identify R-1001 here.
        self.assertNotIn("R-1001", {h.row_code for h in report.hits})

    def test_two_columns_pointing_at_different_rows_is_no_hit(self) -> None:
        """Boundary: name matches R-1001 but email matches R-1003; no single
        internal row code is supported by two columns."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L3", "name", "Zhang Wei", 1, 7, "A7"),
                    cell("L3", "email", "li.qiang@example.com", 1, 7, "B7"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        self.assertEqual(report.hits, [])

    def test_logical_row_spanning_pages_with_repeated_headers(self) -> None:
        """Same logical row continues on page 2; page 2 repeats the header,
        which must not be treated as data.  One column contributes multiple
        cells but still counts once."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L4", "name", "Li Qiang", 1, 9, "A9"),
                ],
            },
            {  # repeated header row, explicit marker
                "page": 2,
                "cells": [
                    cell("", "name", "name", 2, 1, "A1", row_type="header"),
                    cell("", "email", "email", 2, 1, "B1", row_type="header"),
                ],
            },
            {
                "page": 2,
                "cells": [
                    cell("L4", "name", "LI QIANG", 2, 2, "A2"),  # same column again
                    cell("L4", "email", "li.qiang@example.com", 2, 2, "B2"),
                ],
            },
        ]
        report = run_review(self.index, shards)
        self.assertEqual(report.header_rows_skipped, 1)
        self.assertEqual(len(report.hits), 1)
        hit = report.hits[0]
        self.assertEqual(hit.row_code, "R-1003")
        self.assertEqual(set(hit.matched_columns), {"name", "email"})
        pages = {loc.page for loc in hit.cell_locations}
        self.assertEqual(pages, {1, 2}, "locations must support cross-page review")

    def test_interleaved_rows_across_pages_assemble_independently(self) -> None:
        """Two logical rows interleaved across pages must be assembled and
        judged separately."""
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L5", "name", "Zhang Wei", 1, 2, "A2"),
                    cell("L6", "name", "Li Qiang", 1, 3, "A3"),
                ],
            },
            {
                "page": 2,
                "cells": [
                    cell("L5", "email", "zhang.wei@example.com", 2, 2, "B2"),
                    # L6's second cell is not a designated column, so across
                    # both pages it still only contributes one column and
                    # must stay below the hit threshold.
                    cell("L6", "note", "follow up next week", 2, 3, "C3"),
                ],
            },
        ]
        report = run_review(self.index, shards)
        hits_by_row = {h.logical_row: h for h in report.hits}
        self.assertEqual(set(hits_by_row), {"L5"})
        self.assertEqual(hits_by_row["L5"].row_code, "R-1001")

    def test_non_designated_columns_are_ignored(self) -> None:
        assembler = ReviewAssembler(self.index, designated_columns=["name", "email"])
        assembler.ingest_shard(
            {
                "page": 1,
                "cells": [
                    cell("L7", "name", "Zhang Wei", 1, 2, "A2"),
                    cell("L7", "department", "Engineering", 1, 2, "C2"),
                    cell("L7", "employee_id", "EMP-001", 1, 2, "D2"),
                ],
            }
        )
        report = assembler.review()
        self.assertEqual(report.hits, [], "only one designated column matched")

    def test_heuristic_header_row_detection(self) -> None:
        shards = [
            {  # values repeat the column names -> header, no row_type marker
                "page": 1,
                "cells": [
                    cell("", "name", "name", 1, 1, "A1"),
                    cell("", "email", "email", 1, 1, "B1"),
                ],
            },
            {
                "page": 1,
                "cells": [
                    cell("L8", "name", "Li Qiang", 1, 2, "A2"),
                    cell("L8", "email", "li.qiang@example.com", 1, 2, "B2"),
                ],
            },
        ]
        report = run_review(self.index, shards)
        self.assertEqual(report.header_rows_skipped, 1)
        self.assertEqual(len(report.hits), 1)
        self.assertEqual(report.hits[0].row_code, "R-1003")

    def test_output_never_contains_original_values(self) -> None:
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L1", "name", "Zhang Wei", 1, 2, "A2"),
                    cell("L1", "email", "zhang.wei@example.com", 1, 2, "B2"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        serialized = json.dumps(report.to_dict())
        for secret in ("Zhang Wei", "zhang.wei@example.com", "EMP-001", "13800000001"):
            self.assertNotIn(secret, serialized)
        # Only codes, column names and locations may appear.
        hit = report.hits[0].to_dict()
        self.assertEqual(set(hit), {"logical_row", "row_code", "matched_columns", "cell_locations"})
        for loc in hit["cell_locations"]:
            self.assertEqual(set(loc), {"page", "physical_row", "column", "cell_ref"})

    def test_duplicate_cell_delivery_is_deduplicated(self) -> None:
        shard = {
            "page": 1,
            "cells": [
                cell("L10", "name", "Li Qiang", 1, 2, "A2"),
                cell("L10", "name", "Li Qiang", 1, 2, "A2"),  # identical redelivery
                cell("L10", "email", "li.qiang@example.com", 1, 2, "B2"),
            ],
        }
        report = run_review(self.index, [shard, shard])
        self.assertEqual(len(report.hits), 1)
        name_locations = [
            loc for loc in report.hits[0].cell_locations if loc.column == "name"
        ]
        self.assertEqual(len(name_locations), 1)

    def test_empty_cells_never_match(self) -> None:
        shards = [
            {
                "page": 1,
                "cells": [
                    cell("L11", "name", "", 1, 2, "A2"),
                    cell("L11", "email", None, 1, 2, "B2"),
                ],
            }
        ]
        report = run_review(self.index, shards)
        self.assertEqual(report.hits, [])
        self.assertEqual(report.reviewed_logical_rows, 0)


class CliEndToEndTests(unittest.TestCase):
    def test_preprocess_and_review_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_path = os.path.join(tmp, "roster_raw.json")
            roster_path = os.path.join(tmp, "roster_digests.json")
            shards_path = os.path.join(tmp, "shards.json")
            report_path = os.path.join(tmp, "report.json")
            with open(raw_path, "w", encoding="utf-8") as handle:
                json.dump(RAW_ROSTER, handle)
            with open(shards_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "shards": [
                            {
                                "page": 1,
                                "cells": [
                                    cell("L1", "name", "Zhang Wei", 1, 2, "A2"),
                                    cell("L1", "email", "zhang.wei@example.com", 1, 2, "B2"),
                                ],
                            }
                        ]
                    },
                    handle,
                )

            # Subprocess runs against trusted, test-created fixtures only;
            # cwd=REPO_ROOT lets the package resolve as a local directory.
            pre = subprocess.run(
                [sys.executable, "-m", "outbound_review.cli", "preprocess",
                 "--roster", raw_path, "--out", roster_path],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pre.returncode, 0, pre.stderr)
            with open(roster_path, encoding="utf-8") as handle:
                preprocessed = json.load(handle)
            self.assertNotIn("Zhang Wei", json.dumps(preprocessed))

            rev = subprocess.run(
                [sys.executable, "-m", "outbound_review.cli", "review",
                 "--roster", roster_path, "--shards", shards_path,
                 "--out", report_path],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(rev.returncode, 0, rev.stderr)
            with open(report_path, encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertEqual(len(report["hits"]), 1)
            self.assertEqual(report["hits"][0]["row_code"], "R-1001")
            self.assertNotIn("Zhang Wei", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
