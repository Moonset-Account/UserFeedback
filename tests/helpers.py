"""Shared helpers for the property tests.

On a property violation the helpers write the offending (transformed)
input to ``repro/<case_id>.csv`` and raise an AssertionError that names
the source sample, the applied transforms, the fixed random seed and a
command that re-runs the failing input locally.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from importer import ContactImporter  # noqa: E402

SEED = 20261114
SAMPLES_DIR = os.path.join(ROOT, "samples")
REPRO_DIR = os.path.join(ROOT, "repro")

ALL_SAMPLES = [
    "valid_basic",
    "valid_quoted",
    "valid_minimal",
    "valid_header_only",
    "valid_no_trailing_newline",
    "empty",
    "mixed_valid_invalid",
    "invalid_missing_required",
    "invalid_duplicate_column",
    "invalid_unknown_column",
    "invalid_field_count",
    "invalid_quote_syntax",
    "invalid_unterminated",
    "invalid_empty_name",
    "invalid_email",
    "invalid_multiline_record_numbering",
]

# Samples that parse into clean rows and can therefore be round-tripped
# through parse_rows()/serialize_rows() (column order + quoting transforms).
ROUNDTRIP_SAMPLES = [
    "valid_basic",
    "valid_quoted",
    "valid_minimal",
    "valid_header_only",
    "valid_no_trailing_newline",
    "mixed_valid_invalid",
]


def sample_text(case_id):
    path = os.path.join(SAMPLES_DIR, case_id + ".csv")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def signature_of(result):
    """Comparison-relevant projection of an import result."""
    return {
        "contacts": [dict(c) for c in result["contacts"]],
        "errors": [
            {"record": e["record"], "category": e["category"]}
            for e in result["errors"]
        ],
    }


def parse_text(text, chunks=None):
    """Parse ``text``; if ``chunks`` (a list of strings) is given, feed them
    one by one instead of the whole text at once."""
    imp = ContactImporter()
    if chunks is None:
        imp.feed(text)
    else:
        for chunk in chunks:
            imp.feed(chunk)
    imp.close()
    return signature_of(imp.result())


def split_into_sizes(text, sizes):
    chunks = []
    i = 0
    for size in sizes:
        chunks.append(text[i:i + size])
        i += size
    chunks.append(text[i:])
    return chunks


def assert_signature_equal(case_id, sample_id, transform_desc, text,
                           expected, actual):
    """Compare two signatures; on mismatch persist a re-runnable repro."""
    if expected == actual:
        return
    os.makedirs(REPRO_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id)
    path = os.path.join(REPRO_DIR, safe + ".csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    raise AssertionError(
        "property violation\n"
        "  case_id:        %s\n"
        "  random seed:    %d\n"
        "  source sample:  samples/%s.csv\n"
        "  transforms:     %s\n"
        "  repro input:    %s\n"
        "  rerun locally:  /usr/local/bin/python3 app.py %s\n"
        "  expected: %r\n"
        "  actual:   %r"
        % (case_id, SEED, sample_id, transform_desc, path, path,
           expected, actual)
    )
