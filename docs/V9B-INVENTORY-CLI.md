# V9b — read-only inventory CLI

`vuln inventory` emits a deterministic, versioned JSON inventory without network, subprocesses, package managers, or repository code execution:

```bash
python -m src vuln inventory --root REPOSITORY
```

The report contains components, unresolved dependencies, warnings, and counts. Exact versions are preserved as parsed; unresolved requirements are not promoted to confirmed versions.
