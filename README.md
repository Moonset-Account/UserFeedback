# CodeRiskTools Secret Scanner Engine

**Open-source flagship for reviewing secret-like values and risky configuration changes before AI-generated code is merged.**

CodeRiskTools Secret Scanner Engine is MIT licensed, local-first, offline by default, and has no runtime dependencies. It scans unified diffs, staged changes, local directories and bounded Git history without executing target-project code.

> Evidence, not guarantees. A clean result is not proof that code is secure. Findings may include false positives and false negatives. This tool is not a security audit, certification, legal opinion or compliance guarantee.

## What is public here

- Secret Scanner Engine `3.0.1`;
- strict, bounded and fail-closed unified-diff parsing;
- staged-change scanning;
- redacted JSON, Markdown, HTML, SARIF and GitHub output;
- baselines and allowlists;
- signed rule-pack verification support;
- bounded Git-history mode;
- generic offline agent-hook envelopes;
- pre-commit integration;
- composite GitHub Action;
- tests and synthetic fixtures.

## Verified detector coverage

The current public registry contains **299 native detectors**, including **187 stable secret-format detectors**, with 267 line detectors and 32 contextual detectors. The registry includes 73 CI/CD policy detectors. The stable secret set is source-backed and excludes provisional candidates from the stable count. The golden corpus covers all 299 native detectors with deterministic parity.

Stage 8 CI/CD permission-scope coverage is tracked in [`docs/STAGE8_CI_CD_BATCH1_SOURCES.md`](docs/STAGE8_CI_CD_BATCH1_SOURCES.md); workflow-wide write permissions are classified as least-privilege policy reviews, not automatic exploitation claims.

Stage 6 research is tracked in [`docs/STAGE6_SECRET_DETECTOR_BACKLOG.md`](docs/STAGE6_SECRET_DETECTOR_BACKLOG.md); the backlog has been revalidated through PR #49, and opaque, generic and incomplete provider formats remain explicitly excluded from stable counts.

## Product boundary

This public repository contains **only the Scanner flagship**.

Not included and not licensed by this repository:

- **MCPwatch Scanner** — paid proprietary add-on for MCP-focused scanning;
- **AI Change Firewall** — separate paid proprietary product for intent, scope, change budgets, `ALLOW/BLOCKED` controls, receipts, evidence packs and policy workflows.

No MCPwatch Scanner or AI Change Firewall source belongs in this repository. See [Product boundary](docs/PRODUCT_BOUNDARY.md).

## Install from a checkout

Requirements: Python 3.10+. Git is required only for staged/history modes.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --no-deps .
secret-scanner --version
```

## Scan staged changes

```bash
secret-scanner scan --staged --profile balanced --format json
```

## Scan a saved diff

```bash
secret-scanner scan \
  --diff examples/clean.diff \
  --profile strict \
  --format json
```

## Scan a local directory

```bash
secret-scanner scan \
  --dir . \
  --recursive \
  --profile balanced \
  --format json
```

Directory mode skips repository metadata, common dependency/build directories, binary formats and symlinks. Inputs are bounded and regular-file reads are descriptor pinned.

## Exit codes

- `0`: clean or warning-only;
- `1`: secret finding at the configured failure threshold;
- `2`: risky configuration/policy finding without a failing secret;
- `3`: malformed, unsafe or oversized input, or an operational failure.

## Pre-commit

```yaml
repos:
  - repo: https://github.com/9batalion/coderisktools-scanner
    rev: v3.0.1
    hooks:
      - id: coderisktools-secret-scan
```

The hook scans staged Git diff bytes. It disables external diff and text-conversion helpers and does not run project builds or tests.

## GitHub Action

```yaml
name: CodeRiskTools Scanner
on:
  pull_request:
permissions:
  contents: read
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          fetch-depth: 0
      - uses: 9batalion/coderisktools-scanner@v3.0.1
        with:
          profile: balanced
```

The Action analyzes only bounded Git diff bytes. It does not install dependencies from the target repository or execute target-project code.

## Agent-hook envelope

The Scanner can accept strict offline envelopes for generic, Codex-labelled and Claude Code-labelled adapters. This is a documented input contract, not a claim of undocumented vendor-native compatibility. See [Agent hooks](integrations/AI_AGENT_HOOKS.md).

## Security properties

- offline by default;
- no telemetry;
- no runtime dependencies;
- redacted findings by default;
- strict malformed-diff rejection;
- absolute/traversal path rejection;
- bounded diff, file and line inputs;
- non-symlink file reads;
- no external Git diff/textconv helpers in staged/Action paths;
- no target-project code execution.

Optional credential verification is networked, disabled by default and requires explicit per-run consent. Read the CLI help and tests before enabling it.

## Development

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q src tests
```

Contributions must use synthetic fixtures only. Never submit a live credential, private finding, customer repository or proprietary MCPwatch/Firewall source. Changes to controlled provider fixtures must follow the [self-scan baseline procedure](docs/SELF_SCAN_BASELINE.md).

## License

The files in this repository are available under the [MIT License](LICENSE). Product names, website content, MCPwatch Scanner, AI Change Firewall and separately distributed commercial materials are not automatically licensed by this repository.
