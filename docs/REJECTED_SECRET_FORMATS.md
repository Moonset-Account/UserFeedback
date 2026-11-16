# Rejected Secret Formats

This document records candidates that are intentionally not promoted to stable provider-specific detectors. Rejection is a quality decision, not a claim that the credential is harmless or undetectable with context-aware tooling.

## Datadog — API key and application key

Status: `REJECTED_TOO_GENERIC`

Reason: Official documentation confirms the credentials but the standalone values are hexadecimal and do not expose a sufficiently unique provider prefix or structure.

Collision risk: hashes, UUID fragments, package integrity values, and unrelated hexadecimal credentials.

Sources:

- https://docs.datadoghq.com/account_management/api-app-keys/

## PagerDuty — REST API token

Status: `REJECTED_NO_OFFICIAL_FORMAT`

Reason: Official API authentication documentation describes the credential and header usage but does not provide a sufficiently distinctive standalone token format for a low-noise stable regex.

Collision risk: opaque alphanumeric values and generic authorization material.

Sources:

- https://developer.pagerduty.com/docs/ZG9jOjM0MDI5NTc-rest-api-authentication

## LaunchDarkly — access token

Status: `REJECTED_TOO_GENERIC`

Reason: The `api-` marker is too broad and the remaining token body is not sufficiently constrained by provider-specific structure.

Collision risk: generic API labels, documentation examples, and unrelated `api-` identifiers.

Sources:

- https://launchdarkly.com/docs/api

## Atlassian — API token

Status: `REJECTED_NO_OFFICIAL_FORMAT`

Reason: Atlassian documents API-token authentication but does not expose a stable provider-specific standalone prefix and exact body format suitable for a low-noise detector.

Collision risk: opaque account tokens and generic Base64-like values.

Sources:

- https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/

## Microsoft Azure DevOps — personal access token

Status: `REJECTED_TOO_GENERIC`

Reason: The documented PAT is an opaque value without a safe provider-specific standalone prefix. Detection would require broad contextual assignment matching.

Collision risk: generic Base64-like values and unrelated opaque credentials.

Sources:

- https://learn.microsoft.com/en-us/azure/devops/organizations/accounts/use-personal-access-tokens-to-authenticate

## DigitalOcean Spaces — access key pair

Status: `REJECTED_NO_OFFICIAL_FORMAT`

Reason: Official documentation confirms an S3-compatible key pair, but does not provide a unique DigitalOcean-only access-key marker. It is intentionally not counted as a DigitalOcean-specific stable detector.

Collision risk: AWS/S3-compatible access keys and generic cloud credentials.

Sources:

- https://docs.digitalocean.com/products/spaces/how-to/manage-access/
- https://docs.digitalocean.com/reference/api/reference/spaces-keys/

## Generic JWT, UUID, Base64 and hexadecimal credentials

Status: `REJECTED_TOO_GENERIC`

Reason: A generic encoding or container is not provider provenance. Such values require assignment, path, provider keyword, or semantic context and are not added as standalone stable provider detectors.

Decision: Keep provider-specific rules narrow; do not inflate stable coverage with generic classifiers.
