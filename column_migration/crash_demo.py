"""Subprocess entry point for real fail-stop crash tests.

Usage:
    python -m column_migration.crash_demo <store_dir> <target_event_seq>

Exit code 97 means the requested barrier was reached and the process exited
immediately via ``os._exit``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .crash import CrashController
from .crash_scenarios import run_crash_scenario


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m column_migration.crash_demo STORE_DIR EVENT_SEQ", file=sys.stderr)
        return 2
    root = Path(args[0])
    target = int(args[1])
    controller = CrashController(crash_at_seq=target, hard_exit=True)
    run_crash_scenario(root, controller)
    print(
        f"target event {target} was not reached; recorded {len(controller.events)} events",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
