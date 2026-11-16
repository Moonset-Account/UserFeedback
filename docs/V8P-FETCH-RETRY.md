# V8p — bounded fetch retry and backoff

The explicit fetch command supports bounded retries:

```bash
python -m src vuln-db fetch \
  --url https://updates.example.test/osv.json \
  --allowed-host updates.example.test \
  --source-id osv \
  --output staging/osv.json \
  --max-attempts 3 \
  --backoff-seconds 1
```

Defaults preserve the previous behavior: one attempt and no delay. Retries are limited to transport/timeouts and HTTP 408, 429, and 5xx. `Retry-After` is honored when valid, bounded to 300 seconds; otherwise exponential backoff is used. HTTP 4xx validation/authentication errors are not retried. Retries never stage, import, activate, or write partial artifacts.
