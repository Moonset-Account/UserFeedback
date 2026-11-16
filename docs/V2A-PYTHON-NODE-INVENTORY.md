# Stage V2a — Python/Node Inventory MVP

Status: bounded batch

## Scope

V2a reads local manifests only. It does not execute repository code, invoke package managers, resolve dependencies over the network, or query a vulnerability database.

Supported inputs in this batch:

- `requirements.txt` exact pins and unresolved ranges;
- `package-lock.json` lockfile versions 1, 2, and 3;
- root-level inventory discovery for these files.

## Exactness rules

- Python `==`/`===` pins become exact `Component` records.
- Python ranges, missing versions, unsupported sources, and includes remain unresolved or warnings.
- npm lockfile package versions are exact when the lockfile entry has a non-empty `version`.
- No manifest range is represented as an installed version.
- No CVE or advisory match is produced by inventory alone.

## Safety rules

- Read only regular, non-symlink files.
- Enforce a 5 MiB manifest limit.
- Parse JSON with the standard library.
- Never import or execute `setup.py` or project code.
- Never call `pip`, `npm`, `npx`, `node`, `cargo`, `go`, or any package manager.
- A malformed or unsupported manifest becomes a bounded warning and does not discard valid results from another manifest.
- No network APIs are used.

## Output contracts

`InventoryResult` contains:

- `components` — exact component observations;
- `unresolved` — `UnresolvedDependency` records with ecosystem, name, requirement, reason, and manifest path;
- `warnings` — bounded diagnostic strings without manifest content.

`Component` preserves ecosystem, package name, exact version state, PURL, source type, manifest path, and version confidence.

## Explicit non-goals

- no Cargo, Go, Maven, NuGet, Composer, Ruby, Swift, Dart, Elixir or Haskell parser;
- no package graph resolution;
- no direct/transitive inference beyond data present in the lockfile;
- no vulnerability matching;
- no SQLite database;
- no CLI integration.
