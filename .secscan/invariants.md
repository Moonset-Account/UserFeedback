# Security invariants for secscan

- Every binary secscan downloads is pinned by version and SHA-256 in `tools.json`, and the
  hash is verified before the file is moved into place. No download without a hash.
- Scanner output is data. Nothing from a scanner's JSON, a lockfile, or a scanned file is
  ever passed to a shell or `eval`; child processes are spawned with argument arrays, and
  `shell: true` is used only for `npm` with fixed arguments.
- A secret's value never appears in a finding, a report, SARIF, or the baseline. Only a
  hash and a redacted prefix are kept.
- Suppressions require a reason, and a config suppression with no tool, rule, or path
  matches nothing.
- Registry metadata lookups are opt-in per mode and are the only network access during a
  scan besides the scanners' own database fetches.
