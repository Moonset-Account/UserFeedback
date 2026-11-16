# Stage 6 batch — CRT-SEC-182 Cloudinary

Date: 2026-07-19

## Implemented

- `CRT-SEC-182` — `CLOUDINARY_API_CREDENTIAL_URL`
- Detects the documented Cloudinary environment URL:
  `cloudinary://<15-digit-api-key>:<27-character-api-secret>@<cloud-name>`
- Official source: https://cloudinary.com/documentation/cloudinary_cli
- Source-pack provenance: `vendor-documentation`

## Evidence and gates

- Positive fixture: official-shape Cloudinary credential URL.
- Negative fixtures: wrong scheme, wrong API-key length, wrong API-secret length, missing cloud name, placeholders.
- Golden corpus regenerated: 229 covered / 229 expected / 0 unreachable.
- Source pack regenerated and deterministic.
- Full regression: 380 passed, 1 skipped.
- Optimized regression (`python -O`): 380 passed, 1 skipped.
- `compileall`: passed.
- Inventory: 229 native, 223 line, 6 context.
- Stable tier: 181; shortfall to 300: 119.

A recursive self-scan without the existing baseline reports fixture values by design; it was not used as a zero-finding baseline claim.
