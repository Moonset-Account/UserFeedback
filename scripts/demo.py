"""Runnable end-to-end demonstration on fully local synthetic records.

Run with:  python3 scripts/demo.py
The script prints the assessment of one sample whose journey combines all
three accounting rules (container-scoped sensors, ambiguous handover band,
and splicing of adjacent acceptable intervals).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_accounting import (
    HandoverWindow,
    SampleJourney,
    SealCheck,
    Sensor,
    Transfer,
    assess_sample,
)


def main() -> None:
    # Sample S moves C1 -> C2 -> C3 through two transfers.
    t1 = Transfer(10.0, "C1", "C2", "DOC-1")
    t2 = Transfer(20.0, "C2", "C3", "DOC-2")
    journey = SampleJourney("S", "C1", 0.0, 30.0, (t1, t2))

    # S1 stays on the old container C1 and must not testify after t=10.
    # S3 on C3 has a calibration that expires at t=24.
    sensors = [
        Sensor("S1", "C1", (0.0, 30.0), ((0.0, 30.0),)),
        Sensor("S2", "C2", (10.0, 20.0), ((10.0, 20.0),)),
        Sensor("S3", "C3", (20.0, 30.0), ((20.0, 24.0),)),
    ]
    seals = [
        SealCheck("K1", "C1", (0.0, 10.0)),
        SealCheck("K2", "C2", (10.0, 20.0)),
        SealCheck("K3", "C3", (20.0, 30.0)),
    ]
    # DOC-1 confirmed by both parties; DOC-2 reported by only one party.
    windows = [
        HandoverWindow("DOC-1", "alpha", (9.0, 11.0)),
        HandoverWindow("DOC-1", "beta", (9.5, 10.5)),
        HandoverWindow("DOC-2", "alpha", (21.0, 23.0)),
    ]

    result = assess_sample(journey, sensors, seals, windows)
    print("acceptable segments :", result.acceptable_segments)
    print("earliest uncovered   :", result.earliest_uncovered)
    print("ambiguous bands      :", result.ambiguous_intervals)
    print("blocked instants     :", result.blocked_points)
    print("gap documents        :")
    for doc in result.gap_documents:
        print(
            "   ",
            doc.doc_type,
            doc.doc_id,
            doc.reason,
            "|",
            doc.detail,
        )


if __name__ == "__main__":
    main()
