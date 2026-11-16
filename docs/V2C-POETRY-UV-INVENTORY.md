# Stage V2c — Poetry/uv Inventory

Status: bounded batch

## Scope

This batch adds offline parsing for:

- `poetry.lock` package stanzas from Poetry lockfile generations 1/2;
- `uv.lock` package stanzas;
- root-level integration into `build_inventory`.

## Exactness rules

- A package with both name and version becomes an exact PyPI `Component`.
- PyPI names use conservative PEP 503-style normalization for `-`, `_`, and `.`.
- A package stanza without a version becomes a warning, not a confirmed component.
- Workspace/editable metadata is not treated as a registry release unless an exact version is present.
- Duplicate observations continue to be deduplicated by canonical PURL.

## Parser boundary

The parsers intentionally read only the bounded `[[package]]` stanzas and the scalar `name`/`version` fields needed for inventory. They do not execute Poetry or uv, resolve dependencies, evaluate Python markers, or implement a complete TOML parser.

## Safety

- standard library only;
- no network;
- no subprocesses;
- no package manager invocation;
- regular-file and manifest-size limits inherited from V2a;
- malformed/unsupported data remains a warning or parser error handled without discarding other manifests.

## Non-goals

- no `pyproject.toml` dependency resolution;
- no `Pipfile.lock`, `pdm.lock`, or `pylock.toml` parser in this batch;
- no vulnerability matching;
- no SQLite;
- no updater;
- no CLI integration.
