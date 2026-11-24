"""Command-line entry point: ``python3 -m authorization_checker payload.json``."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .engine import evaluate_authorization
from .models import json_safe


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage: python3 -m authorization_checker <synthetic-request.json>\n"
            "Reads one local JSON payload and writes the decision as JSON."
        )
        return 0 if args else 2

    path = Path(args[0])
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    decision = evaluate_authorization(payload)
    print(json.dumps(json_safe(decision.to_dict()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
