"""Measure the real scanner evaluator for Stage 0 performance evidence."""
from __future__ import annotations

import argparse
import copy
import json
import resource
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.patterns import DEFAULT_DETECTION_RULES
from src.scanner import SecretScanner
from src.engine import RuleRegistry


def _diff(lines: list[str]) -> str:
    return "--- a/synthetic.txt\n+++ b/synthetic.txt\n@@ -1,0 +1,%d @@\n%s" % (len(lines), "".join(f"+{line}\n" for line in lines))


def _scanner(rule_count: int) -> SecretScanner:
    scanner = SecretScanner(config_check=False)
    if rule_count > len(scanner.patterns):
        source = list(scanner.patterns)
        copies = []
        index = 0
        while len(source) + len(copies) < rule_count:
            rule = copy.copy(source[index % len(source)])
            rule.rule_id = f"CRT-SEC-BENCH-{len(copies):04d}"
            copies.append(rule)
            index += 1
        scanner.patterns.extend(copies[: rule_count - len(source)])
        scanner.registry = RuleRegistry(scanner.patterns)
    return scanner


def _cases() -> dict[str, tuple[int, list[str]]]:
    clean = [f"ordinary application line {i} with no credentials" for i in range(1000)]
    mixed = [
        "from service import handler",
        "request = build_request(payload)",
        "permissions: read",
        "database = config.get('database')",
    ] * 250
    worst = ["a" * 4096 for _ in range(1000)]
    four_mib_lines = []
    total = 0
    while total < 4 * 1024 * 1024 - 8192:
        line = "safe synthetic content " + ("x" * 4000)
        four_mib_lines.append(line)
        total += len(line) + 2
    return {
        "clean_182": (182, clean),
        "mixed_182": (182, mixed),
        "worst_regex_182": (182, worst),
        "projected_1500_clean": (1500, clean),
        "projected_1500_mixed": (1500, mixed),
        "diff_4mib_182": (182, four_mib_lines),
    }


def _measure(name: str, rule_count: int, lines: list[str], iterations: int) -> dict:
    scanner = _scanner(rule_count)
    diff = _diff(lines)
    durations = []
    findings = []
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for _ in range(iterations):
        started = time.perf_counter()
        result = scanner.scan_diff_text(diff, source=f"benchmark:{name}")
        durations.append(time.perf_counter() - started)
        findings.append(result.summary["total_findings"])
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    ordered = sorted(durations)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return {
        "case": name,
        "iterations": iterations,
        "active_rules": len(scanner.patterns),
        "line_count": len(lines),
        "input_bytes": len(diff.encode("utf-8")),
        "candidate_rule_checks_upper_bound": len(lines) * len(scanner.patterns),
        "fallback_rule_count": sum(not rule.file_globs for rule in scanner.patterns),
        "findings_per_run": sorted(set(findings)),
        "wall_seconds": {
            "median": statistics.median(durations),
            "p95": ordered[p95_index],
            "max": max(durations),
        },
        "max_rss_kib_delta": max(0, rss_after - rss_before),
    }


def build_report(iterations: int = 3) -> dict:
    results = [_measure(name, count, lines, iterations) for name, (count, lines) in _cases().items()]
    return {
        "schema": "coderisktools.performance-baseline",
        "version": 1,
        "engine": "real SecretScanner.scan_diff_text",
        "python": __import__("platform").python_version(),
        "platform": __import__("platform").platform(),
        "iterations": iterations,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(build_report(args.iterations), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
