# V5c — Exact advisory alias index

## Scope

This bounded batch adds exact, source-backed identity lookup for advisory aliases. It does not perform heuristic correlation or merge records.

## Accepted identifiers

The index accepts bounded identifiers such as:

- CVE IDs;
- GHSA IDs;
- OSV IDs;
- RUSTSEC, PYSEC, and GO IDs;
- other source-native identifiers matching the documented safe identifier grammar.

Identifiers are normalized only by trimming and ASCII case normalization. The original advisory record and source remain authoritative.

## Rules

1. An advisory's native ID resolves to itself.
2. Every explicit alias resolves to its imported advisory ID.
3. Re-importing the same advisory/alias is idempotent.
4. An alias mapping to two different advisory IDs is a conflict.
5. Conflicts are recorded with both advisory IDs and are never silently merged.
6. A conflict lookup returns `ambiguous`, not a confirmed advisory.
7. No similarity, description, package, range, or fixed-version heuristic is used.
8. Alias indexing does not modify existing advisory or occurrence fingerprints.

## Result states

- `exact` — one canonical advisory ID is resolved;
- `ambiguous` — the alias has conflicting advisory IDs;
- `not-found` — no imported exact mapping exists.

## Non-goals

- CVE/GHSA remote downloading;
- NVD enrichment;
- heuristic merge/split;
- replacing source records with a merged record;
- claiming that an alias conflict proves the records describe the same vulnerability.
