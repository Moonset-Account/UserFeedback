# V8a — safe offline updater staging

V8a introduces a narrow staging contract for vulnerability source artifacts.

`stage_json_artifact()` accepts already acquired UTF-8 JSON bytes and writes a deterministic envelope atomically. It performs no network access, subprocess execution, package-manager invocation, database activation, or repository-code execution.

The envelope contains:

- schema and version;
- validated lowercase `source_id`;
- original byte size and SHA-256;
- canonical payload SHA-256;
- top-level record count;
- canonical JSON payload.

Safety properties:

- configurable byte and record limits;
- object/array root only;
- invalid input is rejected before destination creation;
- staging uses a temporary file, `fsync`, and `os.replace`;
- verification rejects symlinks, malformed envelopes, unsupported schemas, count mismatches, and payload-hash tampering.

This batch intentionally does not implement downloading, domain allowlists, archive extraction, active snapshot replacement, rollback, or a CLI. Those are separate V8 batches.
