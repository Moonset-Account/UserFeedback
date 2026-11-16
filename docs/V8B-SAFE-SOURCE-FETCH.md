# V8b — safe HTTPS source fetching

V8b adds the network boundary for already staged updater inputs. `fetch_json_artifact()` returns bounded bytes and transport metadata; it does not parse, stage, activate, or mutate the vulnerability database.

Security contract:

- HTTPS only;
- no URL userinfo;
- exact lowercase hostname allowlist;
- default redirect handler validates every redirect destination against the same HTTPS/allowlist policy;
- final response URL is validated even with an injected/test opener;
- `Content-Length` is rejected when it exceeds the byte limit;
- streamed reads fail closed when the limit is exceeded;
- timeout is explicit and positive;
- transport errors become `ConnectionError`;
- ETag, Last-Modified, Content-Type, requested URL and final URL are returned as metadata.

The caller must pass the returned bytes to the V8a staging contract. No automatic retry, conditional request, archive extraction, activation, rollback, or CLI is included in this batch.
