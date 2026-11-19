"""Command line entry point for the naming-history index.

Usage::

    python -m naming_history scenario.json --seq 3 --id f1
    python -m naming_history scenario.json --seq 3
    python -m naming_history scenario.json --list-events

The scenario file contains the initial tree and the ordered event stream::

    {
      "initial": {
        "objects": {"root": "dir", "proj": "dir", "f1": "file"},
        "entries": [
          {"dir": "root", "name": "proj", "id": "proj"},
          {"dir": "proj", "name": "a.txt", "id": "f1"}
        ]
      },
      "events": [
        {"op": "rename", "from": {"dir": "root", "name": "proj"},
                        "to":   {"dir": "root", "name": "docs"}}
      ]
    }

Sequence ``0`` denotes the initial tree; sequence ``k`` denotes the tree
immediately after the ``k``-th event was committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .index import NamingHistoryIndex
from .model import (
    InvalidEventError,
    InvalidQueryError,
    NamingHistoryError,
)
from .tree import build_initial_namespace, load_json_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m naming_history",
        description="Query complete paths of a file object at a sequence "
        "number in a naming history.",
    )
    parser.add_argument("scenario", help="path to the scenario JSON file")
    parser.add_argument(
        "--seq",
        type=int,
        default=None,
        help="sequence number to query (0 = initial tree, k = after the "
        "k-th event)",
    )
    parser.add_argument(
        "--id",
        dest="obj_id",
        default=None,
        help="object id whose paths should be printed",
    )
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="print the committed event stream and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of plain lines",
    )
    return parser


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        scenario = load_json_file(args.scenario)
    except OSError as exc:
        print("error: cannot read scenario: %s" % exc, file=sys.stderr)
        return 2
    except ValueError as exc:
        print("error: scenario is not valid JSON: %s" % exc, file=sys.stderr)
        return 2

    if not isinstance(scenario, dict):
        print("error: scenario must be a JSON object", file=sys.stderr)
        return 2

    try:
        initial = build_initial_namespace(scenario.get("initial", {}))
        index = NamingHistoryIndex(initial)
        events = scenario.get("events", [])
        if not isinstance(events, list):
            raise InvalidEventError("'events' must be a list")
        index.apply_events(events)

        if args.list_events:
            payload = [
                {"seq": seq, "event": event}
                for seq, event in enumerate(events, start=1)
            ]
            _emit(payload, args.json)
            return 0

        if args.seq is None:
            if args.json:
                _emit(
                    {
                        "committed_events": index.committed_events,
                        "last_seq": index.last_seq,
                    },
                    True,
                )
            else:
                print("committed_events=%d" % index.committed_events)
                print("last_seq=%d" % index.last_seq)
            return 0

        if args.obj_id is None:
            paths = index.all_paths_at(args.seq)
            _emit(paths, args.json)
            return 0

        paths = index.paths_at(args.seq, args.obj_id)
        _emit(paths, args.json)
        return 0

    except InvalidQueryError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except NamingHistoryError as exc:
        print("error: rejected event or invalid tree: %s" % exc, file=sys.stderr)
        return 1


def _emit(payload, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(payload, list):
        if not payload:
            print("(no paths)")
            return
        for item in payload:
            if isinstance(item, dict):
                print(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                print(item)
        return
    if isinstance(payload, dict):
        for obj in sorted(payload):
            for path in payload[obj]:
                print("%s\t%s" % (obj, path))
        return
    print(payload)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
