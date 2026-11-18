"""pytest wrappers for the BigData / KeyValue.data QA cases.

The real assertions live in ``tests/bigdata_cases.py`` so that both pytest
and ``tools/local_verify.py`` execute the exact same checks.
"""

from __future__ import annotations

import pytest

from tests.bigdata_cases import CASES, SEED_C04, build_randomized_big_hive, run_case


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_bigdata_case(case_id: str) -> None:
    result = run_case(case_id)
    assert result["assertions"] > 0, "case recorded no assertions"
    assert result["failed"] == 0, result["failures"]


def test_seeded_hive_replay_is_deterministic() -> None:
    builder1, hive_bytes1, content1, layout1 = build_randomized_big_hive(SEED_C04)
    builder2, hive_bytes2, content2, layout2 = build_randomized_big_hive(SEED_C04)

    assert hive_bytes1 == hive_bytes2
    assert content1 == content2
    assert layout1 == layout2
    assert [r.as_dict() for r in builder1.records] == [r.as_dict() for r in builder2.records]
