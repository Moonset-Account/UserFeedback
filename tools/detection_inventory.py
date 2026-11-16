"""Generate the deterministic Stage 0 native detector inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES


SOURCE_FILES = ("src/patterns.py", "src/rulepacks.py")
CONTAINER_KINDS = {"CONTAINER", "DOCKER", "COMPOSE", "K8S", "KUBERNETES"}


def _git_commit() -> str:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
    ).strip()
    if not commit:
        raise RuntimeError("git returned an empty commit")
    return commit


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _family_id(category: str, domain: str) -> str:
    if domain == "containers-kubernetes":
        return "FAM-CONTAINERS-KUBERNETES"
    return {
        "secret": "FAM-SECRET",
        "ci": "FAM-CI",
        "iac": "FAM-IAC-CLOUD",
        "supply-chain": "FAM-SUPPLY-CHAIN",
        "ai-agent": "FAM-AI-MCP",
    }.get(category, f"FAM-{category.upper().replace('-', '_')}")


def _domain(rule: Any) -> str:
    if rule.category == "iac":
        name = rule.name.upper()
        return "containers-kubernetes" if any(token in name for token in CONTAINER_KINDS) else "iac-cloud"
    return {
        "secret": "secrets-credentials",
        "ci": "ci-cd",
        "supply-chain": "supply-chain",
        "ai-agent": "ai-agent-mcp",
    }.get(rule.category, rule.category)


def _rule_entry(rule: Any, engine: str) -> dict[str, Any]:
    domain = _domain(rule)
    return {
        "detector_id": rule.rule_id,
        "family_id": _family_id(rule.category, domain),
        "engine": engine,
        "name": rule.name,
        "category": rule.category,
        "domain": domain,
        "kind": rule.kind,
    }


def build_inventory() -> dict[str, Any]:
    line_rules = list(DEFAULT_DETECTION_RULES)
    context_rules = list(DEFAULT_CONTEXT_RULES)
    all_rules = line_rules + context_rules
    ids = [rule.rule_id for rule in all_rules]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate detector IDs in the real registry")
    if any(not isinstance(rule.rule_id, str) or not rule.rule_id.startswith("CRT-") for rule in all_rules):
        raise ValueError("malformed detector ID in the real registry")

    entries = sorted(
        [_rule_entry(rule, "line") for rule in line_rules]
        + [_rule_entry(rule, "context") for rule in context_rules],
        key=lambda item: item["detector_id"],
    )
    category_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    for entry in entries:
        category_counts[entry["category"]] = category_counts.get(entry["category"], 0) + 1
        domain_counts[entry["domain"]] = domain_counts.get(entry["domain"], 0) + 1

    return {
        "schema": "coderisktools.detection-inventory",
        "version": 1,
        "source_commit": _git_commit(),
        "source_files": {name: _sha256(REPO_ROOT / name) for name in SOURCE_FILES},
        "excluded_generic_classifiers": [
            "ENV_CONFIG", "AUTH_CONFIG", "CI_CONFIG", "INFRA_CONFIG", "SECURITY_CONFIG",
        ],
        "counts": {
            "native_rule_count": len(entries),
            "line_rule_count": len(line_rules),
            "context_rule_count": len(context_rules),
            "category": dict(sorted(category_counts.items())),
            "domain": dict(sorted(domain_counts.items())),
            "infrastructure_partition": {
                "I0_iac_cloud": domain_counts.get("iac-cloud", 0),
                "C0_containers_kubernetes": domain_counts.get("containers-kubernetes", 0),
            },
        },
        "families": sorted(
            ({"family_id": family_id, "domain": domain} for family_id, domain in {
                (item["family_id"], item["domain"]) for item in entries
            }),
            key=lambda item: item["family_id"],
        ),
        "detectors": entries,
    }


def write_inventory(path: str) -> None:
    document = build_inventory()
    output = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    Path(path).write_text(output, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_inventory(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
