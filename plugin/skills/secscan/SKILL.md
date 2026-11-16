---
name: secscan
description: Run a full security scan of the current repo (SAST via Sighthound and Semgrep, secrets via gitleaks, supply chain via npm audit and osv-scanner), triage every new finding by reading the code, check the diff against the repo's security invariants, and report confirmed issues with fixes. Use when asked to scan for vulnerabilities, run SAST, check dependencies, review security before shipping, or when a commit hook or CI job reported secscan findings. Optional argument: a path (default: repo root) or "--changed" to limit SAST findings to files changed since the default branch.
---

# secscan

secscan is a repo-agnostic CLI that runs whichever scanners apply, merges everything into
one fingerprinted finding list, hides what the committed baseline already accepted, and
fails on new findings above a threshold. You add the two things no scanner can do: read
the code around each new finding to confirm or dismiss it, and check the diff against
the plain-language invariants in `.secscan/invariants.md`.

## Run it

```bash
# where the CLI lives; SECSCAN_HOME is set by the plugin install, otherwise use npx
SECSCAN="${SECSCAN_HOME:+node $SECSCAN_HOME/bin/secscan.js}"; SECSCAN="${SECSCAN:-npx --yes github:zapai-inc/secscan}"

$SECSCAN tools status                       # what is installed; `tools install` fetches pinned osv-scanner + gitleaks
$SECSCAN scan --format json --out "$CLAUDE_SCRATCHPAD/secscan" > "$CLAUDE_SCRATCHPAD/secscan/scan.json"
$SECSCAN scan --changed=origin/main         # SAST/secrets only for files changed on this branch
$SECSCAN deps                               # lockfile diff since the baseline commit
```

If the repo has no `.secscan.json`, run `$SECSCAN init` first (it detects the stack,
writes config, an invariants stub, and a baseline from the current state). Tell the user
the baseline accepted N existing findings and that they should review them once.

Exit code 1 means new findings met the threshold. That is information, not an error.

## Triage every new finding by reading code

Open each `newFindings[]` entry's file at its line and decide **confirmed** or
**dismissed**. Write one line of reasoning per finding. Rules of thumb:

- `extra.taintFlow === true` (Sighthound) or a Semgrep rule with `confidence: HIGH`:
  treat as real until the code shows a guard the scanner missed (validation, allow-list,
  parameterised query, path normalisation with a root check).
- `extra.taintFlow === false`: sink-only pattern hit. Real only if the value can come
  from a request, a message, a file upload, or another tenant. Loop indexes, typed
  arrays, `Intl.formatToParts`, and CLI flags in operator scripts are not.
- Secrets: confirmed unless the value is clearly a documented example or a test fixture
  labelled as such. A real secret in history needs rotation, not just deletion.
- Supply chain: confirmed if a fix exists. Mark `extra.dev === true` as lower priority.
  Group rows by package; one upgrade usually clears several advisories.
- Never dismiss for "we trust the caller". Say who the caller is and why it cannot be
  a tenant or an attacker.

## Check the invariants

Read `.secscan/invariants.md`. For each rule, look at the diff (or the files the user
named) for a violation. Report violations as findings with the invariant quoted. This is
the part that catches tenant-scoping bugs and missing signature checks that no scanner sees.

## Report format

1. One line: files scanned, tools that ran, new findings, how many confirmed after triage.
2. **Confirmed** findings, most severe first. Each: `file:line`, the flow in one sentence,
   the impact, and a concrete fix. For dependencies: the package, the upgrade, whether it
   is a major bump.
3. **Invariant violations**, if any, with the rule quoted.
4. **Dependency changes** since baseline that carry a flag (install script, unusual
   registry host, git URL, published under 7 days ago).
5. **Dismissed** findings as a short list with the reason each, so the user can spot-check.
6. Whether to run `secscan baseline update` (only after the user has seen the confirmed list).

Do not paste raw scanner output. Do not include secret values, redacted or not.

## Fixing

If the user asks you to fix: apply the change, re-run the scan on the changed files, and
show that the finding is gone. Add an inline `// secscan-ignore: <rule> <reason>` only
for a confirmed false positive, with a reason a reviewer can verify.

## Suppressions

- Inline: `secscan-ignore[: <rule-id>] <reason>` in a comment on the line or the line above.
- Config: `suppress: [{ rule, path, reason, expires }]` in `.secscan.json`. Prefer an
  `expires` date for accepted risk so it comes back for review.

## Modes and thresholds

- Local: reports, never fails, unless `--fail-on` is given.
- Commit hook (`secscan hook install`): staged files, Sighthound + gitleaks + lockfile
  diff, about two seconds, blocks only on a new secret.
- CI (`--ci`): full history for gitleaks, registry metadata for new packages, fails on
  new high/critical against the committed baseline. Semgrep runs here on Linux.
