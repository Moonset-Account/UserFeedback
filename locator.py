"""Cross-build memory diagnostic symbol localization and grouping.

Reads local diagnostic records (build id, module load ranges, error
category, call-stack addresses) together with offline symbol tables
(per build, per module, relative address ranges -> function/source).

Resolution rules:
  * An absolute stack address must fall inside exactly one loaded
    module range.  Ranges are half-open: [base, end) -- the right
    endpoint is NOT included.
  * The relative address (absolute - module base) must fall inside
    exactly one symbol range, also half-open: [rel_start, rel_end).
  * If no module/symbol matches, or more than one matches (overlap),
    the raw frame is kept together with the reason.

Grouping rules:
  * Records are grouped by (error category, function, source) of the
    first confirmable application frame (top of stack first).
  * Records whose source cannot be confirmed (build mismatch, no
    application frame, etc.) are listed separately with their reason.
"""

from __future__ import annotations

# --- reason codes (stable, used in reports and tests) -----------------------

REASON_BUILD_MISMATCH = "build_mismatch"
REASON_OUTSIDE_MODULES = "address_outside_loaded_ranges"
REASON_MODULE_OVERLAP = "overlapping_module_ranges"
REASON_NO_SYMBOL = "no_symbol_for_relative_address"
REASON_SYMBOL_OVERLAP = "overlapping_symbol_ranges"
REASON_NO_APP_FRAME = "no_confirmable_app_frame"


# --- address helpers ---------------------------------------------------------


def parse_address(value):
    """Accept an int or a decimal/hex string and return an int."""
    if isinstance(value, bool):
        raise ValueError("address must be int or str, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text, 10)
    raise ValueError("unsupported address value: %r" % (value,))


def format_address(value):
    """Canonical hex rendering used in reports."""
    return "0x%x" % value


# --- data model --------------------------------------------------------------


class ModuleRange:
    """A loaded module range [base, end); the right endpoint is excluded."""

    def __init__(self, name, base, end):
        if end <= base:
            raise ValueError("module %r: end must be greater than base" % name)
        self.name = name
        self.base = base
        self.end = end  # exclusive

    def contains(self, addr):
        return self.base <= addr < self.end

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["name"],
            parse_address(data["base"]),
            parse_address(data["end"]),
        )


class SymbolRange:
    """A symbol range [rel_start, rel_end) relative to its module base."""

    def __init__(self, rel_start, rel_end, function, source, is_app):
        if rel_end <= rel_start:
            raise ValueError(
                "symbol %r: rel_end must be greater than rel_start" % function
            )
        self.rel_start = rel_start
        self.rel_end = rel_end  # exclusive
        self.function = function
        self.source = source
        self.is_app = bool(is_app)

    def contains(self, rel):
        return self.rel_start <= rel < self.rel_end

    @classmethod
    def from_dict(cls, data):
        return cls(
            parse_address(data["rel_start"]),
            parse_address(data["rel_end"]),
            data["function"],
            data["source"],
            data.get("is_app", False),
        )


# --- frame / record resolution ----------------------------------------------


def resolve_frame(addr, modules, symbols_by_module):
    """Resolve one absolute address against loaded modules and symbols.

    Returns a frame dict.  Unresolved frames keep the raw address plus a
    stable reason code (and a human-readable detail when useful).
    """
    hits = [m for m in modules if m.contains(addr)]
    if not hits:
        return {
            "address": format_address(addr),
            "resolved": False,
            "reason": REASON_OUTSIDE_MODULES,
            "detail": "address is not inside any loaded module range",
        }
    if len(hits) > 1:
        return {
            "address": format_address(addr),
            "resolved": False,
            "reason": REASON_MODULE_OVERLAP,
            "detail": "matched modules: " + ", ".join(m.name for m in hits),
        }
    module = hits[0]
    rel = addr - module.base
    syms = [s for s in symbols_by_module.get(module.name, []) if s.contains(rel)]
    if not syms:
        return {
            "address": format_address(addr),
            "resolved": False,
            "reason": REASON_NO_SYMBOL,
            "module": module.name,
            "relative_address": format_address(rel),
            "detail": "no symbol range covers the relative address",
        }
    if len(syms) > 1:
        return {
            "address": format_address(addr),
            "resolved": False,
            "reason": REASON_SYMBOL_OVERLAP,
            "module": module.name,
            "relative_address": format_address(rel),
            "detail": "matched symbols: " + ", ".join(s.function for s in syms),
        }
    sym = syms[0]
    return {
        "address": format_address(addr),
        "resolved": True,
        "module": module.name,
        "relative_address": format_address(rel),
        "function": sym.function,
        "source": sym.source,
        "is_app": sym.is_app,
    }


def process_record(record, symbol_tables):
    """Resolve one diagnostic record.

    Returns a result dict with status "resolved" (plus a grouping key) or
    "unresolved" (plus a reason).  Raw frames are always preserved.
    """
    build = record["build"]
    diag_id = record.get("id", "<unknown>")
    category = record["category"]
    modules = [ModuleRange.from_dict(m) for m in record.get("modules", [])]
    stack = [parse_address(a) for a in record.get("stack", [])]

    table = symbol_tables.get(build)
    if table is None:
        frames = [
            {
                "address": format_address(a),
                "resolved": False,
                "reason": REASON_BUILD_MISMATCH,
                "detail": "no symbol table for build %r" % build,
            }
            for a in stack
        ]
        return {
            "diagnostic_id": diag_id,
            "build": build,
            "category": category,
            "status": "unresolved",
            "reason": REASON_BUILD_MISMATCH,
            "detail": "diagnostic references build %r which has no symbol table"
            % build,
            "frames": frames,
        }

    frames = [resolve_frame(a, modules, table) for a in stack]
    app_frame = next(
        (f for f in frames if f.get("resolved") and f.get("is_app")), None
    )
    if app_frame is None:
        return {
            "diagnostic_id": diag_id,
            "build": build,
            "category": category,
            "status": "unresolved",
            "reason": REASON_NO_APP_FRAME,
            "detail": "no frame resolved to application code",
            "frames": frames,
        }

    return {
        "diagnostic_id": diag_id,
        "build": build,
        "category": category,
        "status": "resolved",
        "key": {
            "category": category,
            "function": app_frame["function"],
            "source": app_frame["source"],
        },
        "key_frame": app_frame,
        "frames": frames,
    }


# --- grouping / report -------------------------------------------------------


def load_symbol_tables(payload):
    """Parse the symbol_tables section of an input payload."""
    tables = {}
    for build, data in payload.get("symbol_tables", {}).items():
        modules = {}
        for mod_name, syms in data.get("modules", {}).items():
            modules[mod_name] = [SymbolRange.from_dict(s) for s in syms]
        tables[build] = modules
    return tables


def build_report(payload):
    """Build the full grouping report for an input payload."""
    tables = load_symbol_tables(payload)
    groups = {}
    unresolved = []

    for record in payload.get("diagnostics", []):
        result = process_record(record, tables)
        if result["status"] == "resolved":
            key = result["key"]
            group_key = (key["category"], key["function"], key["source"])
            group = groups.setdefault(
                group_key,
                {
                    "category": key["category"],
                    "function": key["function"],
                    "source": key["source"],
                    "members": [],
                },
            )
            group["members"].append(
                {
                    "diagnostic_id": result["diagnostic_id"],
                    "build": result["build"],
                    "key_frame_address": result["key_frame"]["address"],
                    "module": result["key_frame"]["module"],
                    "relative_address": result["key_frame"]["relative_address"],
                }
            )
        else:
            unresolved.append(
                {
                    "diagnostic_id": result["diagnostic_id"],
                    "build": result["build"],
                    "category": result["category"],
                    "reason": result["reason"],
                    "detail": result["detail"],
                    "frames": result["frames"],
                }
            )

    group_list = []
    for index, key in enumerate(sorted(groups), start=1):
        group = groups[key]
        group_list.append(
            {
                "group_id": "grp-%d" % index,
                "category": group["category"],
                "function": group["function"],
                "source": group["source"],
                "members": group["members"],
            }
        )

    return {"groups": group_list, "unresolved": unresolved}


def render_text(report):
    """Human-readable rendering of a report produced by build_report."""
    lines = []
    lines.append("=== grouped diagnostics ===")
    if not report["groups"]:
        lines.append("(no confirmed groups)")
    for group in report["groups"]:
        lines.append(
            "%s  [%s] %s  (%s)"
            % (group["group_id"], group["category"], group["function"], group["source"])
        )
        for member in group["members"]:
            lines.append(
                "    - %s  build=%s  %s!%s  abs=%s"
                % (
                    member["diagnostic_id"],
                    member["build"],
                    member["module"],
                    member["relative_address"],
                    member["key_frame_address"],
                )
            )
    lines.append("")
    lines.append("=== unresolved (source not confirmable) ===")
    if not report["unresolved"]:
        lines.append("(none)")
    for item in report["unresolved"]:
        lines.append(
            "%s  build=%s  [%s]  reason=%s"
            % (item["diagnostic_id"], item["build"], item["category"], item["reason"])
        )
        lines.append("    detail: %s" % item["detail"])
        for frame in item["frames"]:
            if frame.get("resolved"):
                lines.append(
                    "    frame %s  resolved  %s (%s)"
                    % (frame["address"], frame["function"], frame["source"])
                )
            else:
                lines.append(
                    "    frame %s  RAW  reason=%s"
                    % (frame["address"], frame["reason"])
                )
    lines.append("")
    return "\n".join(lines)
