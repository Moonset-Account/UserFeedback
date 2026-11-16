# V5d — Source-record provenance and merge-decision ledger

## Source records

Each imported source record is retained with:

- `source_id`;
- native record ID;
- stable source-record fingerprint;
- canonical content digest;
- canonical raw record JSON;
- linked advisory ID.

The stable source-record fingerprint depends only on source ID and native record ID. Content digest identifies a particular source revision. Therefore an updated source record creates history without changing the source-record identity.

## Merge decisions

Merge/split decisions are explicit ledger entries. A decision contains:

- decision type (`merge` or `split`);
- ordered advisory IDs;
- reason;
- correlation rules version;
- provenance references;
- deterministic decision ID.

The ledger records intent and evidence. It does not automatically modify advisory rows, aliases, or fingerprints. Heuristic correlation is out of scope for this batch.

## Safety rules

- duplicate source records are idempotent;
- changed content for the same source/native ID is retained as a new revision;
- source JSON is canonicalized for storage and hashing;
- source data is not overwritten by enrichment;
- merge decisions require an explicit reason and at least one provenance reference;
- advisory fingerprints are unaffected by source revision or ledger writes.
