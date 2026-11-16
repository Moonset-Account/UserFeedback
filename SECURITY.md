# Security policy

## Supported versions

The latest tagged `3.x` release is supported.

## Reporting a vulnerability

Use GitHub Private Vulnerability Reporting from this repository's Security tab for vulnerability reports. Do not open a public issue for a vulnerability that would expose a live credential, bypass detail, exploitable payload or private customer data.

For non-sensitive bugs and reproducible public problems, use GitHub Issues without including secrets, private code or customer data.

Do not send live credentials. Use synthetic values and redact paths or customer identifiers.

## Response target

No public response-time SLA is currently claimed.

## Scope

Security reports may cover:

- unsafe path handling or symlink traversal;
- unbounded input or denial-of-service behavior;
- report redaction failures;
- command or environment injection;
- unexpected network access;
- incorrect fail-open behavior;
- release or GitHub Action supply-chain issues.

A scanner false negative or false positive can also be reported, but is not automatically a software vulnerability.
