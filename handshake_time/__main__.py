"""CLI entry point: read receipts as JSON on stdin, print adjudication as JSON.

Input shape::

    {
      "min_sources": 2,
      "valid_from": "2026-11-20T00:00:00Z",
      "valid_to": "2026-11-20T23:59:59Z",
      "receipts": [
        {"start": "...", "end": "...", "source": "gps", "authenticated": true}
      ]
    }

Output is ``AdjudicationResult.to_dict()`` plus ``exit_code`` (0 release,
2 reject, 3 hold) so scripts can branch on it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from .core import TimeReceipt, Verdict, adjudicate

_EXIT_CODES = {
    Verdict.RELEASE: 0,
    Verdict.REJECT: 2,
    Verdict.HOLD: 3,
}


def _parse_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must carry a UTC offset")
    return parsed


def main(argv: list[str] | None = None) -> int:
    del argv  # no positional arguments; input comes from stdin
    try:
        payload = json.load(sys.stdin)
        receipts = [
            TimeReceipt(
                start=_parse_time(item["start"], "receipt.start"),
                end=_parse_time(item["end"], "receipt.end"),
                source=item["source"],
                authenticated=item.get("authenticated", True),
            )
            for item in payload.get("receipts", [])
        ]
        result = adjudicate(
            receipts,
            min_sources=int(payload["min_sources"]),
            valid_from=_parse_time(payload["valid_from"], "valid_from"),
            valid_to=_parse_time(payload["valid_to"], "valid_to"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        json.dump({"error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    output = result.to_dict()
    output["exit_code"] = _EXIT_CODES[result.verdict]
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return _EXIT_CODES[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
