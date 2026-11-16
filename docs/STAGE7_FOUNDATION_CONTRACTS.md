# Stage 7 Foundation Contracts

## Baseline

This document freezes the public scanner contracts at Stage 7 start.

- Baseline branch: `main`
- Baseline commit: `f527d7e3cf0e4fa6a887e64d9dcc98d1808eba8a`
- Native detectors: 235 (229 line, 6 context)
- Stable secret detectors: 187
- Golden corpus: 235 covered, 0 unreachable
- Baseline schema: `coderisktools.scanner.baseline` v1

## Finding

`src.scanner.Finding` is a mutable dataclass with the existing fields and defaults. Its `fingerprint` is the existing v1 `sha256:` identity derived from the rule ID, normalized identity path and normalized matched evidence. The fingerprint is used by baseline suppression and is emitted in existing JSON, SARIF and GitHub output. Stage 7 does not alter it.

`identity_path`, when present, is used instead of the display path. Backslashes are normalized to `/`; Windows drive letters are normalized to a lower-case drive letter. The existing v1 identity is intentionally stable across line-number changes; Stage 7 does not alter it. Existing line/path/evidence behavior is frozen by `tests/test_stage7_contract_freeze.py`.

## ConfigChange

`src.scanner.ConfigChange` contains `type`, `file`, `severity`, `change_type` and `description`. Config changes contribute to `ScanResult.summary`, existing formatters and policy exit code 2. They are not baseline-v1 secret findings.

## ScanResult

`src.scanner.ScanResult` retains its existing fields, finding/config-change order, summary counters, baseline counters, timestamp and formatter methods. Existing output schemas and redaction are unchanged.

## Fingerprint v1

The namespace is `sha256:` followed by 64 lower-case hexadecimal characters. Stage 7 observation, fact, delta and incident IDs are separate namespaces and must never replace or be silently mapped to Finding fingerprints.

## Baseline v1

The exact schema is:

```json
{"schema":"coderisktools.scanner.baseline","version":1,"fingerprints":["sha256:<64 hex>"]}
```

Fingerprints are unique, sorted on write, bounded, loaded fail-closed and used only for existing matched/suppressed/stale behavior. Stage 7 delta state is not written into `.coderisktools-baseline.json`.

## Public output formats and exit codes

JSON, SARIF, Markdown, HTML and GitHub annotations preserve their existing fields, rule IDs, redaction and escaping. Existing exit code meanings remain:

- `0`: clean or warning-only;
- `1`: failing secret finding;
- `2`: policy/config finding without a failing secret;
- `3`: malformed, unsafe, oversized or operational error.

Stage 7 internal models are not automatically added to these outputs in this iteration. Any future public additive field requires a separate compatibility review.

## Stage 7 additive models

`Observation` is a redacted raw signal. `Fact` is a normalized semantic fact. `Delta` describes a change between normalized states. `Incident` aggregates explicitly related facts/deltas within a scope. None is a replacement for `Finding`, a baseline-v1 identity, or proof of a complete security incident.

New IDs use independent namespaces: `obs-sha256:`, `fact-sha256:`, `delta-sha256:` and `incident-sha256:`. IDs and serialization are deterministic and independent of collection input order where the model contract requires it.

## Privacy and deterministic behavior

No Stage 7 model stores or serializes raw evidence, `matched_text`, `line_content`, credentials, private roots or arbitrary objects. Metadata is a flat JSON-safe scalar map. Models are immutable and bounded. New model modules perform no I/O, network access, subprocess execution, `eval` or `exec`, and do not import `scanner.py`.

## Integration boundary

The opt-in Stage 7 pipeline may convert legacy findings/config changes into observations, facts, deltas and incidents. It must preserve legacy `ScanResult`, baseline-v1, formatter and exit-code behavior. It must not load or execute code from the scanned project. External observations require explicit caller-provided normalized data.

## Explicit exclusions

This iteration does not add detectors, migrate baseline v1, replace public output schemas, change existing rule IDs, add external network validation, implement taint/AST/MCP analysis, or begin Stage 8.
