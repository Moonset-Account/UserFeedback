"""Core logic for the offline load-segment W^X page auditor (SEC-20261114-045).

Reads per-build load-segment mapping summaries (start/end addresses, RWX
permissions, page size, load order), page-aligns every segment, computes the
actual page coverage, and reports which address pages end up both writable
and executable.  Pages covered by more than one segment are resolved with the
merge rule explicitly declared in the input:

  - "union":       effective permissions are the union of all covering segments
  - "last_loaded": effective permissions come from the last loaded segment

Builds with unrepresentable addresses, invalid page sizes or missing mapping
records are flagged in the report instead of being silently skipped.
"""

from __future__ import annotations

MAX_ADDRESS = (1 << 64) - 1      # 64-bit address space
MAX_PAGE_SIZE = 1 << 64
MERGE_RULES = ("union", "last_loaded")
_PERM_ORDER = "rwx"


class AuditError(Exception):
    """A per-build validation failure that must be flagged in the report."""

    def __init__(self, error_type, detail):
        super().__init__("%s: %s" % (error_type, detail))
        self.error_type = error_type
        self.detail = detail


def parse_address(value, context):
    """Parse an address given as an int or a decimal/hex string.

    Raises AuditError("address_unrepresentable", ...) when the value cannot be
    represented in the 64-bit address space [0, 2^64 - 1].  Strings are parsed
    as decimal unless they carry hex digits or a 0x prefix.
    """
    if isinstance(value, bool):
        raise AuditError("address_unrepresentable",
                         "%s: %r is not an integer address" % (context, value))
    if isinstance(value, int):
        addr = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            addr = int(text, 10)
        except ValueError:
            try:
                addr = int(text, 16)
            except ValueError:
                raise AuditError(
                    "address_unrepresentable",
                    "%s: cannot parse %r as an address" % (context, value))
    else:
        raise AuditError("address_unrepresentable",
                         "%s: unsupported address value %r" % (context, value))
    if addr < 0 or addr > MAX_ADDRESS:
        raise AuditError(
            "address_unrepresentable",
            "%s: %d is outside the representable range [0, 0x%x]"
            % (context, addr, MAX_ADDRESS))
    return addr


def validate_page_size(value):
    """Validate the declared page size.

    Raises AuditError("invalid_page_size", ...) unless the value is an integer
    that is a positive power of two not exceeding 2^64.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditError("invalid_page_size",
                         "page_size must be an integer, got %r" % (value,))
    if value <= 0 or value > MAX_PAGE_SIZE or (value & (value - 1)) != 0:
        raise AuditError(
            "invalid_page_size",
            "page_size %r is not a positive power of two <= 2^64" % (value,))
    return value


def parse_perms(value, context):
    """Parse a permission string like 'rwx', 'r-x' or 'rw-' into a frozenset."""
    if not isinstance(value, str):
        raise AuditError("invalid_segment",
                         "%s: perms must be a string like 'rwx', got %r"
                         % (context, value))
    letters = set(value) - {"-"}
    if not letters or not letters.issubset(set(_PERM_ORDER)):
        raise AuditError("invalid_segment",
                         "%s: invalid perms %r (allowed letters: r, w, x)"
                         % (context, value))
    return frozenset(letters)


def format_perms(perms):
    """Render a permission set in canonical 'rwx' form, e.g. 'r-x'."""
    return "".join(c if c in perms else "-" for c in _PERM_ORDER)


def _parse_segment(raw, index):
    context = "segments[%d]" % index
    if not isinstance(raw, dict):
        raise AuditError("invalid_segment",
                         "%s: mapping record must be an object" % context)
    name = raw.get("name", "seg%d" % index)
    if not isinstance(name, str) or not name:
        raise AuditError("invalid_segment",
                         "%s: segment name must be a non-empty string" % context)
    if "start" not in raw or "end" not in raw:
        raise AuditError("invalid_segment",
                         "%s (%s): record needs 'start' and 'end'"
                         % (context, name))
    start = parse_address(raw["start"], "%s (%s) start" % (context, name))
    end = parse_address(raw["end"], "%s (%s) end" % (context, name))
    if start >= end:
        raise AuditError(
            "invalid_segment",
            "%s (%s): start 0x%x must be lower than end 0x%x"
            % (context, name, start, end))
    perms = parse_perms(raw.get("perms"), "%s (%s)" % (context, name))
    return {"name": name, "start": start, "end": end, "perms": perms,
            "load_order": index}


def audit_build(build):
    """Audit one build entry; never raises, flags problems in the report."""
    build_id = build.get("build_id") if isinstance(build, dict) else None
    if not isinstance(build_id, str) or not build_id:
        build_id = "<unknown>"
    try:
        return _audit_build(build, build_id)
    except AuditError as exc:
        return {
            "build_id": build_id,
            "status": "error",
            "error": {"type": exc.error_type, "detail": exc.detail},
        }


def _audit_build(build, build_id):
    if not isinstance(build, dict):
        raise AuditError("missing_mapping_records",
                         "build entry must be an object with segment records")
    page_size = validate_page_size(build.get("page_size"))
    merge_rule = build.get("merge_rule")
    if merge_rule not in MERGE_RULES:
        raise AuditError("invalid_merge_rule",
                         "merge_rule must be one of %s, got %r"
                         % (list(MERGE_RULES), merge_rule))
    raw_segments = build.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise AuditError("missing_mapping_records",
                         "build %s has no load-segment mapping records"
                         % build_id)
    # Load order is the order of records in the input array.
    segments = [_parse_segment(raw, i) for i, raw in enumerate(raw_segments)]

    # Page-align every segment and collect the segments covering each page,
    # kept in load order.
    pages = {}
    for seg in segments:
        first_page = seg["start"] // page_size
        last_page = (seg["end"] - 1) // page_size  # end is exclusive
        for page_index in range(first_page, last_page + 1):
            pages.setdefault(page_index, []).append(seg)

    affected = []
    for page_index in sorted(pages):
        covering = pages[page_index]
        if merge_rule == "union":
            effective = set()
            for seg in covering:
                effective |= seg["perms"]
        else:  # last_loaded: the last segment in load order wins
            effective = set(covering[-1]["perms"])
        if "w" in effective and "x" in effective:
            affected.append({
                "page_index": page_index,
                "start": "0x%x" % (page_index * page_size),
                "end": "0x%x" % ((page_index + 1) * page_size),
                "effective_perms": format_perms(effective),
                "contributing_segments": [seg["name"] for seg in covering],
                "applied_rule": merge_rule,
            })

    return {
        "build_id": build_id,
        "status": "ok",
        "page_size": page_size,
        "merge_rule": merge_rule,
        "segment_count": len(segments),
        "affected_page_count": len(affected),
        "affected_pages": affected,
    }


def audit_document(doc):
    """Audit a whole input document and return the full report."""
    if (not isinstance(doc, dict)
            or not isinstance(doc.get("builds"), list)
            or not doc["builds"]):
        return {
            "tool": "wx-page-auditor",
            "status": "error",
            "error": {
                "type": "missing_mapping_records",
                "detail": "input document must contain a non-empty 'builds' array",
            },
            "build_reports": [],
            "summary": {"total_builds": 0, "ok_builds": 0,
                        "error_builds": 0, "total_affected_pages": 0},
        }
    reports = [audit_build(b) for b in doc["builds"]]
    ok = [r for r in reports if r["status"] == "ok"]
    errors = [r for r in reports if r["status"] == "error"]
    return {
        "tool": "wx-page-auditor",
        "status": "ok",
        "build_reports": reports,
        "summary": {
            "total_builds": len(reports),
            "ok_builds": len(ok),
            "error_builds": len(errors),
            "total_affected_pages": sum(r["affected_page_count"] for r in ok),
        },
    }
