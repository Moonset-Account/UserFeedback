"""Command-line runner: read JSONL records, print JSONL output rows.

Usage::

    python3 -m median_window --window 3 data.jsonl
    python3 -m median_window --window 3 --stats data.jsonl

Each input line is a JSON object ``{"partition": ..., "time": ..., "value": ...}``
(or a 3-element array ``[partition, time, value]``).  Output rows are emitted
in input order as ``{"row": n, "partition": ..., "time": ..., "value": ...,
"median": ...}``.  A summary line with structural counters is written to
stderr when ``--stats`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, Iterator

from .window import MedianWindow, WindowError


def _records(path: str) -> Iterator[tuple]:
    stream = sys.stdin if path == "-" else open(path, "r", encoding="utf-8")
    try:
        for lineno, line in enumerate(stream, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WindowError(f"line {lineno}: invalid JSON: {exc}") from exc
            if isinstance(obj, dict):
                missing = {"partition", "time", "value"} - obj.keys()
                if missing:
                    raise WindowError(
                        f"line {lineno}: missing keys {sorted(missing)}"
                    )
                yield obj["partition"], obj["time"], obj["value"]
            elif isinstance(obj, list) and len(obj) == 3:
                yield obj[0], obj[1], obj[2]
            else:
                raise WindowError(
                    f"line {lineno}: expected an object or a 3-element array"
                )
    finally:
        if stream is not sys.stdin:
            stream.close()


def _emit(rows: Iterable, out) -> int:
    count = 0
    for row in rows:
        out.write(
            json.dumps(
                {
                    "row": row.row_number,
                    "partition": row.partition,
                    "time": row.time,
                    "value": row.value,
                    "median": row.median,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="median_window", description=__doc__)
    parser.add_argument("path", nargs="?", default="-", help="JSONL file or -")
    parser.add_argument("--window", required=True, type=int, help="W >= 0")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args(argv)

    try:
        op = MedianWindow(args.window, seed=args.seed)
        _emit(op.push_many(_records(args.path)), sys.stdout)
        op.finish()
    except WindowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.stats:
        print(json.dumps(dict(op.stats()._asdict()), sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
