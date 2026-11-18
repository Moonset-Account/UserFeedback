# dissect.regf

A Dissect module implementing a parser for Windows registry file format, used to store application and OS configuration
on Windows operating systems. For more information, please see [the
documentation](https://docs.dissect.tools/en/latest/projects/dissect.regf/index.html).

## Requirements

This project is part of the Dissect framework and requires Python.

Information on the supported Python versions can be found in the Getting Started section of [the documentation](https://docs.dissect.tools/en/latest/index.html#getting-started).

## Installation

`dissect.regf` is available on [PyPI](https://pypi.org/project/dissect.regf/).

```bash
pip install dissect.regf
```

This module is also automatically installed if you install the `dissect` package.

## Build and test instructions

This project uses `tox` to build source and wheel distributions. Run the following command from the root folder to build
these:

```bash
tox -e build
```

The build artifacts can be found in the `dist/` directory.

`tox` is also used to run linting and unit tests in a self-contained environment. To run both linting and unit tests
using the default installed Python version, run:

```bash
tox
```

For a more elaborate explanation on how to build and test the project, please see [the
documentation](https://docs.dissect.tools/en/latest/contributing/tooling.html).

## Contributing

The Dissect project encourages any contribution to the codebase. To make your contribution fit into the project, please
refer to [the development guide](https://docs.dissect.tools/en/latest/contributing/developing.html).

## Shared raw cell cache

When several registry hives are read interleaved (e.g. SYSTEM, SOFTWARE and NTUSER from one acquisition), the same
raw cell payloads are often requested more than once. An optional `CellCache` can be shared between `RegistryHive`
instances to reuse those raw payload reads under a total byte budget:

```python
from dissect.regf import CellCache, RegistryHive

cache = CellCache(8 * 1024 * 1024)  # total budget for retained raw cell payload bytes
system = RegistryHive(system_fh, cache=cache)
software = RegistryHive(software_fh, cache=cache)

assert cache.resident_bytes <= cache.budget

software.release_cache()  # drops only software's entries; system stays cached
print(cache.stats())
```

Actual API:

| Object | Signature | Behaviour |
| --- | --- | --- |
| `CellCache(budget)` | positional `budget: int` | Total budget in bytes for retained raw cell payloads. `TypeError` for non-int, `ValueError` for negative. |
| `CellCache.fetch(key, reader, length)` | `-> bytes` | Returns cached bytes or calls `reader()` and caches a complete payload. |
| `CellCache.put(key, data)` | `-> bool` | Stores a payload, evicting LRU entries. Oversized or empty payloads are not kept. |
| `CellCache.release(hive)` | `-> int` | Drops entries belonging to one hive; other hives are untouched. |
| `CellCache.clear()` | `-> None` | Drops all entries. |
| `CellCache.resident_bytes` | property | Sum of retained raw payload bytes. |
| `CellCache.stats()` | `-> dict[str, int]` | `budget`, `entries`, `resident_bytes`, `hits`, `misses`, `evictions`, `oversized`, `failed_reads`, `short_reads`, `payload_bytes_fetched`. |
| `RegistryHive(fh, *, cache=None, collection_version=None)` | constructor | `cache` is opt-in; without it the default behaviour (per-hive `lru_cache` of parsed cells only) is unchanged. |
| `RegistryHive.collection_version` | property | The version token currently used in cache keys. |
| `RegistryHive.set_collection_version(version)` | `-> None` | Marks a new acquisition version and drops this hive's stale raw-cache entries. Parsed cells already memoised on the instance are not rebuilt; construct a new hive if bytes changed. |
| `RegistryHive.release_cache()` | `-> int` | Convenience wrapper over `CellCache.release`. Returns `0` without a cache. |

Semantics worth knowing:

- Only the raw cell payload (the bytes after the 4-byte cell size header) is cached. Parsed `Cell` objects, the
  per-hive `lru_cache` of parsed cells, and data the caller already holds are **outside** the budget. The budget only
  bounds the sum of retained payload bytes; it is explicitly **not** a limit on the memory usage of the whole process.
- Cache keys include a per-instance hive id, the current acquisition version, a generation counter and the
  offset/length. Two hives, or two acquisitions of the same hive, never share bytes for the same offset. File names are
  never used as keys.
- A cell whose payload is larger than the whole budget is read directly and not retained.
- A read that raises propagates and is not cached. A short read is returned as-is (same as a plain file read, usually
  `b""`) but is not stored, so a failure never becomes cached empty data.
- After every operation the sum of retained payload bytes is `<= budget`.
- Releasing a hive does not affect other hives' entries, and no method opens or closes file handles; the caller keeps
  ownership of every `fh` passed in.
- The 4-byte cell size header is re-read on each access, so allocation state and truncation are observed live; the
  handle is left at the same position a plain read would leave it.

### Reproducible local verification

`tools/local_verify.py` is the local run entry point. It builds only synthetic hives in memory, drives the real
implementation and asserts on real observations (payload read counts, cached byte counts, returned payloads):

```bash
python tools/local_verify.py --list
python tools/local_verify.py --case C01
python tools/local_verify.py --case C01,C02,C03,C04
```

| Case | Covers |
| --- | --- |
| `C01` | Shared raw cell hot reuse via a counting read adapter; only payload bytes are cached. |
| `C02` | Equal offsets in different hive instances or acquisition versions are isolated (not keyed by name/offset only). |
| `C03` | Heterogeneous cell sizes, LRU eviction, oversized cells read directly, failed/short reads not cached; resident bytes within budget after every operation. |
| `C04` | Releasing one hive leaves other hives intact, caller handles stay open, default path unchanged, no whole-process memory claim. |

Each case prints one JSON object with `case`, `assertions`, `passed`, `failed`, `skipped`, the synthetic sample
description and per-check expected/observed summaries; a final summary object follows. Exit status is non-zero on any
failed assertion, unknown case, case execution error or zero assertions. The wrapper is a run entry point, not an
independent certificate: an audit should still read the implementation and logs.

Not covered by the synthetic fixtures: real acquisition/logging formats, cross-process concurrency of one `CellCache`
(only a lock around cache state is provided), big-data (`db`) values larger than one segment, and actual OS-level
memory accounting. The tests in `tests/` additionally replay the bundled `SYSTEM.gz` fixture through both the plain and
cached paths to confirm identical key walking and values.


## Copyright and license

Dissect is released as open source by Fox-IT (<https://www.fox-it.com>) part of NCC Group Plc
(<https://www.nccgroup.com>).

Developed by the Dissect Team (<dissect@fox-it.com>) and made available at <https://github.com/fox-it/dissect>.

License terms: AGPL3 (<https://www.gnu.org/licenses/agpl-3.0.html>). For more information, see the LICENSE file.
