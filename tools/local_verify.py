#!/usr/bin/env python3
"""Local verification entry point for the rs_shamir reconstruction library.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01|C02|C03|C04

Each case prints one JSON record with the case id, the real assertion count,
passed/failed/skipped tallies, the seed/sample used, and per-assertion
expected/observed summaries.  Exit code is non-zero if any assertion fails,
if a case has zero assertions, or if a whole case is skipped.  No secret
material or share values are printed -- only digests and share numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rs_shamir import (  # noqa: E402
    INCONSISTENT,
    INSUFFICIENT,
    MAX_SHARES,
    OK,
    PRIME,
    Share,
    generate_shares,
    make_descriptor,
    recover,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self) -> None:
        self.results = []

    def check(self, name, condition, expected, observed):
        self.results.append({
            "name": name,
            "passed": bool(condition),
            "expected": str(expected),
            "observed": str(observed),
        })


def corrupt(shares, indices, seed, columns="all"):
    """Return copies of ``shares`` with the given share numbers tampered."""
    rng = random.Random(seed)
    out = []
    for s in shares:
        if s.index in indices:
            ys = list(s.ys)
            cols = range(len(ys)) if columns == "all" else columns
            for c in cols:
                ys[c] = (ys[c] + rng.randrange(1, PRIME)) % PRIME
            out.append(Share(index=s.index, batch_id=s.batch_id, x=s.x,
                             ys=tuple(ys)))
        else:
            out.append(s)
    return out


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# C01: field, threshold gate t+2e, correct byte recovery
# ---------------------------------------------------------------------------

def case_c01():
    rec = Recorder()
    seed = 20261119
    secret = hashlib.sha256(b"rs-shamir-c01-sample").digest()[:16]
    t, e = 3, 2
    xs = list(range(1, 8))  # exactly t + 2e = 7 shares
    batch = "batch-c01"
    desc = make_descriptor(batch, t, xs, secret)
    shares = generate_shares(secret, t, xs, batch, seed)

    rec.check("field_prime_is_65537", PRIME == 65537 and desc.prime == 65537,
              "prime 65537", f"PRIME={PRIME}, descriptor={desc.prime}")
    in_field = all(0 <= y < PRIME for s in shares for y in s.ys)
    spans_field = any(y > 255 for s in shares for y in s.ys)
    rec.check("share_values_in_field", in_field and spans_field,
              "all y in [0,65536] and some y > 255 (true field arithmetic)",
              f"in_field={in_field}, spans_field={spans_field}")
    rec.check("share_count_and_distinct_x",
              len(shares) == 7 and len({s.x for s in shares}) == 7,
              "7 shares with 7 distinct x", f"{len(shares)} shares")

    res = recover(desc, shares, e)
    rec.check("clean_recovery_ok", res.status == OK, "status ok",
              f"status={res.status}, reason={res.reason}")
    rec.check("recovered_bytes_match",
              res.secret == secret and digest_of(res.secret or b"") == desc.sha256,
              f"sha256={desc.sha256[:16]}...",
              f"match={res.secret == secret}, len={len(res.secret or b'')}")
    rec.check("no_errors_reported", res.error_positions == (),
              "no corrected positions", f"error_positions={res.error_positions}")

    short = recover(desc, shares[: t + 2 * e - 1], e)  # one below the gate
    rec.check("below_t_plus_2e_gate_insufficient",
              short.status == INSUFFICIENT and short.secret is None,
              "insufficient_evidence, no secret",
              f"status={short.status}, reason={short.reason}, secret={short.secret}")

    exact = recover(desc, shares[: t + 2 * e], e)
    rec.check("exact_t_plus_2e_recovers",
              exact.status == OK and exact.secret == secret,
              "ok at exactly t+2e distinct x", f"status={exact.status}")

    return {"seed": seed, "t": t, "e": e, "length": len(secret),
            "secret_sha256": desc.sha256}, rec.results


# ---------------------------------------------------------------------------
# C02: missing shares, duplicate dedup, duplicate conflict, batch mixing
# ---------------------------------------------------------------------------

def case_c02():
    rec = Recorder()
    seed = 20261202
    secret = hashlib.sha256(b"rs-shamir-c02-sample").digest()[:8]
    t, e = 2, 1
    xs = list(range(1, 6))  # 5 shares, need t+2e = 4
    batch = "batch-c02"
    desc = make_descriptor(batch, t, xs, secret)
    shares = generate_shares(secret, t, xs, batch, seed)

    res = recover(desc, shares[:4], e)  # one share missing
    rec.check("one_missing_still_recovers",
              res.status == OK and res.secret == secret,
              "ok with 4 of 5 shares", f"status={res.status}")

    res = recover(desc, shares[:3], e)  # two missing -> below t+2e
    rec.check("two_missing_insufficient",
              res.status == INSUFFICIENT and res.secret is None,
              "insufficient_evidence, no secret",
              f"status={res.status}, reason={res.reason}")

    res = recover(desc, shares + [shares[0]], e)  # identical duplicate
    rec.check("duplicate_same_value_deduped",
              res.status == OK and res.secret == secret,
              "ok, duplicate ignored", f"status={res.status}")

    tampered_dup = corrupt([shares[0]], [shares[0].index], seed + 1,
                           columns=[0])[0]
    tampered_dup = Share(index=6, batch_id=batch, x=shares[0].x,
                         ys=tampered_dup.ys)
    res = recover(desc, shares + [tampered_dup], e)
    rec.check("duplicate_conflict_reported",
              res.status == INCONSISTENT and res.reason == "conflicting_shares"
              and res.secret is None,
              "inconsistent/conflicting_shares, no secret",
              f"status={res.status}, reason={res.reason}")
    rec.check("conflict_diagnostics_list_share_numbers",
              set(res.diagnostics) == {shares[0].index, 6},
              "diagnostics = both conflicting share numbers",
              f"diagnostics={list(res.diagnostics)}")

    foreign = Share(index=3, batch_id="other-batch", x=shares[2].x,
                    ys=shares[2].ys)
    mixed = [foreign if s.index == 3 else s for s in shares]
    res = recover(desc, mixed, e)
    rec.check("batch_mixing_rejected",
              res.status == INCONSISTENT and res.reason == "batch_id_mismatch"
              and res.secret is None,
              "inconsistent/batch_id_mismatch, no secret",
              f"status={res.status}, reason={res.reason}, "
              f"diagnostics={list(res.diagnostics)}")

    stranger = Share(index=7, batch_id=batch, x=99, ys=shares[0].ys)
    res = recover(desc, shares + [stranger], e)
    rec.check("foreign_x_coordinate_rejected",
              res.status == INCONSISTENT
              and res.reason == "unknown_x_coordinate" and res.secret is None,
              "inconsistent/unknown_x_coordinate, no secret",
              f"status={res.status}, reason={res.reason}, "
              f"diagnostics={list(res.diagnostics)}")

    return {"seed": seed, "t": t, "e": e, "length": len(secret),
            "secret_sha256": desc.sha256}, rec.results


# ---------------------------------------------------------------------------
# C03: error budget e, byte constant range, SHA-256 of the original
# ---------------------------------------------------------------------------

def case_c03():
    rec = Recorder()
    seed = 20261203
    secret = hashlib.sha256(b"rs-shamir-c03-sample").digest()[:24]
    t, e = 3, 2
    xs = list(range(1, 8))
    batch = "batch-c03"
    desc = make_descriptor(batch, t, xs, secret)
    shares = generate_shares(secret, t, xs, batch, seed)

    bad_positions = (2, 5)
    corrupted = corrupt(shares, bad_positions, seed + 10)
    res = recover(desc, corrupted, e)
    rec.check("e_errors_corrected",
              res.status == OK and res.secret == secret,
              "ok, original bytes restored", f"status={res.status}")
    rec.check("error_positions_identified",
              tuple(bad_positions) == res.error_positions,
              f"error_positions={bad_positions}",
              f"error_positions={res.error_positions}")
    rec.check("recovered_digest_matches_descriptor",
              res.secret is not None
              and digest_of(res.secret) == desc.sha256,
              f"sha256={desc.sha256[:16]}...",
              "match" if res.secret == secret else "mismatch")

    over = corrupt(shares, (2, 5, 7), seed + 11)  # e+1 = 3 errors
    res = recover(desc, over, e)
    rec.check("e_plus_1_errors_rejected",
              res.status != OK and res.secret is None,
              "failure with no secret beyond budget e",
              f"status={res.status}, reason={res.reason}")

    # union of per-column error positions must stay within e
    t2, e2 = 3, 1
    xs2 = list(range(1, 6))
    secret2 = hashlib.sha256(b"rs-shamir-c03-union").digest()[:8]
    desc2 = make_descriptor(batch, t2, xs2, secret2)
    shares2 = generate_shares(secret2, t2, xs2, batch, seed + 12)
    split = corrupt(shares2, (1,), seed + 13, columns=[0])
    split = corrupt(split, (2,), seed + 14, columns=[1])
    res = recover(desc2, split, e2)
    rec.check("union_of_error_positions_bounded_by_e",
              res.status == INCONSISTENT
              and res.reason == "error_budget_exceeded" and res.secret is None,
              "inconsistent/error_budget_exceeded (2 positions > e=1)",
              f"status={res.status}, reason={res.reason}, "
              f"diagnostics={list(res.diagnostics)}")

    # constant term that cannot be a byte must be rejected
    forged = []
    for s in shares:
        y0 = (300 + 7 * s.x + 9 * s.x * s.x) % PRIME  # deg-2 poly, const 300
        forged.append(Share(index=s.index, batch_id=s.batch_id, x=s.x,
                            ys=(y0,) + s.ys[1:]))
    res = recover(desc, forged, e)
    rec.check("non_byte_constant_rejected",
              res.status == INCONSISTENT
              and res.reason == "constant_term_out_of_byte_range"
              and res.secret is None,
              "inconsistent/constant_term_out_of_byte_range",
              f"status={res.status}, reason={res.reason}")

    wrong_digest = digest_of(b"not-the-real-secret")
    wrong_desc = type(desc)(batch_id=batch, threshold=t, x_coords=tuple(xs),
                            length=len(secret), sha256=wrong_digest)
    res = recover(wrong_desc, shares, e)
    rec.check("digest_mismatch_rejected",
              res.status == INCONSISTENT and res.reason == "digest_mismatch"
              and res.secret is None,
              "inconsistent/digest_mismatch, no secret",
              f"status={res.status}, reason={res.reason}")

    return {"seed": seed, "t": t, "e": e, "length": len(secret),
            "secret_sha256": desc.sha256}, rec.results


# ---------------------------------------------------------------------------
# C04: insufficient/inconsistent classification, max declared scale,
#      no partial secret, diagnostics carry share numbers only
# ---------------------------------------------------------------------------

def case_c04():
    rec = Recorder()
    seed = 20261204

    # insufficient evidence: 5 distinct x but t+2e = 6 required
    t, e = 4, 1
    xs = list(range(1, 7))
    secret = hashlib.sha256(b"rs-shamir-c04-small").digest()[:8]
    batch = "batch-c04"
    desc = make_descriptor(batch, t, xs, secret)
    shares = generate_shares(secret, t, xs, batch, seed)
    res = recover(desc, shares[:5], e)
    rec.check("insufficient_evidence_classification",
              res.status == INSUFFICIENT and res.secret is None,
              "insufficient_evidence, no secret",
              f"status={res.status}, reason={res.reason}")
    rec.check("insufficient_diagnostics_are_share_numbers",
              all(isinstance(i, int) for i in res.diagnostics),
              "diagnostics contain only share numbers",
              f"diagnostics={list(res.diagnostics)}")

    # inconsistent: conflicting duplicates, still no partial secret
    t2, e2 = 2, 1
    xs2 = list(range(1, 6))
    desc2 = make_descriptor(batch, t2, xs2, secret)
    shares2 = generate_shares(secret, t2, xs2, batch, seed + 1)
    clash = corrupt([shares2[1]], [shares2[1].index], seed + 2, columns=[0])[0]
    clash = Share(index=9, batch_id=batch, x=shares2[1].x, ys=clash.ys)
    res = recover(desc2, shares2 + [clash], e2)
    rec.check("inconsistent_classification_no_partial_secret",
              res.status == INCONSISTENT and res.secret is None,
              "inconsistent, secret is None",
              f"status={res.status}, reason={res.reason}, secret={res.secret}")
    rec.check("inconsistent_diagnostics_are_share_numbers",
              all(isinstance(i, int) for i in res.diagnostics)
              and set(res.diagnostics) == {shares2[1].index, 9},
              "diagnostics list only the conflicting share numbers",
              f"diagnostics={list(res.diagnostics)}")

    # maximum declared scale: 31 shares, 4096 bytes, e=8 errors corrected
    t3, e3 = 15, 8
    xs3 = list(range(1, MAX_SHARES + 1))  # 31 coordinates
    big = random.Random(seed + 3).randbytes(4096)
    desc3 = make_descriptor(batch, t3, xs3, big)
    shares3 = generate_shares(big, t3, xs3, batch, seed + 4)
    bad_positions = (1, 4, 9, 13, 18, 22, 27, 31)
    corrupted = corrupt(shares3, bad_positions, seed + 5)
    res = recover(desc3, corrupted, e3)
    rec.check("max_scale_recovery_ok",
              res.status == OK and res.secret == big,
              "ok at 31 shares / 4096 bytes with 8 errors",
              f"status={res.status}, recovered_len={len(res.secret or b'')}")
    rec.check("max_scale_error_positions",
              tuple(bad_positions) == res.error_positions,
              f"error_positions={bad_positions}",
              f"error_positions={res.error_positions}")
    rec.check("max_scale_digest_matches",
              res.secret is not None and digest_of(res.secret) == desc3.sha256,
              f"sha256={desc3.sha256[:16]}...", "match"
              if res.secret == big else "mismatch")

    # beyond declared limits is rejected, never silently truncated
    res = recover(desc3, shares3 + [shares3[0]], e3)  # 32 shares
    rec.check("share_count_limit_enforced",
              res.status == INCONSISTENT and res.reason == "too_many_shares"
              and res.secret is None,
              "inconsistent/too_many_shares above 31",
              f"status={res.status}, reason={res.reason}")

    return {"seed": seed, "max_shares": MAX_SHARES, "max_length": 4096,
            "t": t3, "e": e3, "secret_sha256": desc3.sha256}, rec.results


CASES = {
    "C01": ("field 65537, t+2e gate, correct byte recovery", case_c01),
    "C02": ("missing shares, duplicate dedup/conflict, batch mixing", case_c02),
    "C03": ("error budget e, byte constant range, SHA-256 check", case_c03),
    "C04": ("failure classification, max scale, no partial secret", case_c04),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="local verification runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="list available cases as JSON")
    group.add_argument("--case", choices=sorted(CASES), help="run one case")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"case": cid, "title": title} for cid, (title, _) in
             sorted(CASES.items())], indent=2))
        return 0

    title, fn = CASES[args.case]
    sample, results = fn()
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    skipped = sum(1 for r in results if r.get("skipped"))
    record = {
        "case": args.case,
        "title": title,
        "sample": sample,
        "assertions": len(results),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }
    print(json.dumps(record, indent=2))
    if len(results) == 0 or failed > 0 or skipped == len(results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
