# Stage 6 batch — CRT-SEC-183 Azure DevOps PAT v2

Date: 2026-07-19

## Added

- `CRT-SEC-183` — `AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN_V2`
- Microsoft documents the new PAT as exactly 84 alphanumeric characters with a fixed `AZDO` marker at the documented position.
- Official sources:
  - https://learn.microsoft.com/en-us/purview/sit-defn-azure-devops-personal-access-token
  - https://learn.microsoft.com/en-us/azure/devops/release-notes/2024/sprint-241-update

## Why stable

The format is provider-specific and structurally constrained: fixed total length, restricted alphanumeric alphabet, and a fixed marker. It is separate from existing Azure storage-account-key and SAS-signature detectors.

## Verification

- Focused detector tests: 9 passed.
- Full regression: 389 passed, 1 skipped.
- Optimized regression (`python -O`): 389 passed, 1 skipped.
- `compileall`: passed.
- Golden corpus regenerated: 230 covered / 230 expected / 0 unreachable.
- Source pack regenerated with `vendor-documentation` provenance.
- Source-pack determinism and inventory gates run after regeneration.

## Counts

- native: 230
- line: 224
- context: 6
- stable: 182
- stable shortfall: 118
