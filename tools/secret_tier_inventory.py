"""Report secret detector tier counts without inflating unverified tiers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES


def build_report() -> dict:
    stable = sum(rule.kind == "secret" for rule in DEFAULT_DETECTION_RULES)
    return {
        "schema": "coderisktools.secret-tier-inventory",
        "version": 1,
        "tiers": {
            "stable": stable,
            "provisional": 6,
            "contextual_native": sum(rule.kind == "policy" for rule in DEFAULT_CONTEXT_RULES),
            "contextual_external_pack": 28,
        },
        "targets": {"stable_core": 300, "all_secret_tiers": 300},
        "shortfall": {
            "stable_core": 300 - stable,
            "all_secret_tiers_excluding_context_policy": 300 - stable - 28,
        },
        "policy": "Do not count contextual or provisional rules as stable core without qualification.",
        "source_lock": "66924ea",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(build_report(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
