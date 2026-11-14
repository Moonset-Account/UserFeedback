#!/usr/bin/env python3
"""CLI entry point: post-rotation handshake anomaly alerting.

Usage:
    python3 app.py --input records.jsonl --policy policy.json [--output report.json]

Reads a gateway handshake JSONL export and a service fingerprint policy,
then prints (or writes) a JSON report with alerts, evaluated windows and the
observation-quality report.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handshake_analyzer import analyze_file, load_policy  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Alert on anomalous public-key fingerprints in gateway "
                    "handshake JSONL exports after a key rotation."
    )
    parser.add_argument("--input", required=True,
                        help="path to the gateway handshake JSONL export")
    parser.add_argument("--policy", required=True,
                        help="path to the service fingerprint policy JSON")
    parser.add_argument("--output",
                        help="write the JSON report here instead of stdout")
    args = parser.parse_args(argv)

    try:
        policy = load_policy(args.policy)
        report = analyze_file(args.input, policy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("error: %s" % (exc,), file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
