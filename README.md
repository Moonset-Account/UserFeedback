# secscan

One security scan for any repo. It runs the scanners that apply, merges their output into
a single fingerprinted finding list, hides what you have already accepted, and fails only
on new findings above a threshold. A Claude Code plugin does the part scanners cannot:
reading the code around each finding and checking your diff against the security rules
you wrote down.

| Layer | Tool | Finds |
|---|---|---|
| SAST | [Sighthound](https://github.com/Corgea/Sighthound) (tree-sitter, taint) | injection, SSRF, path traversal, XSS, eval |
| SAST | [Semgrep CE](https://github.com/semgrep/semgrep) (CI, Linux) | the same classes with 2,000+ framework-aware rules |
| Secrets | [gitleaks](https://github.com/gitleaks/gitleaks) | tokens and keys in the tree or the history |
| Supply chain | `npm audit` + [osv-scanner](https://github.com/google/osv-scanner) | known advisories in npm, Python (uv, poetry, Pipfile, PDM, requirements), Go, Cargo, Ruby, PHP lockfiles, deduped |
| Supply chain | [pip-audit](https://github.com/pypa/pip-audit) (CI, or where pip works) | pinned `requirements*.txt` against the PyPI advisory DB, merged with the osv rows |
| Supply chain | built-in lockfile diff | new packages with install scripts, unusual registry hosts, git URLs, fresh publishes |
| Judgment | Claude Code plugin | confirms or dismisses each finding by reading code; checks `.secscan/invariants.md` |

Zero runtime dependencies. Node 20+. Binaries are pinned by version and SHA-256 in
[`tools.json`](tools.json) and verified before install.

## Quick start

```bash
npx --yes github:zapai-inc/secscan init --hook      # detect stack, write config + baseline, install pre-commit hook
npx --yes github:zapai-inc/secscan scan             # what is new since the baseline
npx --yes github:zapai-inc/secscan scan --ci        # fail on new high/critical, markdown report
```

`init` writes three files you commit:

- `.secscan.json`: adapters, excludes, thresholds, suppressions, Semgrep rulesets.
- `.secscan/baseline.json`: fingerprints of every finding you have accepted, with the commit.
- `.secscan/invariants.md`: plain-language rules for this repo that the Claude plugin checks.

Scanner binaries land in `~/.secscan/bin`, or `SECSCAN_TOOLS_DIR` if set. `secscan tools status`
shows what is installed; `secscan tools install` fetches osv-scanner and gitleaks. Sighthound
needs a Rust build (`cargo install --git https://github.com/Corgea/Sighthound --tag 1.0 sighthound`)
and Semgrep runs where pip works (Linux, macOS, WSL); pip-audit installs with `pip install pip-audit`
anywhere. All three are optional locally and always on in CI. A `pyproject.toml` without a lockfile
is reported as a gap: Python supply chain needs `uv.lock`, `poetry.lock` or pinned requirements.

## Where it runs

| Where | Command | Scope | Fails on |
|---|---|---|---|
| Commit hook | installed by `init --hook` | staged files; Sighthound, gitleaks, lockfile diff; ~2 s | a new secret |
| Local | `secscan scan` | whole tree, every installed tool | nothing, unless `--fail-on` |
| CI | reusable workflow | whole tree, all tools, gitleaks history, registry metadata | new high/critical vs baseline |
| Claude Code | `/secscan` | all of the above, plus triage and invariants | your judgment |

Thresholds are per mode in `.secscan.json`:

```json
{ "failOn": { "local": "none", "ci": "high", "hook": "none" }, "hookFailOnSecrets": true }
```

## Baseline and fingerprints

A finding's identity is its tool, rule, file and normalised code snippet, not its line
number. Editing code above a finding does not make it "new"; changing the flagged code
does. Supply-chain findings key on advisory, package, version and lockfile. Secrets key
on a hash of the value, never the value.

`secscan baseline update` accepts the current state. The report lists findings that
disappeared since the baseline so you can prune it.

## Suppressions

Inline, on the line or the line above:

```js
const r = await fetch(url); // secscan-ignore: ssrf url passed assertEgressAllowed() above
```

In config, with an expiry so accepted risk comes back for review:

```json
{ "suppress": [{ "rule": "path-traversal", "path": "scripts/**", "reason": "operator CLI, paths come from flags", "expires": "2027-01-01" }] }
```

## CI

```yaml
# .github/workflows/security.yml
on: [push, pull_request]
jobs:
  security:
    uses: zapai-inc/secscan/.github/workflows/scan.yml@v0.2.2
    permissions:
      contents: read
      pull-requests: write
```

It installs Semgrep, builds Sighthound once and caches it, fetches the pinned binaries,
runs `secscan scan --ci`, writes the report to the job summary, updates one PR comment in
place, uploads `report.md`, `report.json` and `results.sarif`, and fails the job on new
findings above the threshold. Pin the tag.

## Claude Code plugin

```
claude plugin marketplace add zapai-inc/secscan
claude plugin install secscan@zapai
```

Then `/secscan` in any repo. The skill runs the scan, reads the code at every new finding,
checks `.secscan/invariants.md` against the diff, and reports confirmed issues with fixes,
invariant violations, flagged dependency changes, and dismissed findings with reasons.
Set `SECSCAN_HOME` to a local checkout to run from source instead of `npx`.

## Commands

```
secscan scan [path] [--ci|--hook] [--changed[=ref]] [--only a,b]   # adapters: sighthound semgrep npm-audit osv-scanner pip-audit gitleaks [--fail-on sev] [--no-baseline] [--format text|md|json|sarif] [--out dir]
secscan init [path] [--hook]
secscan baseline update|show
secscan deps [--since ref]
secscan tools status|install [name…]
secscan hook install|uninstall
secscan detect [path]
```

## Adding a tool

One file in `src/adapters/` exporting `name`, `category`, `applies(detect)`, `available()`,
`version(bin)`, `scan({ root, bin, config, detect, mode })` and a pure `parse()` that the
tests can drive from a saved sample in `tests/samples/`. Pin the binary in `tools.json`
with its SHA-256. Register it in `src/adapters/index.js`.

## Design notes

- Sighthound findings without a taint source are sink-only pattern hits and are lowered
  one severity notch with `extra.taintFlow: false`, so a "high" from Sighthound means an
  actual source-to-sink flow.
- npm audit and osv-scanner report the same advisories; rows are merged on advisory id and
  package, the osv row wins (exact version, correct fixed-in), and `extra.alsoReportedBy`
  records the other. Dev-only packages are marked from the lockfile.
- No SARIF upload to GitHub Code Scanning by default: it needs Advanced Security on private
  repos. The SARIF file is produced anyway for anything that reads it.
- The fixture in `fixtures/node-vuln` is synthetic and intentionally vulnerable; its secret
  is randomly generated and its dependencies are pinned to versions with known advisories.

## License

MIT. Scanner licenses: Sighthound MIT, Semgrep CE LGPL-2.1 (rules under the Semgrep Rules
License), gitleaks MIT, osv-scanner Apache-2.0.
