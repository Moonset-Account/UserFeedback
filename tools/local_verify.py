#!/usr/bin/env python3
"""Local verification harness for the streaming JSON redactor.

Usage:
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01

Each case emits one JSON record with the case id, the real assertion count,
passed/failed/skipped tallies and per-check expected/observed summaries.
Exit code is non-zero if any assertion fails, the case is unknown, or a
case ran zero assertions.  Check details carry structural summaries only --
no raw sensitive sample values are printed.
"""

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stream_redactor import (  # noqa: E402
    ERR_BUDGET,
    ERR_MALFORMED,
    StreamRedactor,
)

# Synthetic phrases used by the cases.  They are test fixtures, not real
# credentials; even so, check details never echo them back.
SECRET_WORD = "机密"  # U+673A U+5BC6
FIRE = "\U0001F525"  # 🔥, surrogate pair D83D DD25 when \u-escaped


def feed_all(redactor, data, chunk_size=None):
    """Feed `data` (optionally in fixed-size chunks) and collect output."""
    out = bytearray()
    if chunk_size is None:
        out += redactor.feed(data)
    else:
        for i in range(0, len(data), chunk_size):
            out += redactor.feed(data[i : i + chunk_size])
    out += redactor.finish()
    return bytes(out)


def decode_lines(out):
    return [json.loads(line) for line in out.split(b"\n") if line.strip()]


# ---------------------------------------------------------------- C01
def case_c01(check):
    """Scalar matching at selected paths; untouched bytes stay identical."""
    paths = ["user.name", "user.id", "s"]
    phrases = [SECRET_WORD, "token"]
    record = (
        b'{ "user" : { "name" : "\\u673a\\u5bc6\\u6587\\u4ef6", "id" : "token-123" },'
        b' "note" : "keep ' + SECRET_WORD.encode("utf-8") + b' here",'
        b' "n" : 1.50e3,'
        b' "s" : "\\u0041\\n' + SECRET_WORD[0].encode("utf-8") + b'\\u5bc6",'
        b' "esc" : "\\u00e9clair" }'
    )
    redactor = StreamRedactor(paths, phrases, "[MASKED]")
    out = feed_all(redactor, record + b"\n")
    check("record processed without errors", redactor.errors == [],
          "errors=%d" % len(redactor.errors))

    doc = None
    try:
        doc = json.loads(out)
    except ValueError:
        pass
    check("output is valid JSON", doc is not None)

    if doc:
        check("escaped-form phrase matched at user.name",
              doc["user"]["name"] == "[MASKED]文件",
              "observed=%r" % doc["user"]["name"])
        check("ascii phrase matched at user.id",
              doc["user"]["id"] == "[MASKED]-123",
              "observed=%r" % doc["user"]["id"])
        check("mixed raw/escaped phrase matched at s",
              doc["s"] == "A\n[MASKED]",
              "observed=%r" % doc["s"])
        check("unselected field note left decoded-identical",
              doc["note"] == "keep " + SECRET_WORD + " here")
        check("unselected escape-form field decoded-identical",
              doc["esc"] == "éclair")

    check("unhit escape \\u0041 preserved byte-exact", b"\\u0041" in out)
    check("unhit escape \\n preserved byte-exact", b"\\n" in out)
    check("unselected escape \\u00e9 preserved byte-exact", b"\\u00e9" in out)
    check("number spelling 1.50e3 preserved byte-exact", b"1.50e3" in out)
    check("whitespace layout preserved",
          out.startswith(b'{ "user" : { "name" : '))
    check("unselected phrase bytes still present verbatim",
          SECRET_WORD.encode("utf-8") in out)
    check("exactly three replacements emitted",
          out.count(b"[MASKED]") == 3,
          "expected=3 observed=%d" % out.count(b"[MASKED]"))


# ---------------------------------------------------------------- C02
def case_c02(check):
    """Longest-match, adjacent/overlapping phrases, replacement encoding."""
    # Longest match at the same start position wins.
    r = StreamRedactor(["v"], ["ab", "abc", "bc"], "X")
    out = feed_all(r, b'{"v":"abc"}\n{"v":"abcd"}\n{"v":"xbc"}\n')
    docs = decode_lines(out)
    check("longest phrase wins at same start (abc -> X)",
          len(docs) == 3 and docs[0]["v"] == "X",
          "observed=%r" % (docs[0]["v"] if docs else None))
    check("scan resumes after match end (abcd -> Xd)",
          len(docs) == 3 and docs[1]["v"] == "Xd",
          "observed=%r" % (docs[1]["v"] if len(docs) > 1 else None))
    check("later-start shorter phrase still matches (xbc -> xX)",
          len(docs) == 3 and docs[2]["v"] == "xX",
          "observed=%r" % (docs[2]["v"] if len(docs) > 2 else None))

    # Adjacent matches both fire; overlapping phrase is not re-scanned.
    r = StreamRedactor(["v"], ["ab", "cd"], "X")
    docs = decode_lines(feed_all(r, b'{"v":"abcd"}\n'))
    check("adjacent phrases both replaced (abcd -> XX)",
          len(docs) == 1 and docs[0]["v"] == "XX",
          "observed=%r" % (docs[0]["v"] if docs else None))

    r = StreamRedactor(["v"], ["ab", "bc"], "X")
    docs = decode_lines(feed_all(r, b'{"v":"abc"}\n'))
    check("overlapping phrase not re-scanned (abc -> Xc)",
          len(docs) == 1 and docs[0]["v"] == "Xc",
          "observed=%r" % (docs[0]["v"] if docs else None))

    # Replacement text is re-encoded as a legal JSON string body.
    replacement = '"><\\\n' + "日"
    r = StreamRedactor(["v"], ["ab"], replacement)
    out = feed_all(r, b'{"v":"zab"}\n')
    docs = decode_lines(out)
    check("replacement decodes back exactly",
          len(docs) == 1 and docs[0]["v"] == "z" + replacement,
          "decoded length expected=%d observed=%s"
          % (1 + len(replacement),
             len(docs[0]["v"]) if docs else "n/a"))
    check("quote in replacement emitted as \\\"",
          b'\\"' in out)
    check("backslash in replacement emitted as \\\\",
          b"\\\\" in out)
    check("newline in replacement emitted as \\n escape",
          b"\\n" in out)
    check("non-ascii replacement emitted as UTF-8",
          "日".encode("utf-8") in out)


# ---------------------------------------------------------------- C03
def case_c03(check):
    """Result is independent of chunk boundaries (UTF-8/escapes/surrogates)."""
    seed = 20261117
    phrase = SECRET_WORD + FIRE
    record = (
        '{"a":"x' + phrase + 'y",'
        '"b":"\\u673a\\u5bc6\\ud83d\\udd25z",'
        '"c":[1,"' + phrase + '"],'
        '"d":"' + FIRE + SECRET_WORD + '"}'
    ).encode("utf-8") + b"\n"
    paths = ["a", "b", "c[]", "d"]

    reference = feed_all(StreamRedactor(paths, [phrase], "[R]"), record)
    ref_doc = json.loads(reference)
    check("reference: raw UTF-8 phrase redacted",
          ref_doc["a"] == "x[R]y", "observed=%r" % ref_doc["a"])
    check("reference: fully \\\\u-escaped phrase (surrogate pair) redacted",
          ref_doc["b"] == "[R]z", "observed=%r" % ref_doc["b"])
    check("reference: array element path redacted",
          ref_doc["c"] == [1, "[R]"], "observed=%r" % ref_doc["c"])
    check("reference: reversed order is not a match",
          ref_doc["d"] == FIRE + SECRET_WORD)

    chunkings = [("fixed-%d" % size, size) for size in (1, 2, 3, 4, 5, 7, 16, 1024)]
    rng = random.Random(seed)
    for k in range(4):
        cuts = sorted(rng.sample(range(1, len(record)), k=min(9, len(record) - 1)))
        sizes = [b - a for a, b in zip([0] + cuts, cuts + [len(record)])]
        chunkings.append(("seeded-%d-%d" % (seed, k), sizes))

    identical = 0
    for name, plan in chunkings:
        redactor = StreamRedactor(paths, [phrase], "[R]")
        out = bytearray()
        if isinstance(plan, int):
            for i in range(0, len(record), plan):
                out += redactor.feed(record[i : i + plan])
        else:
            pos = 0
            for size in plan:
                out += redactor.feed(record[pos : pos + size])
                pos += size
        out += redactor.finish()
        ok = bytes(out) == reference and not redactor.errors
        identical += ok
        check("chunking %s yields identical output" % name, ok,
              "chunks=%d identical=%s" % (
                  (len(record) + plan - 1) // plan if isinstance(plan, int)
                  else len(plan),
                  ok))
    check("all %d chunkings identical to whole-record feed" % len(chunkings),
          identical == len(chunkings),
          "expected=%d observed=%d" % (len(chunkings), identical))


# ---------------------------------------------------------------- C04
def case_c04(check):
    """Malformed/budget failures are atomic; later records still process."""
    paths = ["v"]
    phrases = [SECRET_WORD]
    redactor = StreamRedactor(paths, phrases, "[M]", max_record_bytes=48)
    good1 = '{"v":"%s"}' % SECRET_WORD
    bad = '{"v": "unterminated'
    good2 = '{"v":"ok %s"}' % SECRET_WORD
    big = '{"v":"%s"}' % ("x" * 100)
    good3 = '{"v":"tail"}'
    tail = '{"v": "incomplete'

    out = bytearray()
    for line in (good1, bad, good2, big, good3):
        out += redactor.feed(line.encode("utf-8") + b"\n")
    out += redactor.feed(tail.encode("utf-8"))  # no newline: flushed by finish()
    out += redactor.finish()
    out = bytes(out)

    errors = redactor.errors
    check("three records failed", len(errors) == 3,
          "expected=3 observed=%d" % len(errors))
    check("failure reasons are malformed/budget/malformed",
          [e.reason for e in errors] == [ERR_MALFORMED, ERR_BUDGET, ERR_MALFORMED],
          "observed=%s" % [e.reason for e in errors])
    check("failed record indices are 1, 3, 5",
          [e.record_index for e in errors] == [1, 3, 5],
          "observed=%s" % [e.record_index for e in errors])

    lines = [l for l in out.split(b"\n") if l.strip()]
    check("exactly the three good records were emitted", len(lines) == 3,
          "expected=3 observed=%d" % len(lines))
    docs = decode_lines(out)
    check("record after malformed record still redacted",
          len(docs) == 3 and docs[1]["v"] == "ok [M]",
          "observed=%r" % (docs[1]["v"] if len(docs) > 1 else None))
    check("record after budget-exceeded record still processed",
          len(docs) == 3 and docs[2]["v"] == "tail",
          "observed=%r" % (docs[2]["v"] if len(docs) > 2 else None))
    check("no sensitive phrase bytes anywhere in output",
          SECRET_WORD.encode("utf-8") not in out)
    check("no partial bytes from failed records in output",
          b"unterminated" not in out and b"incomplete" not in out
          and b"x" * 100 not in out)
    check("error objects carry no sensitive value",
          SECRET_WORD not in repr(errors) and SECRET_WORD not in str(errors))
    check("error objects carry no record content at all",
          all(set(repr(e)) <= set("RecordError(record_index=, reason='')"
                                  "abcdefghijklmnopqrstuvwxyz_0123456789")
              for e in errors))


CASES = {
    "C01": ("scalar matching at selected paths; byte fidelity of untouched "
            "content", case_c01),
    "C02": ("longest-match, adjacent/overlapping phrases, replacement JSON "
            "encoding", case_c02),
    "C03": ("chunk-boundary invariance for UTF-8, escapes and surrogate "
            "pairs", case_c03),
    "C04": ("atomic record failure on malformed input / budget exhaustion; "
            "stream recovery", case_c04),
}


def run_case(case_id, fn):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    fn(check)
    passed = sum(1 for c in checks if c["ok"])
    failed = len(checks) - passed
    record = {
        "case": case_id,
        "assertions": len(checks),
        "passed": passed,
        "failed": failed,
        "skipped": 0,
        "checks": checks,
    }
    print(json.dumps(record, ensure_ascii=False))
    if not checks or failed:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="list available cases as JSON and exit")
    parser.add_argument("--case", metavar="ID",
                        help="run one case (C01/C02/C03/C04)")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            [{"case": cid, "description": desc} for cid, (desc, _) in CASES.items()],
            ensure_ascii=False, indent=2))
        return 0
    if not args.case:
        parser.error("either --list or --case is required")
    entry = CASES.get(args.case)
    if entry is None:
        print(json.dumps({"error": "unknown case", "case": args.case,
                          "known": sorted(CASES)}))
        return 2
    return run_case(args.case, entry[1])


if __name__ == "__main__":
    sys.exit(main())
