#!/usr/bin/env python3
"""Local verification harness for the RFC 5849 parameter normaliser.

The harness exercises ``oauthlib.oauth1.rfc5849.signature.normalize_parameters``
against independent references and synthetic fixtures only.  It never touches
the network and never prints sampled parameter values (only structural metadata
such as counts, seeds and fingerprints).

Usage::

    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01 C02 C03 C04

Each selected case prints exactly one JSON object per line on stdout.  A human
readable line is written to stderr.  The process exits non-zero if any check
fails, if the implementation entry point is missing, if a case records zero
assertions, or if a case is entirely skipped.

Case overview
-------------
C01  Independent canonicalisation reference: percent-encode first, then sort on
     the encoded name/value; duplicate names and empty values are preserved.
C02  Unicode, pre-existing percent escapes and literal ``+`` are handled without
     any extra decoding; the existing input-type/exception contract holds.
C03  One-shot iterators are accepted; a counting escape function proves repeated
     values are encoded once; the per-call memo is capacity bounded and never
     survives a request.
C04  Time and extra allocation on a highly duplicated and an all-unique input,
     compared with the original algorithm; the original signature tests pass.
"""

from __future__ import annotations

import argparse
import gc
import inspect
import io
import json
import random
import sys
import time
import tracemalloc
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oauthlib.oauth1.rfc5849 import signature as sig_mod  # noqa: E402
from oauthlib.oauth1.rfc5849 import utils as oauth1_utils  # noqa: E402

SEED = 5849  # deterministic, RFC-numbered seed for every fixture

CASES = {
    "C01": "percent-encode-then-sort vs. an independent reference; duplicates/empties preserved",
    "C02": "Unicode, existing percent escapes and '+', no extra decoding, exception contract",
    "C03": "one-shot iterators, counted duplicate-encoding reduction, bounded per-call memo",
    "C04": "time/allocation on duplicated and unique inputs; original signature tests pass",
}


# ---------------------------------------------------------------------------
# Independent reference implementations (not used by the production code)
# ---------------------------------------------------------------------------

def reference_escape(value):
    """Percent-encode per RFC 3986 unreserved set, implemented from scratch."""
    from urllib.parse import quote

    if not isinstance(value, str):
        raise ValueError("reference escape expects str")
    return quote(value, safe="~")


def reference_normalize(params):
    """Small, independent canonicaliser: encode, then sort, then join."""
    encoded = []
    for name, value in params:
        encoded.append((reference_escape(name), reference_escape(value)))
    encoded.sort()
    return "&".join(f"{name}={value}" for name, value in encoded)


def baseline_normalize(params):
    """Verbatim pre-optimisation algorithm, used for behaviour/bench baselines."""
    key_values = [
        (oauth1_utils.escape(k), oauth1_utils.escape(v)) for k, v in params
    ]
    key_values.sort()
    parameter_parts = ["{}={}".format(k, v) for k, v in key_values]
    return "&".join(parameter_parts)


def fingerprint(text):
    """Short non-reversible fingerprint so output carries no raw samples."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# One-shot iterator and counting escape helpers
# ---------------------------------------------------------------------------

class OneShotPairs:
    """Iterable that may be traversed exactly once and counts iterations."""

    def __init__(self, pairs):
        self._pairs = list(pairs)
        self.iterations = 0
        self.consumed = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations > 1:
            raise RuntimeError("one-shot iterator traversed more than once")
        for pair in self._pairs:
            self.consumed += 1
            yield pair


class CountingEscape:
    """Context manager replacing ``utils.escape`` with a call counter."""

    def __init__(self, observe_cache=False):
        self.calls = []
        self.cache_sizes = []
        self.observe_cache = observe_cache
        self._original = None

    def __enter__(self):
        self._original = oauth1_utils.escape
        oauth1_utils.escape = self
        return self

    def __exit__(self, *exc_info):
        oauth1_utils.escape = self._original
        return False

    def __call__(self, value):
        self.calls.append(value)
        if self.observe_cache:
            frame = inspect.currentframe().f_back
            try:
                cache = frame.f_locals.get("cache") if frame is not None else None
                if isinstance(cache, dict):
                    self.cache_sizes.append(len(cache))
            finally:
                del frame
        return self._original(value)


def scan_for_value(needle):
    """Return paths of module-level containers or live dicts holding *needle*."""
    hits = []

    def walk(obj, path, depth, seen):
        if depth > 3 or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, dict):
            for key, value in list(obj.items()):
                if key == needle or value == needle:
                    hits.append(path + "<dict>")
                    continue
                if isinstance(key, (dict, list, tuple, set, frozenset)):
                    walk(key, path, depth + 1, seen)
                if isinstance(value, (dict, list, tuple, set, frozenset)):
                    walk(value, path, depth + 1, seen)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for item in list(obj):
                if item == needle:
                    hits.append(path)
                elif isinstance(item, (dict, list, tuple, set)):
                    walk(item, path, depth + 1, seen)

    seen = set()
    for name, value in list(vars(sig_mod).items()):
        if name.startswith("__"):
            continue
        walk(value, f"signature.{name}", 0, seen)

    # Any other live container (e.g. a defunct module cache) would also be a
    # retention bug; scan dicts tracked by the collector one level deep.
    for obj in gc.get_objects():
        if isinstance(obj, dict) and obj is not vars(sig_mod):
            try:
                if needle in obj or needle in obj.values():
                    hits.append("<gc:dict>")
            except TypeError:
                continue
    return hits


# ---------------------------------------------------------------------------
# Minimal assertion runner: every check is counted
# ---------------------------------------------------------------------------

class CaseRunner:
    def __init__(self, case_id):
        self.case_id = case_id
        self.assertions = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []
        self.notes = []

    def check(self, condition, label, expected=None, observed=None):
        self.assertions += 1
        if not condition:
            self.failed += 1
            self.failures.append({
                "label": label,
                "expected": _short(expected),
                "observed": _short(observed),
            })
        return bool(condition)

    def equal(self, expected, observed, label):
        return self.check(expected == observed, label, expected, observed)

    def note(self, text):
        self.notes.append(text)

    @property
    def passed(self):
        return self.assertions - self.failed

    @property
    def status(self):
        if self.assertions == 0 or self.failed:
            return "fail"
        if self.skipped and self.passed == 0:
            return "fail"
        return "pass"


def _short(value, limit=120):
    text = repr(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def require_api(runner):
    """Confirm the entry point and documented constant exist."""
    fn = getattr(sig_mod, "normalize_parameters", None)
    present = callable(fn)
    runner.check(present, "normalize_parameters is present and callable",
                 "callable", type(fn).__name__)
    limit = getattr(sig_mod, "ESCAPE_CACHE_LIMIT", None)
    runner.check(isinstance(limit, int) and limit > 0,
                 "ESCAPE_CACHE_LIMIT is a positive int",
                 "positive int", repr(limit))
    return fn if present else None


# ---------------------------------------------------------------------------
# C01: encode first, then sort; duplicates and empty values preserved
# ---------------------------------------------------------------------------

def case_c01(runner):
    normalize = require_api(runner)
    if normalize is None:
        return

    rng = random.Random(SEED)
    names = ["alpha", "dup", "k@", "k0", "a b", "a!b", "empty", "z~y", "A-1", "_x"]
    values = ["", "v", "a b", "x=y", "%20", "0", "@", "~", "dup-val"]
    params = []
    for _ in range(60):
        params.append((rng.choice(names), rng.choice(values)))
        params.append((rng.choice(["dup", "alpha"]), ""))
    # Guarantee duplicate names with both duplicate and distinct values.
    params += [("dup", "one"), ("dup", "one"), ("dup", "two"), ("empty", "")]

    expected = reference_normalize(params)
    observed = normalize(params)
    runner.equal(expected, observed, "matches independent encode-then-sort reference")

    # Every input pair survives exactly once, including duplicates and empties.
    expected_segments = Counter(
        f"{reference_escape(k)}={reference_escape(v)}" for k, v in params
    )
    observed_segments = Counter(observed.split("&")) if observed else Counter()
    runner.equal(expected_segments, observed_segments,
                 "all duplicate/empty pairs preserved with multiplicity")
    runner.equal(len(params), sum(observed_segments.values()),
                 "segment count equals input pair count")

    # Encode-then-sort differs from a naive sort-then-encode for this vector.
    naive = sorted(params)
    naive_encoded = "&".join(
        f"{reference_escape(k)}={reference_escape(v)}" for k, v in naive
    )
    reversal = [("k@", "a"), ("k0", "b")]
    reversal_observed = normalize(reversal)
    reversal_reference = reference_normalize(reversal)
    runner.equal("k%40=a&k0=b", reversal_reference,
                 "reference proves encoding changes ordering")
    runner.equal(reversal_reference, reversal_observed,
                 "implementation sorts on encoded name, not raw name")
    runner.check(naive_encoded != observed,
                 "naive raw-then-encode ordering would differ here",
                 "different", "same")

    # Percent-encoded name/value comparison is what decides order.
    runner.check(reversal_observed.index("k%40") < reversal_observed.index("k0"),
                 "encoded '%' name sorts before digit name",
                 "k%40 before k0", reversal_observed)

    # Empty values still emit the '=' separator.
    only_empty = normalize([("b", ""), ("a", "")])
    runner.equal("a=&b=", only_empty, "empty values keep the '=' separator")

    runner.note(f"{len(params)} pairs, {len(expected_segments)} distinct encoded segments, "
                f"output fingerprint {fingerprint(observed)}")


# ---------------------------------------------------------------------------
# C02: Unicode / pre-existing percent / plus; no extra decoding; exceptions
# ---------------------------------------------------------------------------

def case_c02(runner):
    normalize = require_api(runner)
    if normalize is None:
        return

    vectors = [
        ("café", "caf%C3%A9"),
        ("é", "e%CC%81"),
        ("a%20b", "a%2520b"),      # existing % must be encoded again
        ("a+b", "a%2Bb"),           # '+' is data, not a space
        ("100%", "100%25"),
        ("~_.-", "~_.-"),           # unreserved characters survive
        ("space here", "space%20here"),
        ("emoji\U0001f600", "emoji%F0%9F%98%80"),
        ("", ""),
    ]
    for raw, escaped in vectors:
        observed = normalize([(raw, raw)])
        runner.equal(f"{escaped}={escaped}", observed,
                     f"no extra decode for {fingerprint(raw)}")

    # No decoding happened: a decoded space would appear where '%20' was.
    percent_out = normalize([("q", "a%20b")])
    runner.check("%2520" in percent_out, "pre-existing percent is re-encoded",
                 "%2520 present", percent_out)
    runner.check("a b" not in percent_out, "percent escape is not decoded to space",
                 "no literal space", "literal space found")
    plus_out = normalize([("q", "a+b")])
    runner.check("%2B" in plus_out, "'+' encoded as %2B",
                 "%2B present", plus_out)
    runner.check("a b" not in plus_out, "'+' not decoded to space",
                 "no literal space", "literal space found")

    # Unicode names sort by their encoded (ASCII) form, after encoding.
    unicode_order = normalize([("z", "1"), ("é", "2")])
    runner.equal("%C3%A9=2&z=1", unicode_order,
                 "non-ASCII name sorts by encoded bytes")

    # Input-type / exception contract mirrors the original implementation.
    bad_inputs = [
        ("int value", [("a", 1)]),
        ("bytes value", [("a", b"x")]),
        ("None name", [(None, "v")]),
        ("three-tuple", [("a", "b", "c")]),
        ("params None", None),
    ]
    for label, bad in bad_inputs:
        got_impl = _capture_exception(normalize, bad)
        got_base = _capture_exception(baseline_normalize, bad)
        runner.equal(got_base[0], got_impl[0], f"{label}: same exception type")
        runner.equal(got_base[1], got_impl[1], f"{label}: same exception message")

    value_error = _capture_exception(normalize, [("a", 1)])
    runner.check(value_error[0] == "ValueError", "non-str value raises ValueError",
                 "ValueError", value_error[0])
    runner.check(value_error[1].startswith("Only unicode objects are escapable."),
                 "non-str value keeps the documented message prefix",
                 "Only unicode objects are escapable.", value_error[1])

    runner.note(f"{len(vectors)} literal vectors, unicode ordering, "
                f"{len(bad_inputs)} exception-contract probes")


def _capture_exception(fn, argument):
    try:
        fn(argument)
    except Exception as exc:  # noqa: BLE001 - contract probe
        return type(exc).__name__, str(exc)
    return "no-exception", ""


# ---------------------------------------------------------------------------
# C03: one-shot iterators, counted encoding, bounded per-call memo
# ---------------------------------------------------------------------------

def case_c03(runner):
    normalize = require_api(runner)
    if normalize is None:
        return
    limit = getattr(sig_mod, "ESCAPE_CACHE_LIMIT", 0)

    # --- one-shot iterator is accepted and traversed exactly once ----------
    source = OneShotPairs([("b", "2"), ("a", "1"), ("b", "")])
    observed = normalize(source)
    runner.equal("a=1&b=&b=2", observed, "one-shot iterator normalised correctly")
    runner.equal(1, source.iterations, "iterator traversed exactly once")
    runner.equal(3, source.consumed, "every pair yielded once")

    # --- repeated values are encoded fewer times ---------------------------
    dup_names = ["user", "tag", "filter", "sort", "include"]
    dup_values = ["red", "green", "blue", "amber", "clear"]
    duplicated = [
        (dup_names[i % len(dup_names)], dup_values[(i * 3) % len(dup_values)])
        for i in range(200)
    ]
    distinct = len(set(dup_names) | set(dup_values))
    with CountingEscape() as counter:
        dup_out = normalize(duplicated)
    runner.equal(reference_normalize(duplicated), dup_out,
                 "duplicated input still canonical")
    runner.equal(distinct, len(counter.calls),
                 "duplicates encoded once each (not once per occurrence)")
    runner.check(len(counter.calls) * 10 < 2 * len(duplicated),
                 "encoding calls far below the uncached 2N bound",
                 f"< {2 * len(duplicated) // 10}", len(counter.calls))
    per_value = Counter(counter.calls)
    runner.equal(distinct, len(per_value), "each distinct string encoded")
    runner.check(all(count == 1 for count in per_value.values()),
                 "no distinct string encoded twice within one call",
                 "all counts == 1", sorted(set(per_value.values())))

    # --- capacity cap on a call with more distinct strings than the cap ----
    unique = [(f"name-{i:05d}", f"value-{i:05d}") for i in range(1200)]
    with CountingEscape(observe_cache=True) as cap_counter:
        unique_out = normalize(unique)
    runner.equal(reference_normalize(unique), unique_out,
                 "all-unique input still canonical")
    runner.equal(2 * len(unique), len(cap_counter.calls),
                 "all-unique input encodes every item once (no reuse possible)")
    max_cache = max(cap_counter.cache_sizes) if cap_counter.cache_sizes else 0
    runner.check(max_cache <= limit + 1,
                 "per-call memo never exceeds the declared cap",
                 f"<= {limit + 1}", max_cache)
    runner.check(max_cache >= limit,
                 "cap is actually reached for a large distinct input",
                 f">= {limit}", max_cache)
    runner.check(len(unique) * 2 > limit * 4,
                 "fixture has many more distinct strings than the cap",
                 f"> {limit * 4}", len(unique) * 2)

    # --- reuse does not cross calls ---------------------------------------
    repeat_input = [("k1", "v1"), ("k2", "v2"), ("k1", "v1")]
    with CountingEscape() as cross_counter:
        normalize(repeat_input)
        normalize(repeat_input)
    runner.equal(2 * len(set(k for k, _ in repeat_input)
                         | set(v for _, v in repeat_input)),
                 len(cross_counter.calls),
                 "each call encodes independently; memo does not persist")

    # --- no credential fragments retained after the call -------------------
    canary = "c4n4ry--unique--credential--fragment"
    payload = [("oauth_consumer_key", canary), ("x-" + canary, canary)]
    normalize(payload)
    del payload
    gc.collect()
    hits = scan_for_value(canary)
    runner.equal([], hits, "no module/gc container retains the canary value")
    runner.check(not hasattr(sig_mod, "cache"),
                 "normaliser keeps no module-level cache", "absent",
                 "present" if hasattr(sig_mod, "cache") else "absent")

    runner.note(f"seed={SEED}; duplicated=200 pairs/{distinct} distinct -> "
                f"{len(counter.calls)} encodes; unique=1200 pairs -> "
                f"{len(cap_counter.calls)} encodes, memo peak {max_cache}/{limit}")


# ---------------------------------------------------------------------------
# C04: benchmarks plus the original signature test suite
# ---------------------------------------------------------------------------

def _measure_time(fn, params, repeats=11):
    fn(params)  # warm-up
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn(params)
        best = min(best, time.perf_counter() - start)
    return best


def _measure_peak(fn, params, repeats=5):
    best = None
    for _ in range(repeats):
        gc.collect()
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            fn(params)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        best = peak if best is None else min(best, peak)
    return best


def case_c04(runner):
    normalize = require_api(runner)
    if normalize is None:
        return

    rng = random.Random(SEED)
    dup_names = ["user", "tag", "filter", "sort", "include", "group"]
    dup_values = [f"value-{i} with space & odd=%chars" for i in range(12)]
    duplicated = [
        (rng.choice(dup_names), rng.choice(dup_values)) for _ in range(4000)
    ]
    unique = [
        (f"key-{i}-{rng.randrange(10**9)}", f"value {i} / % x") for i in range(3000)
    ]

    runner.equal(reference_normalize(duplicated), normalize(duplicated),
                 "duplicated fixture is canonical")
    runner.equal(reference_normalize(unique), normalize(unique),
                 "unique fixture is canonical")

    dup_base_time = _measure_time(baseline_normalize, duplicated)
    dup_opt_time = _measure_time(normalize, duplicated)
    dup_base_peak = _measure_peak(baseline_normalize, duplicated)
    dup_opt_peak = _measure_peak(normalize, duplicated)

    uniq_base_time = _measure_time(baseline_normalize, unique)
    uniq_opt_time = _measure_time(normalize, unique)
    uniq_base_peak = _measure_peak(baseline_normalize, unique)
    uniq_opt_peak = _measure_peak(normalize, unique)

    dup_time_ratio = dup_opt_time / dup_base_time
    dup_peak_ratio = dup_opt_peak / dup_base_peak
    uniq_time_ratio = uniq_opt_time / uniq_base_time
    uniq_peak_ratio = uniq_opt_peak / uniq_base_peak

    runner.check(dup_time_ratio < 0.60,
                 "duplicated input runs materially faster than original",
                 "< 0.60x baseline", round(dup_time_ratio, 3))
    runner.check(dup_peak_ratio < 0.90,
                 "duplicated input allocates less peak memory",
                 "< 0.90x baseline", round(dup_peak_ratio, 3))
    runner.check(uniq_time_ratio < 1.60,
                 "all-unique input has no time blow-up",
                 "< 1.60x baseline", round(uniq_time_ratio, 3))
    runner.check(uniq_peak_ratio <= 1.10,
                 "all-unique input does not allocate more peak memory",
                 "<= 1.10x baseline", round(uniq_peak_ratio, 3))

    # The original signature test module must keep passing.
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.oauth1.rfc5849.test_signatures")
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    runner.check(result.testsRun >= 16, "original signature tests were collected",
                 ">= 16 tests", result.testsRun)
    runner.equal([], [repr(t) for t, _ in result.failures],
                 "original signature tests have no failures")
    runner.equal([], [repr(t) for t, _ in result.errors],
                 "original signature tests have no errors")

    runner.note(
        "dup: %.1fms -> %.1fms (%.2fx), peak %dB -> %dB (%.2fx); "
        "unique: %.1fms -> %.1fms (%.2fx), peak %dB -> %dB (%.2fx); "
        "signature tests run=%d"
        % (
            dup_base_time * 1e3, dup_opt_time * 1e3, dup_time_ratio,
            dup_base_peak, dup_opt_peak, dup_peak_ratio,
            uniq_base_time * 1e3, uniq_opt_time * 1e3, uniq_time_ratio,
            uniq_base_peak, uniq_opt_peak, uniq_peak_ratio,
            result.testsRun,
        )
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CASE_FUNCS = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def run_case(case_id):
    runner = CaseRunner(case_id)
    try:
        CASE_FUNCS[case_id](runner)
    except Exception as exc:  # noqa: BLE001 - report as a failed assertion
        runner.assertions += 1
        runner.failed += 1
        runner.failures.append({
            "label": "unexpected exception while running case",
            "expected": "no exception",
            "observed": f"{type(exc).__name__}: {exc}",
        })
    if runner.assertions == 0:
        runner.failures.append({
            "label": "zero assertions executed",
            "expected": "> 0",
            "observed": 0,
        })
        runner.failed += 1
        runner.assertions += 1
    record = {
        "case": case_id,
        "title": CASES[case_id],
        "seed": SEED,
        "assertions": runner.assertions,
        "passed": runner.passed,
        "failed": runner.failed,
        "skipped": runner.skipped,
        "status": runner.status,
        "sample_or_seed": {
            "seed": SEED,
            "notes": runner.notes,
        },
        "failures": runner.failures,
    }
    if record["assertions"] == 0:
        record["status"] = "fail"
    return record


def parse_case_ids(raw):
    ids = []
    for group in raw or []:
        for token in group:
            ids.extend(part.strip() for part in token.split(",") if part.strip())
    return ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true",
                        help="list available cases as JSON and exit")
    parser.add_argument("--case", action="append", nargs="+", metavar="ID",
                        help="case id(s) to run: C01 C02 C03 C04")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({
            "cases": [{"case": cid, "title": title} for cid, title in CASES.items()]
        }))
        return 0

    selected = parse_case_ids(args.case) or list(CASES)
    unknown = [cid for cid in selected if cid not in CASES]
    if unknown:
        print(json.dumps({"error": "unknown case id(s)", "unknown": unknown}),
              file=sys.stderr)
        return 2

    overall_ok = True
    for case_id in selected:
        record = run_case(case_id)
        print(json.dumps(record, ensure_ascii=False))
        print(f"[{case_id}] {record['status']}: "
              f"{record['passed']}/{record['assertions']} assertions passed"
              + (f", {record['failed']} failed" if record["failed"] else "")
              + (f", {record['skipped']} skipped" if record["skipped"] else ""),
              file=sys.stderr)
        if record["status"] != "pass":
            overall_ok = False
            for failure in record["failures"]:
                print(f"  - {failure['label']}: expected {failure['expected']}, "
                      f"observed {failure['observed']}", file=sys.stderr)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
