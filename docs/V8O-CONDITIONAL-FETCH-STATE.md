# V8o — conditional fetch state

`vuln-db fetch` accepts an explicit provenance sidecar as conditional request state:

```bash
python -m src vuln-db fetch \
  --url https://updates.example.test/osv.json \
  --allowed-host updates.example.test \
  --source-id osv \
  --output staging/osv.json \
  --conditions staging/osv.json.provenance.json
```

The sidecar must be a verified `coderisktools.vulnerability.source-provenance` document. Only its ETag and Last-Modified values are used; payload bytes and arbitrary fields are never sent as request headers. Invalid, tampered, symlink, or non-JSON condition files are rejected before network access.
