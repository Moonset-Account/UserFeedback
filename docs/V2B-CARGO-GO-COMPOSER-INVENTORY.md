# Stage V2b — Cargo/Go/Composer Inventory

Status: bounded batch

## Scope

This batch extends the offline inventory with:

- `Cargo.lock` package stanzas;
- `go.mod` `require` declarations;
- `go.sum` checksum rows;
- `composer.lock` production and development packages.

It remains local-only and does not resolve, download, compile, or execute dependencies.

## Exactness rules

- Cargo package stanzas with both `name` and `version` become exact components.
- Go module versions from `go.mod` and checksum rows from `go.sum` become exact components.
- `/go.mod` checksum rows are metadata-only and are ignored as component records.
- Composer `v` prefixes are removed before canonical PURL creation.
- Duplicate component observations are deduplicated by canonical PURL, preserving the first parser observation.

## Parser boundaries

- Cargo uses a bounded parser for `[[package]]` stanzas; it does not claim to be a complete TOML implementation.
- Go parsing handles standard `require` forms and checksum rows, not replace directives or workspace graph resolution.
- Composer parsing accepts JSON package arrays from `packages` and `packages-dev`.

## Safety

- Standard library only.
- No network access.
- No package-manager/toolchain invocation.
- No subprocesses.
- Manifest size and regular-file checks remain inherited from V2a.
- Malformed records become bounded warnings or parser errors handled by `build_inventory` without discarding other manifest results.

## Non-goals

- no vulnerability/advisory matching;
- no dependency graph resolution;
- no SQLite persistence;
- no SBOM import/export;
- no CLI integration;
- no claims that an exact lockfile component is vulnerable without a later matching stage.
