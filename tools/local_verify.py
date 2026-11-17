#!/usr/bin/env python3
"""Local verification harness for the wal_forensics reconstruction module.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C02
    ...

Each case emits one JSON object per line with the case id, the real number of
assertions executed, passed/failed/skipped counts, a sample/seed description
and per-check expected/observed summaries.  The process exits non-zero if any
assertion fails, if a case is unknown, or if a case ran zero assertions.

Sample evidence is generated locally: a throwaway SQLite database created in
a temporary directory (only to *produce* demo bytes -- the evidence itself is
never opened through SQLite by the module under test) plus fully synthetic
WAL segments built byte-by-byte.  No raw evidence content is printed, only
sizes, hashes and short markers.
"""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import wal_forensics as wf  # noqa: E402


# ---------------------------------------------------------------------------
# result bookkeeping
# ---------------------------------------------------------------------------

def _safe(value, limit=120):
    if isinstance(value, (bytes, bytearray)):
        text = "0x" + bytes(value[:16]).hex() + ("..." if len(value) > 16 else "")
    else:
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class CaseResult:
    def __init__(self, case_id, description):
        self.case = case_id
        self.description = description
        self.checks = []
        self.sample = {}

    def check(self, name, expected, observed, ok=None):
        if ok is None:
            ok = expected == observed
        self.checks.append(
            {
                "name": name,
                "ok": bool(ok),
                "expected": _safe(expected),
                "observed": _safe(observed),
            }
        )
        return ok

    def summary(self):
        passed = sum(1 for c in self.checks if c["ok"])
        failed = len(self.checks) - passed
        return {
            "case": self.case,
            "description": self.description,
            "assertions": len(self.checks),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "sample": self.sample,
            "checks": self.checks,
        }


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# sample builders (local synthetic data only)
# ---------------------------------------------------------------------------

def make_demo_evidence(tmpdir):
    """Create a demo database in WAL mode and return (main_bytes, wal_bytes).

    sqlite3 is used here ONLY to manufacture demo evidence bytes.  The module
    under test never touches sqlite3.  wal_autocheckpoint=0 keeps every
    commit inside the WAL so the main file stays the pre-WAL baseline.
    """
    db_path = os.path.join(tmpdir, "demo.db")
    con = sqlite3.connect(db_path)
    con.isolation_level = None  # explicit transactions => deterministic commits
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("BEGIN")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, note TEXT)")
    con.executemany(
        "INSERT INTO t(note) VALUES (?)",
        [("row-%d-" % i + "x" * 220,) for i in range(60)],
    )
    con.execute("COMMIT")
    # checkpoint so the main file becomes the trusted pre-WAL baseline;
    # everything below lives only in the WAL segment under examination
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("BEGIN")
    con.execute("UPDATE t SET note = ? WHERE id = 1", ("updated-" + "y" * 220,))
    con.execute("COMMIT")  # commit 0 (rewrites the page holding rows 1..n)
    con.execute("BEGIN")
    con.execute("UPDATE t SET note = ? WHERE id = 2", ("secondup-" + "w" * 220,))
    con.execute("COMMIT")  # commit 1 (overwrites the SAME page again)
    con.execute("BEGIN")
    con.execute("INSERT INTO t(note) VALUES (?)", ("row-extra-" + "z" * 220,))
    con.execute("COMMIT")  # commit 2
    with open(db_path, "rb") as fh:
        main_bytes = fh.read()
    with open(db_path + "-wal", "rb") as fh:
        wal_bytes = fh.read()
    con.close()
    return main_bytes, wal_bytes


def build_wal(page_size, frames, salt1=0x1BADB002, salt2=0x0D15EA5E, big_endian=False):
    """Build a synthetic WAL segment. frames: [(pgno, db_size_after, page_bytes)]."""
    magic = wf.WAL_MAGIC | (1 if big_endian else 0)
    head24 = struct.pack(">6I", magic, wf.WAL_VERSION, page_size, 0, salt1, salt2)
    s1, s2 = wf.wal_checksum(head24, 0, 0, big_endian)
    out = bytearray(head24 + struct.pack(">2I", s1, s2))
    for pgno, db_size, data in frames:
        if len(data) != page_size:
            raise ValueError("page image size mismatch")
        fh8 = struct.pack(">2I", pgno, db_size)
        s1, s2 = wf.wal_checksum(fh8, s1, s2, big_endian)
        s1, s2 = wf.wal_checksum(data, s1, s2, big_endian)
        out += fh8 + struct.pack(">4I", salt1, salt2, s1, s2) + data
    return bytes(out)


def append_frames(wal_bytes, frames):
    """Append frames to an existing, fully valid WAL (checksum chain continued)."""
    image = wf.parse_wal(wal_bytes)
    if image.stop_reason is not wf.StopReason.CLEAN:
        raise ValueError("base WAL is not clean")
    h = image.header
    page_size = h.page_size
    s1, s2 = h.checksum1, h.checksum2
    for frame in image.frames:
        fh8 = struct.pack(">2I", frame.page_number, frame.db_size_after)
        s1, s2 = wf.wal_checksum(fh8, s1, s2, h.big_endian_checksums)
        s1, s2 = wf.wal_checksum(
            image.read_frame_page(frame.index), s1, s2, h.big_endian_checksums
        )
    out = bytearray(wal_bytes)
    for pgno, db_size, data in frames:
        fh8 = struct.pack(">2I", pgno, db_size)
        s1, s2 = wf.wal_checksum(fh8, s1, s2, h.big_endian_checksums)
        s1, s2 = wf.wal_checksum(data, s1, s2, h.big_endian_checksums)
        out += fh8 + struct.pack(">4I", h.salt1, h.salt2, s1, s2) + data
    return bytes(out)


def page_filled(page_size, tag):
    seed = bytes([tag]) * 4
    block = (seed + hashlib.sha256(seed).digest()) * (page_size // 36 + 1)
    return block[:page_size]


class TrackingReader:
    """Binary file-like wrapper that records every read and refuses writes."""

    def __init__(self, data):
        self._io = io.BytesIO(data)
        self.reads = []

    def read(self, n=-1):
        pos = self._io.tell()
        data = self._io.read(n)
        self.reads.append((pos, len(data)))
        return data

    def seek(self, offset, whence=0):
        return self._io.seek(offset, whence)

    def tell(self):
        return self._io.tell()

    def write(self, *args, **kwargs):
        raise AssertionError("evidence source must stay read-only")

    def writable(self):
        return False


# ---------------------------------------------------------------------------
# C01: header / page size / salts / rolling checksum on a real demo WAL
# ---------------------------------------------------------------------------

def case_c01():
    res = CaseResult("C01", "WAL header, page size, salts and rolling checksums")
    with tempfile.TemporaryDirectory() as tmp:
        main_bytes, wal_bytes = make_demo_evidence(tmp)
    res.sample = {
        "origin": "locally generated demo db (stdlib sqlite3, WAL mode)",
        "main_sha256_16": _sha(main_bytes)[:16],
        "wal_sha256_16": _sha(wal_bytes)[:16],
        "wal_bytes": len(wal_bytes),
    }
    main_hash_before = _sha(main_bytes)
    wal_hash_before = _sha(wal_bytes)

    image = wf.parse_wal(wal_bytes)
    h = image.header

    res.check("magic is a valid WAL magic", "0x377f0682|0x377f0683",
              hex(h.magic), ok=h.magic in (wf.WAL_MAGIC, wf.WAL_MAGIC | 1))
    res.check("format version 3007000", wf.WAL_VERSION, h.version)
    db_page_size = int.from_bytes(main_bytes[16:18], "big")
    db_page_size = 65536 if db_page_size == 1 else db_page_size
    res.check("WAL page size matches main db header page size",
              db_page_size, h.page_size)
    res.check("page size is a power of two within [512, 65536]",
              True, 512 <= h.page_size <= 65536 and not (h.page_size & (h.page_size - 1)))
    head24 = wal_bytes[:24]
    recomputed = wf.wal_checksum(head24, 0, 0, h.big_endian_checksums)
    res.check("header checksum verified independently",
              (h.checksum1, h.checksum2), recomputed)
    frame_salts = set()
    off = wf.WAL_HEADER_SIZE
    for _ in image.frames:
        fs1, fs2 = struct.unpack(">2I", wal_bytes[off + 8:off + 16])
        frame_salts.add((fs1, fs2))
        off += image.frame_size
    res.check("every frame salt pair equals header salts",
              {(h.salt1, h.salt2)}, frame_salts)
    res.check("salts are non-zero (randomly seeded by sqlite)",
              True, (h.salt1, h.salt2) != (0, 0))
    res.check("all frames verified, scan ended clean",
              "clean", image.stop_reason.value)
    expected_frames = (len(wal_bytes) - wf.WAL_HEADER_SIZE) // image.frame_size
    res.check("verified frame count matches WAL size",
              expected_frames, len(image.frames))
    res.check("at least the 3 demo commits are present",
              True, len(image.commits) >= 3)

    module_src = (ROOT / "wal_forensics.py").read_text(encoding="utf-8")
    res.check("module under test never references sqlite3",
              False, "sqlite3" in module_src)
    res.check("evidence bytes unchanged after parsing (read-only)",
              (main_hash_before, wal_hash_before),
              (_sha(main_bytes), _sha(wal_bytes)))
    return res


# ---------------------------------------------------------------------------
# C02: multiple commits, same-page overwrite, commit boundaries, tail frames
# ---------------------------------------------------------------------------

def case_c02():
    res = CaseResult("C02", "commit boundaries, same-page overwrite, uncommitted tail")
    with tempfile.TemporaryDirectory() as tmp:
        main_bytes, wal_bytes = make_demo_evidence(tmp)
    image = wf.parse_wal(wal_bytes)
    res.sample = {
        "origin": "demo db, 3 commits + 2 synthetic uncommitted tail frames",
        "commits": len(image.commits),
        "frames": len(image.frames),
        "page_size": image.page_size,
    }

    res.check("exactly 3 commit boundaries from the demo workload",
              3, len(image.commits))
    boundary_ok = all(
        image.frames[c.end_frame_index].db_size_after != 0 for c in image.commits
    )
    res.check("every commit boundary frame carries a non-zero db size",
              True, boundary_ok)
    interior_ok = all(
        f.db_size_after == 0
        for c in image.commits
        for f in image.frames[c.start_frame_index:c.end_frame_index]
    )
    res.check("non-boundary frames carry db size zero", True, interior_ok)
    sizes = [c.db_size_pages for c in image.commits]
    res.check("commit db sizes are non-zero and sane",
              True, all(0 < s <= 1000000 for s in sizes))

    view0 = image.view(0, main_bytes)
    view1 = image.view(1, main_bytes)
    # locate the page overwritten twice: commit-0 marker before, commit-1 after
    pgno = None
    for candidate in range(2, view0.page_count + 1):
        try:
            if b"updated-" in view0.read_page(candidate) and b"secondup-" in view1.read_page(candidate):
                pgno = candidate
                break
        except wf.WalFormatError:
            continue
    res.check("located hot page overwritten by both commits", True, pgno is not None)
    if pgno is not None:
        ev0 = view0.evidence(pgno)
        ev1 = view1.evidence(pgno)
        res.check("page rewritten by commit 1 comes from a WAL frame in both views",
                  ("wal", "wal"), (ev0.source, ev1.source))
        res.check("commit 1 resolves the page to a later frame than commit 0",
                  True, ev1.frame_index > ev0.frame_index)
        res.check("commit 1 frame index is the commit-1 boundary frame",
                  image.commits[1].end_frame_index, ev1.frame_index)
        res.check("commit 0 view still shows the first update",
                  True, b"updated-" in view0.read_page(pgno))
        res.check("commit 1 view shows the second overwrite",
                  True, b"secondup-" in view1.read_page(pgno))

    # append two uncommitted tail frames (db_size_after == 0) with valid checksums
    tail_page = bytearray(view1.read_page(pgno) if pgno else page_filled(image.page_size, 7))
    tail_page[100:112] = b"TAILGARBAGE!"
    wal_with_tail = append_frames(
        wal_bytes,
        [(pgno or 2, 0, bytes(tail_page)), (3, 0, page_filled(image.page_size, 9))],
    )
    image2 = wf.parse_wal(wal_with_tail)
    res.check("tail frames verified but not committed",
              2, image2.trailing_uncommitted_frames)
    res.check("commit list unchanged by uncommitted tail",
              len(image.commits), len(image2.commits))
    latest = image2.latest_view(main_bytes)
    res.check("tail garbage never enters the final view",
              False, b"TAILGARBAGE!" in latest.read_page(pgno or 2))
    res.check("final view identical to pre-tail view on the hot page",
              view1.read_page(pgno) if pgno else b"",
              latest.read_page(pgno) if pgno else b"")
    return res


# ---------------------------------------------------------------------------
# C03: truncation / checksum corruption / shrinking commit
# ---------------------------------------------------------------------------

def case_c03():
    res = CaseResult("C03", "corruption, truncation and database-shrinking commit")
    page_size = 512
    main = b"".join(page_filled(page_size, 0xA0 + i) for i in range(6))  # 6-page baseline
    frames = [
        (1, 0, page_filled(page_size, 0x11)),
        (2, 0, page_filled(page_size, 0x12)),
        (3, 0, page_filled(page_size, 0x13)),
        (4, 6, page_filled(page_size, 0x14)),  # commit 0: db stays 6 pages
        (1, 3, page_filled(page_size, 0x21)),  # commit 1: SHRINK to 3 pages
        (2, 0, page_filled(page_size, 0x31)),
        (3, 3, page_filled(page_size, 0x32)),  # commit 2 (will be destroyed)
    ]
    wal_ok = build_wal(page_size, frames)
    res.sample = {
        "origin": "fully synthetic WAL (fixed salts, page_size=512)",
        "salt1": "0x1badb002",
        "frames_built": len(frames),
        "main_pages": 6,
    }

    # (a) checksum corruption inside commit 2
    bad = bytearray(wal_ok)
    corrupt_at = wf.WAL_HEADER_SIZE + 6 * (wf.FRAME_HEADER_SIZE + page_size) \
        + wf.FRAME_HEADER_SIZE + 40  # inside frame 6 page data
    bad[corrupt_at] ^= 0xFF
    img_bad = wf.parse_wal(bytes(bad))
    res.check("corruption reported as checksum mismatch",
              "checksum_mismatch", img_bad.stop_reason.value)
    res.check("only the 2 proven commits survive the corruption",
              2, len(img_bad.commits))
    res.check("corrupted frame index is the stop point",
              6, len(img_bad.frames))

    # (b) physical truncation mid-frame inside commit 2
    cut = wf.WAL_HEADER_SIZE + 5 * (wf.FRAME_HEADER_SIZE + page_size) + 100
    img_trunc = wf.parse_wal(wal_ok[:cut])
    res.check("truncation reported as truncated frame",
              "truncated_frame", img_trunc.stop_reason.value)
    res.check("same 2 proven commits survive truncation",
              2, len(img_trunc.commits))

    # (c) views over the surviving commits
    view0 = img_bad.view(0, main)
    view1 = img_bad.view(1, main)
    res.check("commit 0 keeps full 6-page database size", 6, view0.page_count)
    res.check("commit 1 shrinks the database to 3 pages", 3, view1.page_count)
    res.check("page written by commit 0 is served from its WAL frame",
              ("wal", 3), (view0.evidence(4).source, view0.evidence(4).frame_index))
    res.check("page 6 of commit 0 falls back to the main-file baseline",
              "main", view0.evidence(6).source)
    res.check("commit 0 page 6 content matches the baseline image",
              page_filled(page_size, 0xA5), view0.read_page(6))
    out_of_range = False
    try:
        view1.read_page(4)
    except wf.PageOutOfRangeError:
        out_of_range = True
    res.check("page 4 excluded after the shrink commit", True, out_of_range)
    out_of_range = False
    try:
        view1.evidence(6)
    except wf.PageOutOfRangeError:
        out_of_range = True
    res.check("page 6 excluded after the shrink commit", True, out_of_range)
    res.check("shrink commit rewrites page 1 from its own frame",
              ("wal", 4), (view1.evidence(1).source, view1.evidence(1).frame_index))
    res.check("shrink commit page 1 content is the new image",
              page_filled(page_size, 0x21), view1.read_page(1))
    res.check("pages 2-3 of the shrunk db still resolve to commit-0 frames",
              ("wal", "wal"), (view1.evidence(2).source, view1.evidence(3).source))
    return res


# ---------------------------------------------------------------------------
# C04: lazy paging, per-page provenance, read-only evidence
# ---------------------------------------------------------------------------

def case_c04():
    res = CaseResult("C04", "on-demand paging, per-page provenance, read-only evidence")
    with tempfile.TemporaryDirectory() as tmp:
        main_bytes, wal_bytes = make_demo_evidence(tmp)
        main_path = os.path.join(tmp, "evidence-main.db")
        wal_path = os.path.join(tmp, "evidence-wal.db")
        with open(main_path, "wb") as fh:
            fh.write(main_bytes)
        with open(wal_path, "wb") as fh:
            fh.write(wal_bytes)
        os.chmod(main_path, 0o444)
        os.chmod(wal_path, 0o444)

        main_tracking = TrackingReader(main_bytes)
        wal_tracking = TrackingReader(wal_bytes)
        image = wf.parse_wal(wal_tracking)
        parse_reads = sum(n for _, n in wal_tracking.reads)
        res.sample = {
            "origin": "demo db evidence via read-only tracking readers",
            "commits": len(image.commits),
            "main_bytes": len(main_bytes),
            "wal_bytes_read_during_parse": parse_reads,
        }
        res.check("parsing touched only the WAL, never the main file",
                  0, len(main_tracking.reads))

        wal_reads_before_views = len(wal_tracking.reads)
        views = [image.view(i, main_tracking) for i in range(len(image.commits))]
        res.check("building every commit view performed zero page reads",
                  0, len(main_tracking.reads))
        res.check("view construction caused no extra WAL reads",
                  wal_reads_before_views, len(wal_tracking.reads))
        latest = views[-1]

        # one page from the main file: exactly one read of exactly one page
        main_pgno = None
        for candidate in range(1, latest.page_count + 1):
            if latest.evidence(candidate).source == "main":
                main_pgno = candidate
                break
        res.check("found a page served by the main-file baseline",
                  True, main_pgno is not None)
        if main_pgno is not None:
            before = len(main_tracking.reads)
            data = latest.read_page(main_pgno)
            new_reads = main_tracking.reads[before:]
            res.check("exactly one read for one on-demand page", 1, len(new_reads))
            res.check("read hit the exact page offset and size",
                      ((main_pgno - 1) * image.page_size, image.page_size),
                      new_reads[0] if new_reads else None)
            res.check("page bytes match the baseline image",
                      main_bytes[(main_pgno - 1) * image.page_size:
                                 main_pgno * image.page_size],
                      data)

        # per-page provenance across the whole final view
        sources = {}
        provenance_ok = True
        for pgno in range(1, latest.page_count + 1):
            ev = latest.evidence(pgno)
            sources[ev.source] = sources.get(ev.source, 0) + 1
            if ev.source == "wal":
                if not (ev.frame_index is not None
                        and ev.frame_index <= latest.commit.end_frame_index):
                    provenance_ok = False
            elif ev.source != "main":
                provenance_ok = False
        res.check("every page carries main/wal provenance within the commit",
                  True, provenance_ok)
        res.check("both provenance kinds are exercised",
                  True, sources.get("main", 0) > 0 and sources.get("wal", 0) > 0)
        res.sample["provenance_histogram"] = sources

        # no full-database copies: total main bytes read stay tiny
        total_main_read = sum(n for _, n in main_tracking.reads)
        res.check("main-file reads stay far below a full copy",
                  True, total_main_read <= 2 * image.page_size)

        # evidence files opened read-only and never modified
        with open(main_path, "rb") as fh_main, open(wal_path, "rb") as fh_wal:
            res.check("evidence file objects are not writable",
                      (False, False), (fh_main.writable(), fh_wal.writable()))
            reader_main = wf.PagedReader(fh_main)
            reader_wal = wf.PagedReader(fh_wal)
            img2 = wf.parse_wal(reader_wal)
            v = img2.latest_view(reader_main)
            _ = v.read_page(1)
        res.check("evidence content unchanged after all access",
                  (_sha(main_bytes), _sha(wal_bytes)),
                  (_sha(open(main_path, "rb").read()), _sha(open(wal_path, "rb").read())))
    return res


CASES = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}

CASE_DESCRIPTIONS = {
    "C01": "WAL header, page size, salts and rolling checksums on a demo WAL",
    "C02": "commit boundaries via non-zero db size, same-page overwrite, tail frames",
    "C03": "truncation / checksum corruption tolerance and shrinking commits",
    "C04": "on-demand paging, per-page provenance, read-only evidence",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--case", help="run a single case (C01..C04)")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps([
            {"case": cid, "description": CASE_DESCRIPTIONS[cid]}
            for cid in sorted(CASES)
        ], indent=2))
        return 0

    selected = [args.case] if args.case else sorted(CASES)
    exit_code = 0
    for cid in selected:
        fn = CASES.get(cid)
        if fn is None:
            print(json.dumps({"case": cid, "error": "unknown case"}))
            exit_code = 2
            continue
        try:
            result = fn().summary()
        except Exception as exc:  # a crashing case is a failing case
            result = {
                "case": cid,
                "assertions": 0,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        print(json.dumps(result))
        if result["assertions"] == 0 or result["failed"] != 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
