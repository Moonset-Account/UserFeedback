#!/usr/bin/env python3
"""Local verification entry point for the shared raw cell cache.

Every case below builds a synthetic registry hive in memory, drives the real
``dissect.regf`` implementation and asserts on real observations (read counts, cached byte
counts, returned payloads). It is a run entry point, not an independent certificate of
correctness: the audit still has to read the code and the logs.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01 --case C02
    python tools/local_verify.py --case all

Exit status is non-zero when a case is unknown, a case reports zero assertions, or any
assertion fails. Output is one JSON object per case plus a final summary object. Only
synthetic fixture bytes and aggregate counters are included; no credentials, accounts or
real acquisition samples are read or printed.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dissect.regf import CellCache, RegistryHive, regf  # noqa: E402

from tests.synth_hive import build_working_hive  # noqa: E402

HBIN_OFFSET = len(regf.c_regf._HBASE_BLOCK)
CHILD_NAMES = ("Alpha", "Bravo", "Charlie", "Delta")


class CountingReader(io.BytesIO):
    """A read adapter that records every read so payload reuse can be counted."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.reads: list[tuple[int, int]] = []

    def read(self, size: int = -1) -> bytes:  # noqa: D102
        offset = self.tell()
        data = super().read(size)
        self.reads.append((offset, len(data)))
        return data

    def payload_reads(self, offset: int, length: int) -> int:
        return sum(1 for start, size in self.reads if start == offset and size == length)


class FailingReader(CountingReader):
    """Fails reads that start at ``fail_at``; used to prove failures are not cached."""

    def __init__(self, data: bytes, fail_at: int):
        super().__init__(data)
        self.fail_at = fail_at

    def read(self, size: int = -1) -> bytes:  # noqa: D102
        if self.tell() == self.fail_at:
            self.reads.append((self.tell(), 0))
            raise OSError("synthetic read failure")
        return super().read(size)


@dataclass
class CaseResult:
    case: str
    title: str
    checks: list[dict] = field(default_factory=list)
    sample: dict = field(default_factory=dict)
    error: str | None = None

    def check(self, name: str, condition: bool, expected: object, observed: object) -> None:
        self.checks.append(
            {
                "name": name,
                "ok": bool(condition),
                "expected": expected,
                "observed": observed,
            }
        )

    def to_json(self) -> dict:
        passed = sum(1 for c in self.checks if c["ok"])
        failed = len(self.checks) - passed
        payload = {
            "case": self.case,
            "title": self.title,
            "assertions": len(self.checks),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "sample": self.sample,
            "checks": self.checks,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def _working(tag: bytes, filename: str = "SYNTH", extra: tuple[bytes, ...] = ()) -> dict:
    return build_working_hive(filename, tag=tag, extra_payloads=extra)


def _payload(spec: dict, child: str, field_name: str = "blob_data_offset") -> tuple[int, int]:
    offset = spec["children"][child][field_name]
    return offset, spec["builder"].payload_length(offset)


def case_c01() -> CaseResult:
    """Hot shared raw cell payload is read once and only payload bytes count."""
    result = CaseResult("C01", "shared raw cell hot reuse; cache holds payload bytes only")
    spec = _working(b"\x41")
    offset, length = _payload(spec, "Bravo")
    payload_start = HBIN_OFFSET + offset + 4

    fh = CountingReader(spec["blob"])
    cache = CellCache(64 * 1024)
    hive = RegistryHive(fh, cache=cache)
    baseline_reads = fh.payload_reads(payload_start, length)
    baseline_bytes = cache.stats()["resident_bytes"]

    first = hive.cell_data(offset)
    after_first = fh.payload_reads(payload_start, length)
    for _ in range(6):
        assert hive.cell_data(offset) == first

    second_hive = RegistryHive(io.BytesIO(spec["blob"]), cache=CellCache(4096))
    second_hive.cell_data(offset)

    stats = cache.stats()
    entry = cache._entries[hive._cache_key(offset, length)]

    result.sample = {"fixture": "synthetic", "tag": "0x41", "cell_offset": offset, "cell_payload_bytes": length}
    result.check(
        "first access performs exactly one payload read",
        after_first - baseline_reads == 1,
        {"payload_reads": baseline_reads + 1},
        {"payload_reads": after_first},
    )
    result.check(
        "repeated accesses do not re-read the payload from the file",
        fh.payload_reads(payload_start, length) == after_first,
        {"payload_reads": after_first},
        {"payload_reads": fh.payload_reads(payload_start, length)},
    )
    result.check("cache reports hits for the repeated accesses", stats["hits"] >= 6, {"hits": ">=6"}, {"hits": stats["hits"]})
    result.check(
        "cached value is exactly the raw payload bytes",
        len(entry) == length and entry == first,
        {"cached_bytes": length},
        {"cached_bytes": len(entry)},
    )
    result.check(
        "resident bytes grew by exactly one payload (size header not cached)",
        stats["resident_bytes"] - baseline_bytes == length,
        {"delta": length},
        {"delta": stats["resident_bytes"] - baseline_bytes},
    )
    result.check(
        "parsed cell objects are not part of the payload budget",
        stats["resident_bytes"] == sum(len(v) for v in cache._entries.values()),
        {"resident_bytes": "sum of payload lengths"},
        {"resident_bytes": stats["resident_bytes"]},
    )
    return result


def case_c02() -> CaseResult:
    """Equal offsets in different hive instances or acquisition versions stay isolated."""
    result = CaseResult("C02", "same offset across hive identity and collection version is isolated")
    one = _working(b"\x41", filename="SAME.NAME")
    two = _working(b"\x42", filename="SAME.NAME")
    offset, length = _payload(one, "Alpha")
    payload_len = len(one["children"]["Alpha"]["blob_payload"])
    assert offset == _payload(two, "Alpha")[0], "fixture layout mismatch"

    cache = CellCache(16 * 1024)
    hive_one = RegistryHive(io.BytesIO(one["blob"]), cache=cache)
    hive_two = RegistryHive(io.BytesIO(two["blob"]), cache=cache)

    data_one = hive_one.cell_data(offset)[:payload_len]
    data_two = hive_two.cell_data(offset)[:payload_len]

    # Same file name and offset, then a re-collection of the same hive instance.
    fh = io.BytesIO(one["blob"])
    versioned = RegistryHive(fh, cache=cache)
    old = versioned.cell_data(offset)[:payload_len]
    fh.seek(0)
    fh.write(two["blob"])
    versioned.set_collection_version("acquisition-2")
    new = versioned.cell_data(offset)[:payload_len]

    stats = cache.stats()
    result.sample = {
        "fixture": "synthetic",
        "tags": ["0x41", "0x42"],
        "cell_offset": offset,
        "cell_payload_bytes": length,
        "same_filename": True,
    }
    result.check(
        "same offset is genuinely shared by both fixtures",
        _payload(two, "Alpha")[0] == offset,
        {"offset": offset},
        {"offset": _payload(two, "Alpha")[0]},
    )
    result.check(
        "hive instances with equal offsets receive their own bytes",
        data_one == b"\x41" * payload_len and data_two == b"\x42" * payload_len,
        {"hive_one_prefix": "0x41", "hive_two_prefix": "0x42"},
        {"hive_one_prefix": data_one[:1].hex(), "hive_two_prefix": data_two[:1].hex()},
    )
    result.check(
        "two hives occupy separate cache entries for the same offset",
        hive_one._cache_key(offset, length) != hive_two._cache_key(offset, length),
        {"distinct_keys": True},
        {"distinct_keys": hive_one._cache_key(offset, length) != hive_two._cache_key(offset, length)},
    )
    result.check(
        "changing the collection version drops the stale entries",
        old == b"\x41" * payload_len and new == b"\x42" * payload_len,
        {"before": "0x41", "after": "0x42"},
        {"before": old[:1].hex(), "after": new[:1].hex()},
    )
    result.check(
        "cache stats show entries from more than one hive",
        stats["entries"] >= 2,
        {"entries": ">=2"},
        {"entries": stats["entries"]},
    )
    return result


def case_c03() -> CaseResult:
    """Mixed cell sizes, LRU eviction, oversized bypass, failed reads, budget invariant."""
    result = CaseResult("C03", "size disparity, eviction, over-budget direct read, failure not cached")
    extra = (b"\x01" * 200, b"\x02" * 1536, b"\x03" * 240)
    spec = _working(b"\x55", filename="MIXED", extra=extra)
    budget = 512
    cache = CellCache(budget)
    hive = RegistryHive(io.BytesIO(spec["blob"]), cache=cache)

    operations = [child["blob_data_offset"] for child in spec["children"].values()]
    operations.extend(spec["extra_offsets"])

    over_limit = []
    sizes = []
    for _ in range(3):
        for offset in operations:
            length = spec["builder"].payload_length(offset)
            sizes.append(length)
            hive.cell_data(offset)
            if cache.resident_bytes > budget:
                over_limit.append((offset, cache.resident_bytes))

    evicted_stats = cache.stats()

    # A cell larger than the whole budget must be served from the file, not retained.
    big_payload = b"\x77" * (budget + 256)
    big_spec = _working(b"\x55", filename="BIG", extra=(big_payload,))
    big_offset = big_spec["extra_offsets"][0]
    big_cache = CellCache(budget)
    big_hive = RegistryHive(io.BytesIO(big_spec["blob"]), cache=big_cache)
    before_big = big_cache.stats()["resident_bytes"]
    big_data = big_hive.cell_data(big_offset)
    after_big = big_cache.stats()

    # A failing read must surface and must not be cached as empty data.
    fail_spec = _working(b"\x55", filename="FAIL")
    fail_offset = fail_spec["children"]["Charlie"]["node_offset"]
    fail_len = fail_spec["builder"].payload_length(fail_offset)
    fail_cache = CellCache(4096)
    fail_hive = RegistryHive(io.BytesIO(fail_spec["blob"]), cache=fail_cache)
    fail_baseline = fail_cache.stats()
    fail_hive.fh = FailingReader(fail_spec["blob"], HBIN_OFFSET + fail_offset + 4)
    raised = None
    try:
        fail_hive.cell_data(fail_offset)
    except OSError as exc:
        raised = type(exc).__name__
    fail_stats = fail_cache.stats()

    # A truncated read returns what the handle gives, but is not cached as if complete.
    short_spec = _working(b"\x55", filename="SHORT")
    short_offset = short_spec["children"]["Delta"]["node_offset"]
    short_cache = CellCache(4096)
    short_hive = RegistryHive(io.BytesIO(short_spec["blob"]), cache=short_cache)
    short_baseline = short_cache.stats()
    short_hive.fh = io.BytesIO(short_spec["blob"][: HBIN_OFFSET + short_offset + 4])
    short_data = short_hive.cell_data(short_offset)
    short_stats = short_cache.stats()

    result.sample = {
        "fixture": "synthetic",
        "budget_bytes": budget,
        "cell_payload_sizes": sorted(set(sizes))[:8],
        "oversized_payload_bytes": len(big_payload),
    }
    result.check(
        "resident bytes never exceed the budget after any operation",
        not over_limit,
        {"max_resident_bytes": budget},
        {"violations": len(over_limit)},
    )
    result.check(
        "heterogeneous sizes force evictions while staying within budget",
        evicted_stats["evictions"] > 0 and evicted_stats["resident_bytes"] <= budget,
        {"evictions": ">0", "resident_bytes": f"<={budget}"},
        {"evictions": evicted_stats["evictions"], "resident_bytes": evicted_stats["resident_bytes"]},
    )
    result.check(
        "a cell larger than the budget is still read correctly",
        big_data[: len(big_payload)] == big_payload,
        {"prefix": "0x77", "payload_bytes": len(big_payload)},
        {"prefix": big_data[:1].hex(), "payload_bytes": len(big_data)},
    )
    result.check(
        "an oversized cell is not retained",
        after_big["resident_bytes"] == before_big and after_big["oversized"] >= 1,
        {"resident_delta": 0, "oversized": ">=1"},
        {"resident_delta": after_big["resident_bytes"] - before_big, "oversized": after_big["oversized"]},
    )
    result.check(
        "a failing read raises instead of returning cached empty data",
        raised == "OSError",
        {"exception": "OSError"},
        {"exception": raised},
    )
    result.check(
        "a failed read adds no cache entry",
        fail_stats["entries"] == fail_baseline["entries"]
        and fail_stats["resident_bytes"] == fail_baseline["resident_bytes"]
        and fail_stats["failed_reads"] == 1,
        {"entries": fail_baseline["entries"], "resident_bytes": fail_baseline["resident_bytes"]},
        {"entries": fail_stats["entries"], "resident_bytes": fail_stats["resident_bytes"]},
    )
    result.check(
        "a short/truncated read is served but not cached",
        short_data == b""
        and short_stats["entries"] == short_baseline["entries"]
        and short_stats["short_reads"] == 1
        and fail_len > 0,
        {"data": "b''", "entries": short_baseline["entries"], "short_reads": 1},
        {"data_len": len(short_data), "entries": short_stats["entries"], "short_reads": short_stats["short_reads"]},
    )
    return result


def case_c04() -> CaseResult:
    """Release is per-hive, caller handles stay open, default path unchanged."""
    result = CaseResult("C04", "per-hive release, caller handle ownership, compatible default")
    one = _working(b"\x41", filename="ONE")
    two = _working(b"\x42", filename="TWO")
    offset, length = _payload(one, "Charlie")

    cache = CellCache(8192)
    fh_one = CountingReader(one["blob"])
    fh_two = CountingReader(two["blob"])
    hive_one = RegistryHive(fh_one, cache=cache)
    hive_two = RegistryHive(fh_two, cache=cache)

    hive_one.cell_data(offset)
    hive_two.cell_data(offset)
    entries_before = cache.stats()["entries"]
    hive_one_keys = {key for key in cache._entries if key[0] == hive_one._cache_id}
    hive_two_keys = {key for key in cache._entries if key[0] == hive_two._cache_id}

    released = hive_one.release_cache()
    remaining = {key for key in cache._entries}
    payload_start = HBIN_OFFSET + offset + 4
    reads_before = fh_two.payload_reads(payload_start, length)
    still_correct = hive_two.cell_data(offset)
    reads_after = fh_two.payload_reads(payload_start, length)

    # Default construction must behave exactly as before and not adopt a shared cache.
    plain_fh = io.BytesIO(one["blob"])
    plain = RegistryHive(plain_fh)
    plain_root = plain.root()
    plain_children = [sk.name for sk in plain_root.subkeys()]
    plain_cell_repeat = plain.cell(plain_root.cell.SubKeyLists[0]) is plain.cell(plain_root.cell.SubKeyLists[0])
    plain_release = plain.release_cache()

    doc = CellCache.__doc__ or ""
    lowered = doc.lower()
    result.sample = {
        "fixture": "synthetic",
        "tags": ["0x41", "0x42"],
        "cell_offset": offset,
        "shared_cache_budget_bytes": cache.budget,
    }
    result.check(
        "releasing one hive drops only its own entries",
        released == len(hive_one_keys)
        and remaining == hive_two_keys
        and entries_before == len(hive_one_keys) + len(hive_two_keys),
        {"released": len(hive_one_keys), "remaining": len(hive_two_keys)},
        {"released": released, "remaining": len(remaining)},
    )
    result.check(
        "the other hive keeps serving cached bytes without a new payload read",
        still_correct[: len(two["children"]["Charlie"]["blob_payload"])] == b"\x42" * len(two["children"]["Charlie"]["blob_payload"])
        and reads_after == reads_before,
        {"payload_reads": reads_before, "prefix": "0x42"},
        {"payload_reads": reads_after, "prefix": still_correct[:1].hex()},
    )
    result.check(
        "neither caller handle is closed",
        not fh_one.closed and not fh_two.closed,
        {"closed": False},
        {"fh_one_closed": fh_one.closed, "fh_two_closed": fh_two.closed},
    )
    result.check(
        "default construction keeps working without a shared cache",
        plain.cache is None and plain_root.name == "ROOT" and plain_children == list(CHILD_NAMES),
        {"cache": None, "subkeys": list(CHILD_NAMES)},
        {"cache": plain.cache, "subkeys": plain_children},
    )
    result.check(
        "default parsed-cell caching and release semantics are unchanged",
        plain_cell_repeat is True and plain_release == 0 and not plain_fh.closed,
        {"parsed_cell_identity": True, "released": 0, "closed": False},
        {"parsed_cell_identity": plain_cell_repeat, "released": plain_release, "closed": plain_fh.closed},
    )
    result.check(
        "documentation does not claim a whole-process memory limit",
        "process" in lowered and "not" in lowered and "budget is therefore *not*" in lowered,
        {"claim": "raw payload budget only, explicitly not process memory"},
        {"mentions_process": "process" in lowered},
    )
    result.check(
        "no process-wide memory accounting is exposed",
        not hasattr(cache, "process_memory_limit")
        and "process_memory_limit" not in cache.stats()
        and set(cache.stats()) >= {"budget", "resident_bytes", "entries"},
        {"process_memory_limit": "absent"},
        {"stats_keys": sorted(cache.stats())},
    )
    return result


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


DEBUG = False


def run_case(name: str) -> dict:
    try:
        result = CASES[name]()
    except Exception as exc:  # a raised fixture/implementation error is a failed case, not a pass
        result = CaseResult(name, CASES[name].__doc__ or name)
        result.error = f"{type(exc).__name__}: {exc}"
        result.checks.append(
            {
                "name": "case executed",
                "ok": False,
                "expected": "case runs to completion",
                "observed": result.error,
            }
        )
        if DEBUG:
            traceback.print_exc()

    payload = result.to_json()
    if payload["assertions"] == 0:
        payload["failed"] = payload["failed"] + 1
        payload["error"] = payload.get("error") or "case produced zero assertions"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list available cases and exit")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="C01|C02|C03|C04|all",
        help="case to run; repeatable, comma separated or 'all'",
    )
    parser.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    parser.add_argument("--debug", action="store_true", help="print tracebacks for case execution errors")
    args = parser.parse_args(argv)

    global DEBUG
    DEBUG = args.debug

    if args.list:
        print(json.dumps({"cases": list(CASES), "count": len(CASES)}))
        return 0

    requested: list[str] = []
    for value in args.case:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if part.lower() == "all":
                requested.extend(CASES)
            else:
                requested.append(part.upper())

    if not requested:
        print(json.dumps({"error": "no case requested; use --list or --case", "cases": list(CASES)}))
        return 2

    unknown = [name for name in requested if name not in CASES]
    if unknown:
        print(json.dumps({"error": "unknown case", "unknown": unknown, "cases": list(CASES)}))
        return 2

    # De-duplicate but keep the requested order.
    ordered = list(dict.fromkeys(requested))

    total = {"assertions": 0, "passed": 0, "failed": 0, "skipped": 0}
    nonzero = False
    for name in ordered:
        payload = run_case(name)
        print(json.dumps(payload, sort_keys=True))
        total["assertions"] += payload["assertions"]
        total["passed"] += payload["passed"]
        total["failed"] += payload["failed"]
        total["skipped"] += payload["skipped"]
        if payload["failed"] or payload["assertions"] == 0 or payload.get("error"):
            nonzero = True

    print(json.dumps({"summary": total, "cases": ordered, "ok": not nonzero}, sort_keys=True))
    return 1 if nonzero else 0


if __name__ == "__main__":
    sys.exit(main())
