# Stage V0 — Existing Contract Inventory

Baseline commit: `90d6091` (`origin/main` after PR #89)
Baseline tests: `573 passed, 1 skipped, 902 subtests passed` in `11.10s`
Python requirement: `>=3.10`; declared runtime dependencies: none.

## Existing public contracts

### `Finding`

Location: `src/scanner.py`

Fields:

- `type`
- `pattern_name`
- `severity`
- `file`
- `line`
- `matched_text`
- `line_content`
- `rule`
- `rule_id`
- `category`
- `confidence`
- `remediation`
- `identity_path`

Fingerprint:

```text
sha256(
  "coderisktools-finding-v1\0" +
  rule_id + "\0" +
  normalized_identity_path + "\0" +
  whitespace_normalized_matched_text
)
```

The public representation is secret-safe: JSON output replaces `matched_text` and `line_content` with `[REDACTED]`.

### `ScanResult`

Location: `src/scanner.py`

Fields include scanner/version/timestamp/input identity, `findings`, `config_changes`, severity thresholds, and baseline counters.

Stable behavior:

- `summary` reports severity, secret, policy, config, and baseline counters;
- `findings` and `config_changes` remain separate collections;
- `exit_code` returns `0` for clean, `1` for failing secret findings, `2` for policy/config findings, and CLI/runtime errors exit via the CLI with `3`.

### Baseline

Location: `src/baseline.py`

- schema: `coderisktools.scanner.baseline`;
- version: `1`;
- root keys are exactly `schema`, `version`, `fingerprints`;
- fingerprints are unique lowercase `sha256:<64 hex>` values;
- bounded to 5 MiB and 100,000 fingerprints;
- loaded from regular non-symlink files;
- written deterministically and atomically as a private artifact.

### SARIF

Location: `src/formatters.py`

- SARIF version: `2.1.0`;
- findings use stable rule IDs and partial fingerprint properties;
- evidence messages contain no matched secret value;
- severity mapping remains critical/high → error, medium → warning, low → note.

### JSON, Markdown, HTML, GitHub output

Location: `src/formatters.py`

The existing output formats are secret/config reports. They must not be silently repurposed as vulnerability reports. A vulnerability formatter requires a separate versioned contract.

### CLI

Location: `src/__main__.py`; executable: `secret-scanner`.

Existing command families:

- `scan`
- `verify`
- `rules install`
- `rules rollback`
- `hook`

There is currently no vulnerability command and no vulnerability database updater. V0 does not add placeholder commands.

## Explicit V0 freeze

The following remain unchanged while vulnerability work starts:

- `Finding` field shape and fingerprint formula;
- `ScanResult` field shape and summary/exit semantics;
- baseline schema/version and fingerprint syntax;
- SARIF 2.1.0 output contract;
- CLI names, existing options, and exit codes;
- offline/no-package-manager/no-analyzed-code-execution guarantees.

## Known boundary

The current self-scan of the repository produced real findings and exited non-zero. That is evidence of current scanner behavior, not evidence of vulnerability coverage and not a clean result. No self-scan findings are being converted into the vulnerability domain.
