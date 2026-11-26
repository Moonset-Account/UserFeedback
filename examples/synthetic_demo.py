"""Local synthetic end-to-end demonstration.

Run from the repository root:

    python3 examples/synthetic_demo.py

No network, credentials or third-party packages are used.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mtu_geometry import (  # noqa: E402
    PathSpec,
    StepOverhead,
    TunnelLayer,
    check_commitment,
    common_payload_bounds,
    plan_multipath,
)


def main() -> None:
    padding = StepOverhead.by_upper_bounds(
        [
            (128, 20),
            (256, 60),
            (None, 90),
        ]
    )
    layers = [
        TunnelLayer(overhead=20, name="inner-tunnel"),
        TunnelLayer(overhead=padding, name="outer-tunnel"),
    ]

    path_a = PathSpec(
        path_id="metro-a",
        direction="forward",
        hops=[("access", (1450, 1500)), ("core", 1400)],
        layers=layers,
        alignment=8,
        dont_fragment=True,
        confirmed_windows=[(0, 30), (40, 70)],
    )
    path_b = PathSpec(
        path_id="metro-b",
        direction="forward",
        hops=[("edge", 1500), ("core-backup", (1380, 1420))],
        layers=layers,
        alignment=8,
        dont_fragment=True,
        confirmed_windows=[(10, 50), (60, 90)],
    )

    common = common_payload_bounds([path_a, path_b])
    print("common guaranteed payload:", common.guaranteed_max)
    print("limiting path/hop:", common.limiting_path, common.limiting_hop)

    payload = common.guaranteed_max or 0
    plan = plan_multipath(payload, [path_a, path_b])
    print("switch payload:", plan.payload)
    print("common windows:", [(w.start, w.end) for w in plan.common_windows])
    print(
        "redundant intervals:",
        [
            ((s.window.start, s.window.end), s.path_ids)
            for s in plan.redundant_windows()
        ],
    )

    missing = PathSpec(
        path_id="partial-path",
        hops=[("access", 1500), ("unknown-tunnel", None), ("core", 1400)],
        layers=[TunnelLayer(overhead=40)],
    )
    report = check_commitment(missing, 1300)
    print("missing status:", report.status)
    print("blocking hop:", report.blocker.hop)
    print("required MTU:", report.blocker.required_mtu)
    print("optimistic payload ceiling:", report.size_report.bounds.optimistic_max)
    print("proven impossible from:", report.size_report.bounds.proven_impossible_from)


if __name__ == "__main__":
    main()
