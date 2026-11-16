# V8n — explicit HTTPS artifact fetch

`vuln-db fetch` performs one explicitly requested HTTPS fetch and writes the raw JSON bytes plus a provenance sidecar:

```bash
python -m src vuln-db fetch \
  --url https://updates.example.test/osv.json \
  --allowed-host updates.example.test \
  --source-id osv \
  --output staging/osv.json
```

The URL must be HTTPS and its hostname, including any redirect destination, must be in the required lowercase allowlist. Response size and timeout are bounded. The raw payload is validated as bounded UTF-8 JSON before an atomic write. Provenance contains URL, final URL, ETag, Last-Modified, content type, payload digest, and its own digest; it never contains payload bytes.

HTTP 304 returns `not_modified` and does not overwrite output. Fetch never imports, stages, activates, prunes, or rolls back a snapshot. It is an explicit network operation and is never part of ordinary scanning.
