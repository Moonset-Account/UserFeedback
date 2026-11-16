# Stage V5b — Offline signed OSV provenance

Status: bounded batch, standard-library runtime, no network

## Verified import

```bash
secret-scanner osv-import \
  --input ./osv-feed.signed.json \
  --db ./vulnerability.sqlite \
  --snapshot-id osv-2025-01 \
  --source-id osv-official-fixture \
  --keyring ./trusted-keyring.json \
  --activate
```

When `--keyring` is supplied, the input must be a signed envelope. The keyring is loaded only from the explicitly supplied local file. There is no key discovery, URL fetching, or network fallback.

## Keyring

The existing strict offline rule keyring contract is reused:

```json
{
  "schema": "coderisktools.rule-keyring",
  "version": 1,
  "keys": {
    "osv-fixture": "64-hex-character-Ed25519-public-key"
  }
}
```

## Signed envelope

```json
{
  "schema": "coderisktools.vulnerability.signed-feed",
  "version": 1,
  "key_id": "osv-fixture",
  "payload": {"vulns": []},
  "signature": "128-lowercase-hex-characters"
}
```

The signature covers canonical JSON of exactly:

```json
{
  "schema": "...",
  "version": 1,
  "key_id": "...",
  "payload": {}
}
```

The signature itself is not included in the signed message. Verification uses the existing pure-Python Ed25519 verifier; no runtime dependency is added.

## Statuses

- `verified` — trusted key and valid Ed25519 signature;
- `unsigned` — ordinary V5a local feed supplied while verification was requested;
- `untrusted` — envelope key ID is absent from the supplied keyring;
- `invalid` — malformed envelope, unsupported version, malformed signature, or failed verification.

Any non-`verified` status with `--keyring` is rejected before target database mutation and cannot create a staged or active snapshot.

Without `--keyring`, V5a unsigned local fixture behavior remains available and is reported as `unsigned`.

## Provenance

Verified status and `signing_key_id` are retained in the snapshot manifest and reconciliation report, together with the exact raw source SHA-256.

## Non-goals

- no remote keyring retrieval;
- no certificate/identity authority;
- no key rotation protocol;
- no claim that a trusted key proves a feed is current or complete;
- no automatic activation.
