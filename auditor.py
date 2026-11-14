"""Offline image-layer credential auditor (core library).

Streams ordered layer archives (tar, optionally gz/bz2/xz compressed) in a
single read-only pass, builds the final file view (plain overwrites, whiteout
deletion markers, opaque-directory markers) and records synthetic credential
marker matches. Reports keep only the matching rule id, layer index, path and
a redacted fingerprint -- never the matched content itself.

Semantics implemented:
  * normal overwrite: a later layer writing the same path replaces the
    earlier content in the final view;
  * deletion marker (``.wh.<name>``): removes the target path (and its
    children) from the view;
  * opaque directory marker (``.wh..wh..opq``): hides only *earlier* layer
    content under that directory; files added by the *same* layer stay
    visible regardless of their order inside the archive;
  * corrupted layer: scanning stops at the first unreadable layer, matches
    already proven from intact layers are kept, and the final visibility of
    anything not yet decided is reported as ``"unknown"``.
"""

import hashlib
import json
import lzma
import re
import tarfile
import zlib
from dataclasses import dataclass

WHITEOUT_PREFIX = ".wh."
OPAQUE_DIR_MARKER = ".wh..wh..opq"
CHUNK_SIZE = 64 * 1024

# Any of these while reading a layer archive marks that layer as corrupted.
LAYER_READ_ERRORS = (
    tarfile.TarError,
    EOFError,
    OSError,
    ValueError,
    zlib.error,
    lzma.LZMAError,
)


def normalize_member_path(name):
    """Normalize a tar member name to a relative POSIX path ('' means root)."""
    parts = []
    for seg in name.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@dataclass
class Rule:
    rule_id: str
    pattern: str

    def compiled(self):
        # Patterns are applied to raw member bytes; the textual pattern is
        # UTF-8 encoded first (synthetic markers are ASCII).
        return re.compile(self.pattern.encode("utf-8"))


def compile_rules(raw_rules):
    rules = []
    for i, item in enumerate(raw_rules):
        if not isinstance(item, dict) or "pattern" not in item:
            raise ValueError("rule #%d must be an object with a 'pattern' field" % i)
        pattern = str(item["pattern"])
        rule_id = str(item.get("id") or "rule-%d" % i)
        try:
            re.compile(pattern.encode("utf-8"))
        except re.error as exc:
            raise ValueError("rule %r has invalid pattern: %s" % (rule_id, exc)) from exc
        rules.append(Rule(rule_id, pattern))
    if not rules:
        raise ValueError("no rules defined")
    return rules


def load_rules(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    raw = doc.get("rules") if isinstance(doc, dict) else doc
    if not isinstance(raw, list):
        raise ValueError("rules file must be a JSON list or an object with a 'rules' list")
    return compile_rules(raw)


# --------------------------------------------------------------------------
# Matches
# --------------------------------------------------------------------------

def redacted_fingerprint(matched):
    """Redacted fingerprint: truncated SHA-256 of the matched bytes only."""
    return "sha256:" + hashlib.sha256(matched).hexdigest()[:16]


@dataclass
class Match:
    rule_id: str
    layer_index: int
    path: str
    fingerprint: str
    match_length: int
    visible_final: object = None  # True | False | "unknown"

    def to_dict(self):
        return {
            "rule": self.rule_id,
            "layer": self.layer_index,
            "path": self.path,
            "fingerprint": self.fingerprint,
            "match_length": self.match_length,
            "visible_final": self.visible_final,
        }


# --------------------------------------------------------------------------
# Final file view
# --------------------------------------------------------------------------

class LayerView:
    """Virtual final file system view built while streaming layers in order."""

    def __init__(self):
        self.entries = {}  # normalized path -> origin layer index
        self.complete = True

    def add(self, path, layer_index):
        if path:
            self.entries[path] = layer_index

    def remove(self, path):
        """Whiteout deletion: drop the path and everything beneath it."""
        if not path:
            return
        prefix = path + "/"
        doomed = [p for p in self.entries if p == path or p.startswith(prefix)]
        for p in doomed:
            del self.entries[p]

    def apply_opaque_dir(self, dir_path, layer_index):
        """Opaque directory: hide earlier-layer content under ``dir_path``.

        Entries added by the *same* layer (origin == layer_index) stay
        visible no matter where they appear inside the archive, so both
        archive orders (marker before/after the new files) behave the same.
        """
        prefix = dir_path + "/" if dir_path else ""
        doomed = [
            p for p, origin in self.entries.items()
            if origin < layer_index and p.startswith(prefix) and p != dir_path
        ]
        for p in doomed:
            del self.entries[p]


# --------------------------------------------------------------------------
# Layer scanning
# --------------------------------------------------------------------------

def _read_member_bytes(tf, member):
    fh = tf.extractfile(member)
    if fh is None:
        return b""
    chunks = []
    while True:
        chunk = fh.read(CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def scan_layer(layer_path, layer_index, rules, view):
    """Stream one layer archive read-only, update ``view``.

    Returns ``(matches, error)``; ``error`` is the exception that marked the
    layer as corrupted, or ``None`` when the layer was read completely.
    Matches found before a mid-archive failure are still returned (they were
    genuinely observed in the archive).
    """
    matches = []
    compiled = [(r.rule_id, r.compiled()) for r in rules]
    try:
        with tarfile.open(layer_path, "r|*") as tf:
            for member in tf:
                path = normalize_member_path(member.name)
                if not path:
                    continue
                base = path.rsplit("/", 1)[-1]
                parent = path[: len(path) - len(base) - 1] if "/" in path else ""
                if base == OPAQUE_DIR_MARKER:
                    view.apply_opaque_dir(parent, layer_index)
                    continue
                if base.startswith(WHITEOUT_PREFIX):
                    target = (parent + "/" if parent else "") + base[len(WHITEOUT_PREFIX):]
                    view.remove(target)
                    continue
                if member.isfile():
                    view.add(path, layer_index)
                    data = _read_member_bytes(tf, member)
                    for rule_id, rx in compiled:
                        for m in rx.finditer(data):
                            matched = m.group(0)
                            matches.append(Match(
                                rule_id=rule_id,
                                layer_index=layer_index,
                                path=path,
                                fingerprint=redacted_fingerprint(matched),
                                match_length=len(matched),
                            ))
                else:
                    # Directories, symlinks, etc. occupy the path in the view.
                    view.add(path, layer_index)
    except LAYER_READ_ERRORS as exc:
        return matches, exc
    return matches, None


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

@dataclass
class AuditResult:
    layers: list            # per-layer status dicts
    matches: list           # list[Match]
    view: LayerView
    view_complete: bool
    corrupted_layer: object  # index of first corrupted layer or None


def _final_visibility(match, view, corrupted_layer):
    origin = view.entries.get(match.path)
    if corrupted_layer is None:
        # Complete view: visible only if the surviving version of the path
        # originates from the very layer where the marker was found.
        return origin == match.layer_index
    # Incomplete view: only decisions made by intact earlier layers are proven.
    if match.layer_index >= corrupted_layer:
        return "unknown"
    if origin != match.layer_index:
        # Deleted or overwritten by an intact layer before the corruption:
        # proven not visible in the final view.
        return False
    return "unknown"


def audit(layer_paths, rules):
    """Audit ordered layer archives (oldest first) against ``rules``."""
    view = LayerView()
    matches = []
    layer_reports = []
    corrupted = None
    for i, lp in enumerate(layer_paths):
        if corrupted is not None:
            layer_reports.append({"index": i, "path": lp, "status": "skipped", "error": None})
            continue
        found, err = scan_layer(lp, i, rules, view)
        matches.extend(found)
        if err is None:
            layer_reports.append({"index": i, "path": lp, "status": "ok", "error": None})
        else:
            layer_reports.append({
                "index": i,
                "path": lp,
                "status": "corrupted",
                "error": "%s: %s" % (type(err).__name__, err),
            })
            corrupted = i
            view.complete = False
    for m in matches:
        m.visible_final = _final_visibility(m, view, corrupted)
    return AuditResult(
        layers=layer_reports,
        matches=matches,
        view=view,
        view_complete=corrupted is None,
        corrupted_layer=corrupted,
    )


def report_to_dict(result):
    """JSON-serializable report: rules, layer indexes, paths, fingerprints."""
    matches = result.matches
    summary = {
        "total_matches": len(matches),
        "visible": sum(1 for m in matches if m.visible_final is True),
        "hidden": sum(1 for m in matches if m.visible_final is False),
        "unknown": sum(1 for m in matches if m.visible_final == "unknown"),
        "layers_with_matches": sorted({m.layer_index for m in matches}),
    }
    return {
        "tool": "image-layer-credential-auditor",
        "view_complete": result.view_complete,
        "corrupted_layer": result.corrupted_layer,
        "layers": [dict(l) for l in result.layers],
        "matches": [m.to_dict() for m in matches],
        "summary": summary,
    }
