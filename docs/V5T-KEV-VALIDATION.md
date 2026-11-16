# V5t — Strict CISA KEV validation

## Validation contract

`parse_kev_record()` now validates:

- required KEV text fields remain non-empty strings;
- `dateAdded` and `dueDate` use exact `YYYY-MM-DD` syntax;
- both dates are real calendar dates;
- `knownRansomwareCampaignUse` is `Known`, `Unknown`, `Yes`, `No`, a boolean, or absent;
- optional `notes` is a string when present.

`import_kev_json()` reports malformed JSON through `ImportStats.errors` instead of leaking `JSONDecodeError` to callers.

The importer remains offline and preserves exact advisory matching semantics.
