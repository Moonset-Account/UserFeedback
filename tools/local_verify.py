#!/usr/bin/env python3
"""Local verification harness for the gitverify quarantine checker.

Usage:
    tools/local_verify.py --list
    tools/local_verify.py --case C01
    tools/local_verify.py            # run all cases

Every case builds synthetic loose objects locally (zlib + hashlib only),
runs the real gitverify implementation, and performs real assertions.
Output is one JSON record per case with the assertion tally and short
expect/observed summaries.  Exit code is non-zero if any assertion fails,
if a case has zero assertions, or if a requested case is unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gitverify  # noqa: E402

# ---------------------------------------------------------------- helpers


def loose_bytes(otype: str, payload: bytes) -> bytes:
    return zlib.compress(otype.encode() + b" " + str(len(payload)).encode()
                         + b"\0" + payload)


def object_id(otype: str, payload: bytes) -> str:
    return hashlib.sha1(otype.encode() + b" " + str(len(payload)).encode()
                        + b"\0" + payload).hexdigest()


def inflated_size(otype: str, payload: bytes) -> int:
    return len(otype) + 1 + len(str(len(payload))) + 1 + len(payload)


def store_object(store: dict, otype: str, payload: bytes) -> str:
    oid = object_id(otype, payload)
    store[oid] = loose_bytes(otype, payload)
    return oid


def tree_entry(mode: int, name: bytes, oid: str) -> bytes:
    return ("%o" % mode).encode() + b" " + name + b"\0" + bytes.fromhex(oid)


def commit_payload(tree_oid: str, parents=(), message: bytes = b"synthetic\n") -> bytes:
    lines = [b"tree " + tree_oid.encode()]
    lines += [b"parent " + p.encode() for p in parents]
    lines += [b"author T <t@example.invalid> 1 +0000",
              b"committer T <t@example.invalid> 1 +0000",
              b"", message]
    return b"\n".join(lines)


class Adapter:
    """Read adapter over an in-memory store; records every object id read."""

    def __init__(self, store: dict):
        self.store = store
        self.calls: list = []

    def __call__(self, oid: str):
        self.calls.append(oid)
        return self.store.get(oid)


class CaseRun:
    def __init__(self, cid: str, description: str, seed: str):
        self.cid = cid
        self.description = description
        self.seed = seed
        self.items: list = []

    def check(self, name: str, cond: bool, expect: str = "", observed: str = ""):
        self.items.append({
            "name": name,
            "ok": bool(cond),
            "expect": str(expect)[:140],
            "observed": str(observed)[:140],
        })

    def summary(self) -> dict:
        passed = sum(1 for i in self.items if i["ok"])
        failed = len(self.items) - passed
        return {
            "case": self.cid,
            "description": self.description,
            "seed": self.seed,
            "assertions": len(self.items),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "details": self.items,
        }


def last_error(verdict) -> str:
    return verdict.chain[-1] if verdict.chain else ""


# ------------------------------------------------------------------- C01


def case_c01() -> CaseRun:
    run = CaseRun("C01", "loose header type/length, SHA-1 digest, commit/tree "
                         "target types", "c01-fixed-1")
    store: dict = {}
    blob_oid = store_object(store, "blob", b"hello\n")
    tree_oid = store_object(store, "tree", tree_entry(0o100644, b"a.txt", blob_oid))
    head = store_object(store, "commit", commit_payload(tree_oid))

    rep = gitverify.check_quarantine({"refs/heads/ok": head}, set(), Adapter(store))
    v = rep.branches["refs/heads/ok"]
    run.check("well-formed commit/tree/blob chain accepted", rep.ok and v.ok,
              "ok", v.chain)

    tampered = dict(store)
    tampered[blob_oid] = zlib.compress(b"blob 6\0hellO\n")  # same header, new body
    rep = gitverify.check_quarantine({"refs/heads/x": head}, set(), Adapter(tampered))
    v = rep.branches["refs/heads/x"]
    run.check("tampered payload rejected via SHA-1 mismatch",
              not v.ok and "sha1 mismatch" in last_error(v),
              "sha1 mismatch", last_error(v))

    lied = dict(store)
    lied[blob_oid] = zlib.compress(b"blob 7\0hello\n")  # declared 7, actual 6
    rep = gitverify.check_quarantine({"refs/heads/x": head}, set(), Adapter(lied))
    v = rep.branches["refs/heads/x"]
    run.check("lying header length rejected",
              not v.ok and "header length 7 != payload length 6" in last_error(v),
              "header length mismatch", last_error(v))

    weird_payload = b"x"
    weird_oid = object_id("weird", weird_payload)
    weird_store = dict(store)
    weird_store[weird_oid] = loose_bytes("weird", weird_payload)
    weird_tree = store_object(weird_store, "tree",
                              tree_entry(0o100644, b"w", weird_oid))
    weird_head = store_object(weird_store, "commit", commit_payload(weird_tree))
    rep = gitverify.check_quarantine({"refs/heads/x": weird_head}, set(),
                                     Adapter(weird_store))
    v = rep.branches["refs/heads/x"]
    run.check("unknown object type rejected",
              not v.ok and "unknown object type" in last_error(v),
              "unknown object type", last_error(v))

    bad_tree_head = store_object(store, "commit", commit_payload(blob_oid))
    rep = gitverify.check_quarantine({"refs/heads/x": bad_tree_head}, set(),
                                     Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("commit tree reference pointing at blob rejected",
              not v.ok and f"must be tree but is blob" in last_error(v),
              "must be tree but is blob", last_error(v))
    run.check("type-mismatch chain starts at the head commit",
              v.chain is not None and v.chain[0] == f"commit:{bad_tree_head}",
              f"commit:{bad_tree_head}", v.chain[0] if v.chain else None)

    parent_bad = store_object(store, "commit", commit_payload(tree_oid, [tree_oid]))
    rep = gitverify.check_quarantine({"refs/heads/x": parent_bad}, set(),
                                     Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("commit parent pointing at tree rejected",
              not v.ok and "must be commit but is tree" in last_error(v),
              "must be commit but is tree", last_error(v))

    file_to_tree = store_object(store, "tree",
                                tree_entry(0o100644, b"d", tree_oid))
    file_head = store_object(store, "commit", commit_payload(file_to_tree))
    rep = gitverify.check_quarantine({"refs/heads/x": file_head}, set(),
                                     Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("regular-file entry pointing at tree rejected",
              not v.ok and "must be blob but is tree" in last_error(v),
              "must be blob but is tree", last_error(v))
    return run


# ------------------------------------------------------------------- C02


def case_c02() -> CaseRun:
    run = CaseRun("C02", "tree raw-name duplicates/separators, entry target "
                         "types, gitlink not expanded", "c02-fixed-1")
    store: dict = {}
    blob_oid = store_object(store, "blob", b"data\n")
    link_oid = store_object(store, "blob", b"target-path")
    sub_tree = store_object(store, "tree", b"")
    gitlink_oid = "f" * 40  # external repo commit id, deliberately absent

    good_tree = store_object(store, "tree", b"".join([
        tree_entry(0o100644, b"file.txt", blob_oid),
        tree_entry(0o100755, b"run.sh", blob_oid),
        tree_entry(0o120000, b"link", link_oid),
        tree_entry(0o040000, b"dir", sub_tree),
        tree_entry(0o160000, b"submodule", gitlink_oid),
    ]))
    head = store_object(store, "commit", commit_payload(good_tree))
    adapter = Adapter(store)
    rep = gitverify.check_quarantine({"refs/heads/ok": head}, set(), adapter)
    v = rep.branches["refs/heads/ok"]
    run.check("file/link/dir/gitlink tree accepted", rep.ok and v.ok,
              "ok", v.chain)
    run.check("gitlink object id never requested from adapter",
              gitlink_oid not in adapter.calls,
              "not read", adapter.calls.count(gitlink_oid))
    run.check("gitlink absent from store yet branch still accepted",
              gitlink_oid not in store and v.ok, "accepted", v.ok)

    dup_tree = store_object(store, "tree",
                            tree_entry(0o100644, b"same", blob_oid)
                            + tree_entry(0o100644, b"same", link_oid))
    dup_head = store_object(store, "commit", commit_payload(dup_tree))
    rep = gitverify.check_quarantine({"refs/heads/x": dup_head}, set(), Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("duplicate raw file name rejected",
              not v.ok and "duplicated" in last_error(v),
              "duplicated", last_error(v))

    sep_tree = store_object(store, "tree",
                            tree_entry(0o100644, b"a/b", blob_oid))
    sep_head = store_object(store, "commit", commit_payload(sep_tree))
    rep = gitverify.check_quarantine({"refs/heads/x": sep_head}, set(), Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("name containing path separator rejected",
              not v.ok and "path separator" in last_error(v),
              "path separator", last_error(v))

    link_to_tree = store_object(store, "tree",
                                tree_entry(0o120000, b"l", sub_tree))
    link_head = store_object(store, "commit", commit_payload(link_to_tree))
    rep = gitverify.check_quarantine({"refs/heads/x": link_head}, set(), Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("symlink entry pointing at tree rejected",
              not v.ok and "must be blob but is tree" in last_error(v),
              "must be blob but is tree", last_error(v))

    dir_to_blob = store_object(store, "tree",
                               tree_entry(0o040000, b"d", blob_oid))
    dir_head = store_object(store, "commit", commit_payload(dir_to_blob))
    rep = gitverify.check_quarantine({"refs/heads/x": dir_head}, set(), Adapter(store))
    v = rep.branches["refs/heads/x"]
    run.check("directory entry pointing at blob rejected",
              not v.ok and "must be tree but is blob" in last_error(v),
              "must be tree but is blob", last_error(v))
    return run


# ------------------------------------------------------------------- C03


def case_c03() -> CaseRun:
    run = CaseRun("C03", "shared objects counted once, trusted boundary stops "
                         "expansion, unregistered missing object blocks",
                  "c03-fixed-1")
    store: dict = {}
    blob_oid = store_object(store, "blob", b"shared-bytes\n")
    tree_oid = store_object(store, "tree", tree_entry(0o100644, b"s", blob_oid))
    base = store_object(store, "commit", commit_payload(tree_oid))
    tip = store_object(store, "commit", commit_payload(tree_oid, [base]))

    adapter = Adapter(store)
    rep = gitverify.check_quarantine(
        {"refs/heads/a": base, "refs/heads/b": tip}, set(), adapter)
    run.check("both branches sharing objects accepted", rep.ok,
              "ok", {k: v.ok for k, v in rep.branches.items()})
    run.check("shared objects verified exactly once",
              rep.objects_verified == 4,
              "4 unique objects", rep.objects_verified)
    expected_bytes = sum(inflated_size(t, p) for t, p in [
        ("blob", b"shared-bytes\n"),
        ("tree", tree_entry(0o100644, b"s", blob_oid)),
        ("commit", commit_payload(tree_oid)),
        ("commit", commit_payload(tree_oid, [base])),
    ])
    run.check("cumulative bytes count shared objects once",
              rep.bytes_expanded == expected_bytes,
              expected_bytes, rep.bytes_expanded)
    run.check("adapter never asked twice for the shared tree",
              adapter.calls.count(tree_oid) == 1,
              "1 read", adapter.calls.count(tree_oid))

    ghost_parent = "e" * 40  # trusted old object, not present in the store
    bounded = store_object(store, "commit", commit_payload(tree_oid, [ghost_parent]))
    adapter2 = Adapter(store)
    rep = gitverify.check_quarantine({"refs/heads/c": bounded}, {ghost_parent},
                                     adapter2)
    v = rep.branches["refs/heads/c"]
    run.check("trusted boundary object stops expansion and is accepted",
              rep.ok and v.ok, "ok", v.chain)
    run.check("trusted boundary object never read",
              ghost_parent not in adapter2.calls,
              "not read", adapter2.calls.count(ghost_parent))
    run.check("trusted boundary reported as trusted",
              rep.objects_trusted == 1, "1", rep.objects_trusted)

    missing_tree = "d" * 40
    broken = store_object(store, "commit", commit_payload(missing_tree))
    rep = gitverify.check_quarantine({"refs/heads/d": broken}, set(), Adapter(store))
    v = rep.branches["refs/heads/d"]
    run.check("unregistered unreadable object blocks the branch",
              not v.ok and "not registered as trusted" in last_error(v),
              "missing-object error", last_error(v))
    run.check("missing-object chain is commit -> missing -> error",
              v.chain is not None
              and v.chain[0] == f"commit:{broken}"
              and v.chain[1] == f"missing:{missing_tree}",
              f"[commit:{broken}, missing:{missing_tree}, error]", v.chain)
    return run


# ------------------------------------------------------------------- C04


def _budget_store():
    store: dict = {}
    big_blob = store_object(store, "blob", b"B" * 256)
    small_blob = store_object(store, "blob", b"tiny\n")
    big_tree = store_object(store, "tree", tree_entry(0o100644, b"big", big_blob))
    small_tree = store_object(store, "tree",
                              tree_entry(0o100644, b"small", small_blob))
    big_head = store_object(store, "commit", commit_payload(big_tree))
    small_head = store_object(store, "commit", commit_payload(small_tree))
    ids = {
        "big_blob": big_blob, "small_blob": small_blob,
        "big_tree": big_tree, "small_tree": small_tree,
        "big_head": big_head, "small_head": small_head,
    }
    return store, ids


def case_c04() -> CaseRun:
    run = CaseRun("C04", "single-object / cumulative-byte / object-count "
                         "budgets, per-branch shortest error chains, content "
                         "never executed", "c04-fixed-1")

    # -- single-object size budget: only the oversized branch fails, no halt
    store, ids = _budget_store()
    big_blob_size = inflated_size("blob", b"B" * 256)
    adapter = Adapter(store)
    rep = gitverify.check_quarantine(
        {"refs/heads/big": ids["big_head"],
         "refs/heads/small": ids["small_head"]},
        set(), adapter, max_object_bytes=big_blob_size - 1)
    vb, vs = rep.branches["refs/heads/big"], rep.branches["refs/heads/small"]
    run.check("single-object budget trips the oversized blob",
              not vb.ok and "single-object expanded size" in last_error(vb),
              "single-object budget error", last_error(vb))
    run.check("single-object violation does not halt other branches",
              vs.ok and not rep.halted, "small branch ok, no halt",
              f"small ok={vs.ok} halted={rep.halted}")
    run.check("single-object error chain is commit -> tree -> object -> error",
              vb.chain == [f"commit:{ids['big_head']}",
                           f"tree:{ids['big_tree']}",
                           f"object:{ids['big_blob']}",
                           vb.chain[-1]] and len(vb.chain) == 4,
              "4-node chain (budget trip before header parse)", vb.chain)

    # -- cumulative byte budget: trips at the last object, halts expansion
    store, ids = _budget_store()
    total = (inflated_size("commit", commit_payload(ids["big_tree"]))
             + inflated_size("tree", tree_entry(0o100644, b"big", ids["big_blob"]))
             + big_blob_size)
    adapter = Adapter(store)
    rep = gitverify.check_quarantine({"refs/heads/big": ids["big_head"]},
                                     set(), adapter, max_total_bytes=total - 1)
    v = rep.branches["refs/heads/big"]
    run.check("cumulative-byte budget trips at the last object",
              not v.ok and "cumulative expanded-byte budget" in last_error(v),
              "cumulative budget error", last_error(v))
    run.check("cumulative trip halts expansion",
              rep.halted, "halted", rep.halted)
    run.check("cumulative error chain ends at the tripping object",
              v.chain is not None and v.chain[-2] == f"object:{ids['big_blob']}",
              f"object:{ids['big_blob']}", v.chain[-2] if v.chain else None)

    # -- object-count budget: tripping object is never read from the adapter
    store, ids = _budget_store()
    adapter = Adapter(store)
    rep = gitverify.check_quarantine({"refs/heads/big": ids["big_head"]},
                                     set(), adapter, max_objects=2)
    v = rep.branches["refs/heads/big"]
    run.check("object-count budget trips on the third new object",
              not v.ok and "object-count budget exceeded" in last_error(v),
              "object-count budget error", last_error(v))
    run.check("object-count trip halts expansion",
              rep.halted, "halted", rep.halted)
    run.check("tripping object never read from adapter",
              ids["big_blob"] not in adapter.calls and len(adapter.calls) == 2,
              "2 reads, blob unread",
              f"reads={len(adapter.calls)} blob_read={ids['big_blob'] in adapter.calls}")

    # -- per-branch shortest chains to a shared bad object
    store2: dict = {}
    bad_blob = store_object(store2, "blob", b"__import__('os').system('true')")
    tampered = dict(store2)
    tampered[bad_blob] = zlib.compress(b"blob 38\0" + b"Z" * 38)  # sha mismatch
    tree_a = store_object(tampered, "tree",
                          tree_entry(0o100644, b"x", bad_blob))
    head_a = store_object(tampered, "commit", commit_payload(tree_a))
    inner = store_object(tampered, "tree",
                         tree_entry(0o100644, b"x", bad_blob))
    tree_b = store_object(tampered, "tree",
                          tree_entry(0o040000, b"sub", inner))
    head_b = store_object(tampered, "commit", commit_payload(tree_b))
    adapter = Adapter(tampered)
    rep = gitverify.check_quarantine(
        {"refs/heads/short": head_a, "refs/heads/long": head_b}, set(), adapter)
    va, vb2 = rep.branches["refs/heads/short"], rep.branches["refs/heads/long"]
    run.check("short branch gets the 4-node shortest chain",
              va.chain == [f"commit:{head_a}", f"tree:{tree_a}",
                           f"blob:{bad_blob}", va.chain[-1]]
              and len(va.chain) == 4 and not va.ok,
              "4-node chain", va.chain)
    run.check("long branch gets its own 5-node shortest chain",
              vb2.chain == [f"commit:{head_b}", f"tree:{tree_b}",
                            f"tree:{inner}", f"blob:{bad_blob}",
                            vb2.chain[-1]] and len(vb2.chain) == 5 and not vb2.ok,
              "5-node chain", vb2.chain)
    run.check("shared bad object still read only once",
              adapter.calls.count(bad_blob) == 1,
              "1 read", adapter.calls.count(bad_blob))

    # -- object content is data, never executed; gitlinks never dereferenced
    src = (Path(__file__).resolve().parent.parent / "gitverify.py").read_text()
    banned = [tok for tok in ("eval(", "exec(", "subprocess", "os.system",
                              "__import__") if tok in src]
    run.check("checker module contains no code-execution primitives",
              not banned, "no eval/exec/subprocess", banned or "clean")
    store3: dict = {}
    gitlink_oid = "c" * 40
    gl_tree = store_object(store3, "tree",
                           tree_entry(0o160000, b"mod", gitlink_oid))
    gl_head = store_object(store3, "commit", commit_payload(gl_tree))
    adapter = Adapter(store3)
    rep = gitverify.check_quarantine({"refs/heads/gl": gl_head}, set(), adapter)
    run.check("gitlink commit id kept but never expanded or read",
              rep.ok and gitlink_oid not in adapter.calls,
              "accepted, gitlink unread",
              f"ok={rep.ok} gitlink_reads={adapter.calls.count(gitlink_oid)}")
    return run


# ------------------------------------------------------------------- main

CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="gitverify local verification")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--case", choices=sorted(CASES), help="run a single case")
    args = parser.parse_args(argv)

    if args.list:
        for cid, fn in sorted(CASES.items()):
            print(f"{cid}: {fn.__doc__ or ''}".strip())
        return 0

    selected = [args.case] if args.case else sorted(CASES)
    summaries = []
    for cid in selected:
        run = CASES[cid]()
        summary = run.summary()
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False))

    bad = [s for s in summaries
           if s["failed"] > 0 or s["assertions"] == 0
           or s["skipped"] == s["assertions"]]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
