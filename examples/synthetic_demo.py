"""Local synthetic demonstration of out-of-order dictionary reduction.

Run from the workspace root:

    python3 examples/synthetic_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dictionary_reduction import (  # noqa: E402
    CASE_INSENSITIVE,
    BINARY,
    DictionaryReducer,
    DictionarySegment,
    OrderDeclaration,
)


def main() -> None:
    shard_1 = DictionarySegment(
        segment_id="shard-1",
        dictionary=["alpha", "beta"],
        codes=[0, 0, 1],
        measures=[1, 4, 2],
        row_ids=[10, 12, 11],
        ordering=OrderDeclaration.ascending(rows="any"),
    )
    shard_2 = DictionarySegment(
        segment_id="shard-2",
        dictionary=["beta", "alpha"],
        codes=[1, 0, 1, 0],
        measures=[10, 20, 30, 40],
        row_ids=[3, 0, 1, 2],
    )

    reducer = DictionaryReducer(
        selected_dictionary=["beta", "alpha"],
        verify_declarations=True,
    )
    batch_1 = reducer.add_segment(shard_1)
    batch_2 = reducer.add_segment(shard_2)
    result = reducer.result(order="ascending")

    print("batch_1 codes:", batch_1.codes)
    print("batch_2 codes:", batch_2.codes)
    print("sum/first_row:", result.tuples())

    collation_segment = DictionarySegment(
        dictionary=["Apple", "apple"],
        codes=[0, 1],
        measures=[1, 2],
        row_ids=[1, 0],
    )
    binary = DictionaryReducer(domain=BINARY).reduce([collation_segment])
    ci = DictionaryReducer(domain=CASE_INSENSITIVE).reduce([collation_segment])
    print("binary groups:", sorted(binary.tuples().items()))
    print("case-insensitive groups:", ci.tuples())


if __name__ == "__main__":
    main()
