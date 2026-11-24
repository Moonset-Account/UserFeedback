"""Command-line entry point: ``python3 -m memory_accounting receipts.json``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

from .replay import replay_receipts


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage: python3 -m memory_accounting RECEIPTS.json [--summary]\n"
            "\n"
            "Replay synthetic or collected receipt JSON and print accounting JSON.\n"
            "Use --summary for a compact text summary instead of full JSON."
        )
        return 0 if args else 2

    path = Path(args[0])
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    result = replay_receipts(data)
    if "--summary" in args[1:]:
        report = result.report
        print(f"latest_accountable_position={result.latest_accountable_position}")
        print(f"latest_complete_position={result.latest_complete_position}")
        print(f"gaps={[g.to_dict() for g in result.gaps]}")
        print(f"physical_frames={report.physical_frame_count}")
        print(f"physical_resident_bytes={report.physical_resident_bytes}")
        print(
            "rss_reconciliation="
            f"process_rss_sum={report.process_rss_sum_bytes},"
            f"shared_overcount={report.shared_frame_overcount_bytes},"
            f"duplicate_mapping_overcount={report.duplicate_mapping_overcount_bytes}"
        )
        for pid, process in report.processes.items():
            print(
                f"pid={pid} exclusive={process.exclusive_bytes} "
                f"proportional={process.proportional_bytes} "
                f"share={process.share_of_physical}"
            )
        return 0

    json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
