# V8c — conditional fetch and source provenance

V8c extends the V8b fetch boundary without changing V8a's stage envelope or activating any database.

## Conditional requests

`FetchConditions` optionally supplies:

- `If-None-Match` from a prior ETag;
- `If-Modified-Since` from a prior Last-Modified value.

A `304 Not Modified` response becomes an explicit `DownloadedArtifact` with:

- `not_modified=True`;
- `payload=None`;
- validated final URL;
- returned validator metadata.

A normal response remains `not_modified=False` and carries bounded bytes.

## Provenance

`build_source_provenance()` creates deterministic metadata-only provenance containing:

- schema/version;
- source ID;
- downloaded/not-modified status;
- requested and final URL;
- ETag, Last-Modified, Content-Type;
- payload SHA-256 when bytes were downloaded;
- canonical provenance SHA-256.

The provenance object contains no raw payload and does not mutate staging or the active database.

Retries, archive extraction, snapshot activation, rollback, and CLI remain outside this batch.
