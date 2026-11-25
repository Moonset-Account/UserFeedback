"""Small local synthetic demonstration of the forensic reader."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forensic_reader import ForensicReader  # noqa: E402


def main() -> None:
    chips = {
        "A": bytes([0x00, 0x01, 0x02, 0x03]),
        "B": bytes([0x10, 0x11, 0x12, 0x13]),
    }
    reader = ForensicReader(
        chips,
        [
            {"id": "AB", "order": ["A", "B"]},
            {"id": "BA", "order": ["B", "A"]},
        ],
        word_size=2,
    )

    before = reader.read(0, 8)
    print("before:", before.state)
    for segment in before.ambiguous_segments:
        for interpretation in segment.interpretations:
            print(" ", interpretation.wiring_ids, interpretation.hex)

    reader.add_known_byte(0, 0x00, source="synthetic reset vector")
    after = reader.read(0, 8)
    print("after:", after.state, after.confirmed_bytes().hex())

    trace = reader.trace(0, 8)
    print("trace:", trace.initial.state, "->", trace.current.state)


if __name__ == "__main__":
    main()
