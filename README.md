# Deployment Secret-Reference Resolver

Turns one deployment's environment config, per-service config and secret
reference catalog into a **per-service injection plan**: which variable each
service receives, which reference and version it points at, and which config
layer supplied it.

It deliberately handles **only reference identifiers and versions**. No secret
material is read, stored, resolved or emitted anywhere in this project.

## Why it exists

Before rolling out, deployment staff need to know which services will receive a
variable of the *same name* but from a *different source*. This tool answers
that up front, and it blocks only the service plans that are genuinely
unresolvable — one broken service never erases the results for the rest.

## Requirements

- Python 3 standard library only (`>=3.9`; developed and verified on 3.11).
- No `pip`, `venv`, `pytest` or third-party packages.

## Local entry points

```bash
# resolve a deployment (JSON output, exit code reflects executability)
/usr/local/bin/python3 app.py \
    --environment samples/prod/environment.json \
    --services    samples/prod/services.json \
    --catalog     samples/prod/catalog.json

# same run, human-readable
/usr/local/bin/python3 app.py \
    --environment samples/prod/environment.json \
    --services    samples/prod/services.json \
    --catalog     samples/prod/catalog.json \
    --format text

# full local verification: unit suite + delivered sample + boundary assertions
/usr/local/bin/python3 tools/local_verify.py
```

`tools/local_verify.py` exits `0` only when **all** of the following hold, and
prints the actual evidence for each:

- `unittest.defaultTestLoader.discover("tests")` reported `countTestCases() >= 1`;
- the suite actually ran at least one test and at least one non-skipped test;
- zero failures and zero errors;
- every delivered sample/boundary assertion passed.

Discovering zero tests, running zero tests, skipping everything, raising, or
reporting any failure all produce a non-zero exit. It runs under a 60-second
internal deadline and normally completes in under one second.

## Input format

Three JSON documents, each validated strictly (unknown keys are rejected).

### 1. Environment file (`--environment`)

Deployment environment plus the two non-service-specific config layers.

```json
{
  "environment": "prod",
  "common": [
    {"variable": "DB_PASSWORD", "ref": "shared-db-password"},
    {"variable": "LOG_SINK_KEY", "ref": "log-sink-key", "required": true}
  ],
  "environment_overrides": [
    {"variable": "DB_PASSWORD", "ref": "shared-db-password", "version": 3}
  ]
}
```

A binding has these fields:

| field      | required | meaning                                                     |
|------------|----------|-------------------------------------------------------------|
| `variable` | yes      | environment variable name injected into the service         |
| `ref`      | yes      | secret reference identifier (a catalog key, never a secret) |
| `version`  | no       | integer version pin; omitted means "catalog default"        |
| `required` | no       | defaults to `true`; `false` makes an unresolved ref a warning |

### 2. Services file (`--services`)

The services in this deployment and their per-service overrides.

```json
{
  "services": ["api", "worker", "billing"],
  "service_overrides": {
    "api": [{"variable": "API_SIGNING_KEY", "ref": "api-signing-key-legacy"}],
    "worker": [{"variable": "DB_PASSWORD", "ref": "shared-db-password", "version": 4}]
  }
}
```

`service_overrides` may only target a service listed in `services`, otherwise
the inputs are rejected.

### 3. Catalog file (`--catalog`)

Registers which references exist for which environment/service identity, with
their declared versions.

```json
{
  "environment": "prod",
  "services": ["api", "worker", "billing"],
  "references": [
    {"service": "api", "ref": "shared-db-password", "versions": [1,2,3,4], "current": 3},
    {"service": "api", "ref": "api-signing-key", "versions": [1,2], "current": 2},
    {"service": "api", "ref": "api-signing-key-v2", "alias_of": {"ref": "api-signing-key"}},
    {"service": "api", "ref": "api-signing-key-legacy", "alias_of": {"ref": "api-signing-key-v2"}},
    {"environment": "staging", "service": "api", "ref": "staging-only", "versions": [1], "current": 1}
  ]
}
```

An entry is either a **leaf** (`versions`, optional `current`) or an **alias**
(`alias_of`, no versions). `environment` defaults to the catalog environment and
`service`/`environment` inside `alias_of` default to the entry's own identity.

## Resolution semantics

### Layer precedence

For each service, layers are applied in this order (later wins):

1. `common`
2. `environment_overrides`
3. `service_overrides[<service>]`

The winning layer for each variable is reported as `source_layer`, and every
layer it shadowed is listed under `overrides`.

### Same-layer conflicts

If a variable is declared more than once inside one layer, that is a
`layer_conflict` and is always reported with all competing declarations.

- A conflict in the layer that **wins** for that variable blocks the service.
- A conflict in a fully **shadowed** lower layer is still reported (under
  `shadowed_conflicts`) but does not block, because it cannot change the plan.

### Reference identity

A reference is only valid under the deployment's environment and the
requesting service's identity. A reference that exists elsewhere fails with:

- `cross_environment` — registered under another environment,
- `identity_mismatch` — registered under another service,
- `not_registered` — not registered at all.

### Aliases

An alias points at another reference. Chains are followed to a leaf, and:

- `alias_cycle` — a loop; the full traversal is reported and closed on the
  repeated node,
- `identity_mismatch` — the target leaves the requesting service's identity,
- `cross_environment` — the target leaves the deployment environment,
- `dangling_alias` — the target is not registered.

When a chain is traversed, `resolution_chain` lists every
`{environment, service, ref, version?}` step, and error messages spell the chain
out in full (e.g. `prod/svc/one -> prod/svc/two -> prod/svc/one`).

### Versions

- An explicit `version` must be among the catalog's declared versions, otherwise
  `version_unavailable`.
- Without a version pin, `current` is used when present, otherwise the highest
  declared version.
- The rule applied is reported as `version_rule`
  (`explicit` | `catalog_current` | `catalog_highest`).

## Output semantics

JSON output (`--format json`, the default) has this shape:

```json
{
  "environment": "prod",
  "executable": true,
  "services": [
    {
      "service": "api",
      "executable": true,
      "variables": [
        {"variable": "API_SIGNING_KEY", "source_layer": "service",
         "required": true, "ref": "api-signing-key-legacy",
         "status": "resolved", "resolved_ref": "api-signing-key",
         "version": 2, "version_rule": "catalog_current", "aliased": true,
         "resolution_chain": [
           {"environment": "prod", "service": "api", "ref": "api-signing-key-legacy"},
           {"environment": "prod", "service": "api", "ref": "api-signing-key-v2"},
           {"environment": "prod", "service": "api", "ref": "api-signing-key"}
         ]}
      ],
      "unresolved": [],
      "conflicts": [],
      "shadowed_conflicts": [],
      "diagnostics": [],
      "summary": {"resolved": 1, "unresolved": 0, "blocking": 0,
                  "conflicts": 0, "shadowed_conflicts": 0}
    }
  ],
  "same_name_variables": [],
  "same_name_collisions": [],
  "summary": {"services": 1, "executable_services": 1,
              "blocked_services": 0, "same_name_collisions": 0}
}
```

Key semantics:

- **`executable`** per service is `false` when any *required* variable is
  unresolved or blocked by a same-layer conflict. An unresolved `required:
  false` binding is reported in `unresolved` with `blocking: false` and does not
  block.
- **Per-service independence**: a blocked service never prevents other services
  from being fully resolved and reported.
- **`same_name_variables`** lists every variable name that appears for more than
  one service, with each service's resolved ref, version and layer, and a
  `uniform` flag. **`same_name_collisions`** contains only the non-uniform ones.
  This is the pre-rollout answer to "which services get a same-named secret from
  a different source".

Exit codes:

| code | meaning                                                          |
|------|------------------------------------------------------------------|
| `0`  | every service plan is executable                                 |
| `1`  | inputs are valid but at least one service plan is not executable |
| `2`  | the input documents are missing, malformed or inconsistent       |

Exit `2` prints `{"error": "invalid_input", "message": …, "path": …}` and is
reserved for problems with the documents themselves, not with reference
resolution.

## Verification mapping

`tools/local_verify.py` runs both the discovered unit suite and a fixed set of
delivered assertions, printing `expected` / `actual` for each. The `case_id`
values are stable:

| case_id                              | what it proves                                              |
|--------------------------------------|-------------------------------------------------------------|
| `SAMPLE-EXIT`                        | shipped prod sample exits 0                                 |
| `SAMPLE-JSON`                        | sample output parses as JSON                                |
| `SAMPLE-ENV-OVERRIDE`                | environment layer beats common for all services             |
| `SAMPLE-SVC-OVERRIDE`                | service layer beats environment                             |
| `SAMPLE-ALIAS-CHAIN`                 | two-hop alias resolves to the base ref and version          |
| `SAMPLE-OUTPUT-FIELDS`               | each variable carries name, ref, version and layer          |
| `SAMPLE-SAME-NAME`                   | differing sources for one name are reported                 |
| `SAMPLE-SAME-NAME-DIFF-VERSION`      | one ref name pinned to different versions is reported       |
| `EDGE-ALIAS-CHAIN`                   | multi-hop alias chain order                                 |
| `EDGE-ALIAS-CYCLE`                   | cycle reports a closed full chain                           |
| `EDGE-CROSS-ENV`                     | cross-environment alias names both endpoints                |
| `EDGE-LAYER-CONFLICT`                | same-layer duplicate blocks every affected service          |
| `EDGE-SHADOWED-CONFLICT`             | shadowed duplicate is warned about, not blocking            |
| `EDGE-SERVICE-INDEPENDENCE`          | a blocked service keeps other services' results             |
| `EDGE-OPTIONAL-UNRESOLVED`           | `required: false` unresolved does not block                 |
| `EDGE-INPUT-ERROR`                   | unreadable inputs exit 2 with `invalid_input`               |

## Limitations

- Output is identifiers and versions only; no secret value is ever fetched.
- Layer precedence is fixed to common → environment → service; it is not
  configurable.
- No partial-ordering or semver; versions are non-negative integers.
- Service order in output follows the services file.
- A single run covers one environment; cross-environment resolution is always a
  failure by design.
- JSON only; YAML/env-file inputs are out of scope.
