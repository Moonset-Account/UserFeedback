#!/usr/bin/env python3
"""CLI entry for the workspace lock scheduler.

Usage:
    python3 app.py <input.json>          # pretty report to stdout
    python3 app.py <input.json> --compact

Input JSON:
{
  "case_id": "optional-label",
  "timezone": "America/New_York",          # IANA name
  "now_utc": "2026-11-01T05:45:00Z",       # current instant, explicit offset
  "weekly_shifts": [
    {"weekday": "sun", "start": "01:30", "end": "05:00"}
  ],
  "extensions": [
    {"start_utc": "2026-03-08T06:00:00Z",
     "end_utc": "2026-03-08T06:15:00Z",
     "label": "hotfix-window"}
  ],
  "workspaces": [{"id": "ws-nyc-01"}]
}

Output: JSON report with, per workspace, whether it must be locked now,
the decision basis, and the next state-switch instant (UTC).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lock_scheduler import evaluate


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        config = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        report = evaluate(config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    compact = "--compact" in argv
    json.dump(report, sys.stdout, indent=None if compact else 2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
