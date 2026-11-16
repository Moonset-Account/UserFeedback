# Self-scan baseline

The Scanner repository necessarily contains provider token formats and synthetic positive fixtures that the Scanner detects. CI therefore uses a strict fingerprint baseline rather than ignoring all of `src/` or `tests/`.

## Current contract

- baseline schema: `coderisktools.scanner.baseline`, version 1;
- unique expected fingerprints: 58;
- values stored in the baseline: SHA-256 fingerprints only;
- expected CI state: zero unsuppressed findings, all 58 fingerprints matched, zero stale fingerprints;
- scope: source-backed pattern literals and synthetic test fixtures;
- prohibited: adding a real credential to the baseline.

A fingerprint binds the stable rule ID, normalized repository-relative path and matched evidence. Moving or changing a fixture creates reviewable drift.

## CI verification

```bash
secret-scanner scan \
  --dir . \
  --recursive \
  --profile secrets-only \
  --no-config-check \
  --baseline .coderisktools-baseline.json \
  --format json \
  --output /tmp/coderisktools-self-scan.json
```

The command must exit `0`. The JSON summary must report:

- `findings = []`;
- `baseline_matched = 58`;
- `baseline_stale = 0`.

## Controlled regeneration

Never overwrite the committed baseline directly. Generate a candidate in a clean checkout:

```bash
secret-scanner scan \
  --dir . \
  --recursive \
  --profile secrets-only \
  --no-config-check \
  --format json \
  --output /tmp/coderisktools-baseline-review.json \
  --write-baseline /tmp/coderisktools-baseline-candidate.json
```

Before replacing the committed file:

1. review every new finding by source path, rule ID and synthetic-fixture purpose;
2. verify no live credential, private finding, customer artifact or proprietary MCPwatch/Firewall material is present;
3. confirm removals are expected rather than accidental detection loss;
4. run the complete test suite;
5. rerun self-scan against the candidate and require zero new and zero stale fingerprints;
6. require independent review in the pull request.

The baseline is test infrastructure, not proof that the repository contains no secrets. Release review must also inspect the tracked tree, Git history and built wheel contents.
