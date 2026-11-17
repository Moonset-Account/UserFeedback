#!/usr/bin/env python3
"""Local verification entry point for the memreader library.

Runs real assertions against the in-workspace implementation using
synthetic, deterministic page fragments (no cloud services, no network,
no real memory images).  This wrapper is a *run entry point*, not proof
of correctness by itself: auditors should still read the library code
and the real JSON logs it emits.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py            # runs all cases

Exit code is non-zero if any assertion fails, a requested case is
missing/unimplemented, a case makes zero assertions, or a whole case is
skipped.  Output is one JSON object per case (JSONL) with the case id,
seed, assertion counts and expected/observed summaries.  Byte samples
are truncated hex prefixes; all data is synthetic.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memreader import (  # noqa: E402
    ArrayField,
    Budgets,
    BytesField,
    FieldStatus,
    MapEntry,
    ObjectReader,
    PageFragment,
    PointerField,
    SnapshotStore,
    SourceInterval,
    StructDesc,
    UIntField,
)

SEED = 0x5EED


def norm(v, depth=0):
    """JSON-safe, size-bounded summary of an observed/expected value."""
    if isinstance(v, FieldStatus):
        return v.value
    if isinstance(v, SourceInterval):
        return {
            "file_offset": v.file_offset,
            "length": v.length,
            "origin": v.origin,
        }
    if isinstance(v, bytes):
        return "hex:" + v.hex()[:48] + ("..." if len(v) > 24 else "")
    if isinstance(v, (list, tuple)) and depth < 4:
        return [norm(x, depth + 1) for x in v]
    if isinstance(v, dict) and depth < 4:
        return {str(k): norm(x, depth + 1) for k, x in v.items()}
    return v


class CaseRunner:
    def __init__(self, case_id: str, seed: int):
        self.case_id = case_id
        self.seed = seed
        self.checks: list[dict] = []

    def check(self, name, expected, observed, note=""):
        self.checks.append(
            {
                "name": name,
                "passed": bool(expected == observed),
                "expected": norm(expected),
                "observed": norm(observed),
                "note": note,
            }
        )

    def report(self) -> dict:
        passed = sum(1 for c in self.checks if c["passed"])
        failed = len(self.checks) - passed
        return {
            "case": self.case_id,
            "seed": self.seed,
            "assertions": len(self.checks),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "status": "pass" if (failed == 0 and self.checks) else "fail",
            "checks": self.checks,
        }


def src_pairs(field_result):
    return [(s.file_offset, s.length) for s in field_result.sources]


# --------------------------------------------------------------------------
# C01: cross-page structure -- contiguous fragment reads, values, sources
# --------------------------------------------------------------------------

def case_c01(r: CaseRunner) -> None:
    """Cross-page struct: contiguous fragment reads, field values, source intervals."""
    gen, snap = 7, "s1"
    store = SnapshotStore([MapEntry(0x1000, 0x1000, 0x40000)])

    magic, count, tag, nxt = 0xDEADBEEF, 3, b"MEMSEG", 0x2000
    items = bytes([0x11, 0x22, 0x33])
    img = (
        magic.to_bytes(4, "little")
        + count.to_bytes(2, "little")
        + tag
        + nxt.to_bytes(8, "little")
        + items
    )
    assert len(img) == 0x17
    # Fragments split so that `tag` and `next` each span a fragment
    # boundary; f1b overlaps f1/f2 with byte-identical content (mergeable).
    store.add_fragment(PageFragment(gen, snap, 0x1000, img[0:8], "seg-1"))
    store.add_fragment(PageFragment(gen, snap, 0x1004, img[4:12], "seg-1b"))
    store.add_fragment(PageFragment(gen, snap, 0x1008, img[8:12], "seg-2"))
    store.add_fragment(PageFragment(gen, snap, 0x100C, img[12:20], "seg-3"))
    store.add_fragment(PageFragment(gen, snap, 0x1014, img[20:23], "seg-4"))

    hdr = StructDesc(
        "sample_hdr",
        (
            UIntField("magic", 4),
            UIntField("count", 2),
            BytesField("tag", 6),
            PointerField("next"),
            ArrayField("items", UIntField("item", 1), count_from="count"),
        ),
    )
    reader = ObjectReader(store, Budgets(max_field_length=64, max_objects=8))
    obj = reader.read_object(gen, snap, hdr, 0x1000)

    r.check("object fully ok across fragments", True, obj.ok)
    r.check("magic value", magic, obj.fields["magic"].value)
    r.check("count value", count, obj.fields["count"].value)
    r.check("tag spans fragment boundary", tag, obj.fields["tag"].value)
    r.check(
        "tag source intervals",
        [(0x40006, 2), (0x40008, 4)],
        src_pairs(obj.fields["tag"]),
        "two intervals from two fragments",
    )
    r.check("next pointer value", nxt, obj.fields["next"].value)
    r.check(
        "next source interval",
        [(0x4000C, 8)],
        src_pairs(obj.fields["next"]),
    )
    r.check("length-prefixed array value", [0x11, 0x22, 0x33], obj.fields["items"].value)
    r.check(
        "items source interval",
        [(0x40014, 3)],
        src_pairs(obj.fields["items"]),
    )
    raw = store.read_bytes(gen, snap, 0x1000, len(img))
    r.check("contiguous cross-fragment read ok", FieldStatus.OK, raw.status)
    r.check("contiguous read bytes", img, raw.data)
    r.check(
        "identical overlap merged without conflict",
        (FieldStatus.OK, img[4:12]),
        (store.read_bytes(gen, snap, 0x1004, 8).status,
         store.read_bytes(gen, snap, 0x1004, 8).data),
    )


# --------------------------------------------------------------------------
# C02: missing page vs true zero vs same-snapshot conflict vs other snapshot
# --------------------------------------------------------------------------

def case_c02(r: CaseRunner) -> None:
    """Missing page vs true zero vs same-snapshot conflict vs cross-snapshot isolation."""
    gen = 1
    store = SnapshotStore([MapEntry(0x2000, 0x1000, 0x50000)])
    rec = StructDesc(
        "rec",
        (UIntField("a", 4), UIntField("b", 4), UIntField("c", 4)),
    )
    reader = ObjectReader(store, Budgets(max_field_length=64, max_objects=8))

    store.add_fragment(
        PageFragment(gen, "s1", 0x2000, (0x11111111).to_bytes(4, "little") + b"\x00" * 4, "segA")
    )
    obj1 = reader.read_object(gen, "s1", rec, 0x2000)
    r.check("a ok", (FieldStatus.OK, 0x11111111),
            (obj1.fields["a"].status, obj1.fields["a"].value))
    r.check("b is a REAL zero, not a missing page", (FieldStatus.OK, 0),
            (obj1.fields["b"].status, obj1.fields["b"].value))
    r.check("c missing page", FieldStatus.MISSING_PAGE, obj1.fields["c"].status)
    r.check("c missing interval", [(0x2008, 0x200C)], list(obj1.fields["c"].missing))
    r.check("confirmed field a retained despite missing c",
            0x11111111, obj1.fields["a"].value)

    # Same snapshot, overlapping fragment whose `a` bytes disagree.
    store.add_fragment(
        PageFragment(gen, "s1", 0x2000, (0x22222222).to_bytes(4, "little") + b"\x00" * 4, "segB")
    )
    obj2 = reader.read_object(gen, "s1", rec, 0x2000)
    fa = obj2.fields["a"]
    r.check("conflicting a flagged", FieldStatus.CONFLICT, fa.status)
    r.check("conflict value withheld", None, fa.value)
    r.check("one conflict segment", 1, len(fa.conflicts))
    cand = fa.conflicts[0].candidates
    r.check("both candidate byte strings kept",
            sorted([b"\x11\x11\x11\x11", b"\x22\x22\x22\x22"]),
            sorted(c.data for c in cand))
    r.check("both candidate sources kept",
            sorted(["segA", "segB"]),
            sorted(c.sources[0].origin for c in cand))
    r.check("agreeing b still merges to real zero", (FieldStatus.OK, 0),
            (obj2.fields["b"].status, obj2.fields["b"].value))

    # A different snapshot holds c; it must NOT complete the s1 object.
    store.add_fragment(
        PageFragment(gen, "s2", 0x2008, (0x33333333).to_bytes(4, "little"), "segC")
    )
    obj3 = reader.read_object(gen, "s1", rec, 0x2000)
    r.check("no cross-snapshot assembly for c",
            FieldStatus.MISSING_PAGE, obj3.fields["c"].status)
    obj_s2 = reader.read_object(gen, "s2", rec, 0x2000)
    r.check("s2 sees its own c", (FieldStatus.OK, 0x33333333),
            (obj_s2.fields["c"].status, obj_s2.fields["c"].value))
    r.check("s2 does not see s1's a",
            FieldStatus.MISSING_PAGE, obj_s2.fields["a"].status)


# --------------------------------------------------------------------------
# C03: bounds and length budgets; confirmed fields survive
# --------------------------------------------------------------------------

def case_c03(r: CaseRunner) -> None:
    """Int/bytes/pointer/array bounds and length budgets; confirmed fields survive."""
    rng = random.Random(r.seed)
    gen, snap = 3, "s1"
    store = SnapshotStore([MapEntry(0x3000, 0x1000, 0x60000)])
    blob = StructDesc(
        "blob",
        (
            UIntField("len", 4),
            ArrayField("payload", UIntField("byte", 1), count_from="len"),
            PointerField("next"),
            BytesField("sig", 16),
        ),
    )
    reader = ObjectReader(store, Budgets(max_field_length=32, max_objects=4))

    # (a) array length 100 exceeds the 32-byte field budget; later fields
    #     still parse because their offsets remain computable.
    sig_a = rng.randbytes(16)
    store.add_fragment(PageFragment(gen, snap, 0x3000, (100).to_bytes(4, "little"), "c03a-len"))
    store.add_fragment(PageFragment(gen, snap, 0x3068, (0x4000).to_bytes(8, "little"), "c03a-next"))
    store.add_fragment(PageFragment(gen, snap, 0x3070, sig_a, "c03a-sig"))
    obj_a = reader.read_object(gen, snap, blob, 0x3000)
    r.check("len confirmed", (FieldStatus.OK, 100),
            (obj_a.fields["len"].status, obj_a.fields["len"].value))
    r.check("payload out of bounds", FieldStatus.OUT_OF_BOUNDS,
            obj_a.fields["payload"].status)
    r.check("oob payload has no value/sources",
            (None, []), (obj_a.fields["payload"].value, src_pairs(obj_a.fields["payload"])))
    r.check("next retained after oob array", (FieldStatus.OK, 0x4000),
            (obj_a.fields["next"].status, obj_a.fields["next"].value))
    r.check("sig retained after oob array", (FieldStatus.OK, sig_a),
            (obj_a.fields["sig"].status, obj_a.fields["sig"].value))

    # (b) in-budget array parses; zero-length array is a real empty value.
    store.add_fragment(PageFragment(gen, snap, 0x3100, (5).to_bytes(4, "little"), "c03b-len"))
    store.add_fragment(PageFragment(gen, snap, 0x3104, b"PAYLD", "c03b-pay"))
    store.add_fragment(PageFragment(gen, snap, 0x3109, (0).to_bytes(8, "little"), "c03b-next"))
    store.add_fragment(PageFragment(gen, snap, 0x3111, rng.randbytes(16), "c03b-sig"))
    obj_b = reader.read_object(gen, snap, blob, 0x3100)
    r.check("in-budget array ok", (FieldStatus.OK, list(b"PAYLD")),
            (obj_b.fields["payload"].status, obj_b.fields["payload"].value))

    store.add_fragment(PageFragment(gen, snap, 0x3180, (0).to_bytes(4, "little"), "c03b2-len"))
    store.add_fragment(PageFragment(gen, snap, 0x3184, (0).to_bytes(8, "little"), "c03b2-next"))
    store.add_fragment(PageFragment(gen, snap, 0x318C, rng.randbytes(16), "c03b2-sig"))
    obj_b2 = reader.read_object(gen, snap, blob, 0x3180)
    r.check("zero-length array is ok and empty", (FieldStatus.OK, []),
            (obj_b2.fields["payload"].status, obj_b2.fields["payload"].value))

    # (c) fixed-width int / pointer / bytes fields each exceed a tiny
    #     budget; a small int and byte still confirm.
    small = StructDesc(
        "small",
        (
            UIntField("big", 8),
            UIntField("small32", 4),
            PointerField("ptr"),
            BytesField("blob", 16),
            UIntField("tail", 1),
        ),
    )
    body = (
        (0x0102030405060708).to_bytes(8, "little")
        + (0xA5A5A5A5).to_bytes(4, "little")
        + (0x9000).to_bytes(8, "little")
        + rng.randbytes(16)
        + b"\x7f"
    )
    store.add_fragment(PageFragment(gen, snap, 0x3200, body, "c03c"))
    tiny = ObjectReader(store, Budgets(max_field_length=4, max_objects=4))
    obj_c = tiny.read_object(gen, snap, small, 0x3200)
    r.check("8-byte int over budget", FieldStatus.OUT_OF_BOUNDS,
            obj_c.fields["big"].status)
    r.check("4-byte int confirmed", (FieldStatus.OK, 0xA5A5A5A5),
            (obj_c.fields["small32"].status, obj_c.fields["small32"].value))
    r.check("8-byte pointer over budget", FieldStatus.OUT_OF_BOUNDS,
            obj_c.fields["ptr"].status)
    r.check("16-byte bytes field over budget", FieldStatus.OUT_OF_BOUNDS,
            obj_c.fields["blob"].status)
    r.check("1-byte tail confirmed", (FieldStatus.OK, 0x7F),
            (obj_c.fields["tail"].status, obj_c.fields["tail"].value))

    # (d) count field missing -> array and everything after it cannot be
    #     located; object is marked truncated, earlier fields unaffected.
    obj_d = reader.read_object(gen, snap, blob, 0x3300)
    r.check("missing count field", FieldStatus.MISSING_PAGE, obj_d.fields["len"].status)
    r.check("array dependency failed", FieldStatus.DEPENDENCY_FAILED,
            obj_d.fields["payload"].status)
    r.check("later fields unlocatable", FieldStatus.DEPENDENCY_FAILED,
            obj_d.fields["next"].status)
    r.check("object flagged truncated", True, obj_d.truncated)


# --------------------------------------------------------------------------
# C04: self-loop list -- identity, object budget, cycle termination
# --------------------------------------------------------------------------

def case_c04(r: CaseRunner) -> None:
    """Self-loop list: (generation, snapshot, va) identity, object budget, cycle stop."""
    store = SnapshotStore([MapEntry(0x8000, 0x2000, 0x70000)])
    node = StructDesc(
        "node",
        (UIntField("id", 4), BytesField("pad", 4), PointerField("next")),
    )

    def node_bytes(nid, nxt):
        return nid.to_bytes(4, "little") + b"\x00" * 4 + nxt.to_bytes(8, "little")

    # Same VA 0x8000 in three different (generation, snapshot) contexts.
    store.add_fragment(PageFragment(1, "s1", 0x8000, node_bytes(1, 0x8000), "n-g1s1"))  # self-loop
    store.add_fragment(PageFragment(1, "s2", 0x8000, node_bytes(2, 0x0), "n-g1s2"))      # terminates
    store.add_fragment(PageFragment(2, "s1", 0x8000, node_bytes(3, 0x8000), "n-g2s1"))  # self-loop
    # A 5-node chain in snapshot s3.
    for i, nid in enumerate([10, 11, 12, 13, 14]):
        va = 0x8000 + i * 0x10
        nxt = 0x8000 + (i + 1) * 0x10 if nid != 14 else 0
        store.add_fragment(PageFragment(1, "s3", va, node_bytes(nid, nxt), f"n-chain-{nid}"))

    wide = ObjectReader(store, Budgets(max_field_length=64, max_objects=16))

    t1 = wide.traverse(1, "s1", node, 0x8000, "next")
    r.check("self-loop yields one object", 1, len(t1.objects))
    r.check("self-loop detected as cycle", True, t1.cycle_detected)
    r.check("cycle identity is (gen, snap, va)", (1, "s1", 0x8000), t1.cycle_identity)
    r.check("self-loop node id", 1, t1.objects[0].fields["id"].value)
    r.check("cycle stop is not budget stop", "cycle", t1.stop_reason)

    t2 = wide.traverse(1, "s2", node, 0x8000, "next")
    r.check("same VA in s2 terminates on null", True, t2.terminated_normally)
    r.check("s2 node id independent", 2, t2.objects[0].fields["id"].value)
    r.check("same VA different snapshot => different identity",
            False, t1.objects[0].identity == t2.objects[0].identity)

    t3 = wide.traverse(2, "s1", node, 0x8000, "next")
    r.check("same VA different generation cycles separately",
            (True, (2, "s1", 0x8000)), (t3.cycle_detected, t3.cycle_identity))
    r.check("generation differs in identity",
            False, t3.cycle_identity == t1.cycle_identity)

    t1_again = wide.traverse(1, "s1", node, 0x8000, "next")
    r.check("s1 self-loop unaffected by other snapshots",
            (True, 1), (t1_again.cycle_detected, len(t1_again.objects)))

    tight = ObjectReader(store, Budgets(max_field_length=64, max_objects=3))
    t4 = tight.traverse(1, "s3", node, 0x8000, "next")
    r.check("object budget stops chain at 3", 3, len(t4.objects))
    r.check("budget stop flagged", True, t4.budget_exceeded)
    r.check("budget stop keeps confirmed ids",
            [10, 11, 12], [o.fields["id"].value for o in t4.objects])

    t5 = wide.traverse(1, "s3", node, 0x8000, "next")
    r.check("wide budget walks whole chain",
            [10, 11, 12, 13, 14], [o.fields["id"].value for o in t5.objects])
    r.check("chain terminates on null pointer", True, t5.terminated_normally)


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="list cases and exit")
    ap.add_argument("--case", help="run a single case (default: run all)")
    args = ap.parse_args(argv)

    if args.list:
        for cid, fn in CASES.items():
            print(json.dumps({"case": cid, "description": (fn.__doc__ or "").strip()}))
        return 0

    if args.case:
        if args.case not in CASES:
            print(json.dumps({"error": f"unknown case {args.case!r}",
                              "known": sorted(CASES)}))
            return 2
        selected = [args.case]
    else:
        selected = list(CASES)

    reports = []
    for cid in selected:
        fn = CASES.get(cid)
        if fn is None:
            reports.append({"case": cid, "status": "fail",
                            "error": "not implemented", "assertions": 0})
            continue
        runner = CaseRunner(cid, SEED)
        fn(runner)
        reports.append(runner.report())

    for rep in reports:
        print(json.dumps(rep, ensure_ascii=False))

    ok = all(
        rep.get("status") == "pass"
        and rep.get("assertions", 0) > 0
        and rep.get("skipped", 1) == 0
        for rep in reports
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
