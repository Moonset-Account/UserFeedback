"""Receive-side quarantine object checker for SHA-1 git repositories.

The hosting receive end calls this module *before* updating any reference,
while pushed objects still live in a quarantine area.  The caller supplies:

* candidate branch heads (ref name -> 40-hex object id),
* a trusted boundary of old object ids (already verified in a previous run),
* a read adapter mapping an object id to its *compressed* loose-object bytes.

The checker inflates each new reachable object exactly once (objects shared
between branches are verified and budgeted a single time), validates the
loose-object header (type and length), the SHA-1 content digest, and the
types of commit/tree/tag references.  Tree entries are checked for duplicate
raw names and path separators; regular-file and symlink entries must point
at blobs, directory entries at trees.  Gitlink entries keep the external
repository's commit id and are never expanded or read.

Trusted boundary objects stop expansion and are never read.  An object that
is neither registered as trusted nor readable blocks every branch that can
reach it.  Three independent budgets are enforced: single-object inflated
size, cumulative inflated bytes, and number of new objects; shared objects
are counted once.  The result is per-branch: acceptable or not, plus the
shortest reference chain to the first error.  No worktree is created and
object content is never executed.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = ["BranchVerdict", "Report", "QuarantineChecker", "check_quarantine"]

VALID_TYPES = frozenset({"blob", "tree", "commit", "tag"})
_OID_RE = re.compile(r"[0-9a-f]{40}\Z")

_FILE_MODES = frozenset({0o100644, 0o100755})
_LINK_MODE = 0o120000
_DIR_MODE = 0o040000
_GITLINK_MODE = 0o160000


@dataclass
class BranchVerdict:
    """Per-branch outcome.  ``chain`` is None when ``ok`` is True."""

    name: str
    ok: bool
    chain: Optional[List[str]] = None


@dataclass
class Report:
    """Aggregate result of one quarantine check run."""

    branches: Dict[str, BranchVerdict]
    objects_verified: int
    objects_trusted: int
    objects_rejected: int
    bytes_expanded: int
    halted: bool

    @property
    def ok(self) -> bool:
        return all(b.ok for b in self.branches.values())


@dataclass
class _Object:
    oid: str
    state: str  # "ok" | "error" | "trusted"
    otype: Optional[str] = None
    size: int = 0
    error: Optional[str] = None
    missing: bool = False
    children: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    gitlinks: List[str] = field(default_factory=list)


class QuarantineChecker:
    """Verifies quarantined objects reachable from candidate branch heads."""

    def __init__(
        self,
        read_object: Callable[[str], Optional[bytes]],
        *,
        max_object_bytes: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        max_objects: Optional[int] = None,
    ) -> None:
        if not callable(read_object):
            raise TypeError("read_object must be callable")
        self._read = read_object
        self.max_object_bytes = max_object_bytes
        self.max_total_bytes = max_total_bytes
        self.max_objects = max_objects
        self._objects: Dict[str, _Object] = {}
        self._trusted: set = set()
        self._read_count = 0
        self._total_bytes = 0
        self._halted = False

    # ------------------------------------------------------------------ API

    def check(self, heads: Mapping[str, str], trusted: Iterable[str] = ()) -> Report:
        trusted_set = set()
        for oid in trusted:
            norm = oid.lower()
            if not _OID_RE.match(norm):
                raise ValueError(f"trusted boundary id is not a SHA-1 hex id: {oid!r}")
            trusted_set.add(norm)
        self._trusted = trusted_set

        branch_heads: Dict[str, str] = {}
        queue: deque = deque()
        for name, oid in heads.items():
            norm = oid.lower()
            branch_heads[name] = norm
            if _OID_RE.match(norm):
                queue.append(norm)

        while queue and not self._halted:
            oid = queue.popleft()
            if oid in self._objects:
                continue
            if oid in self._trusted:
                self._objects[oid] = _Object(oid, "trusted")
                continue
            rec = self._expand(oid)
            if rec.state == "ok":
                for child, _expected in rec.children:
                    if child not in self._objects:
                        queue.append(child)

        self._check_edge_types()
        return self._report(branch_heads)

    # ------------------------------------------------------------- expansion

    def _expand(self, oid: str) -> _Object:
        if self.max_objects is not None and self._read_count + 1 > self.max_objects:
            rec = _Object(oid, "error",
                          error=f"object-count budget exceeded (limit {self.max_objects})")
            self._objects[oid] = rec
            self._halted = True
            return rec

        try:
            raw = self._read(oid)
        except KeyError:
            raw = None
        if raw is None:
            rec = _Object(oid, "error", missing=True,
                          error="object is not registered as trusted and cannot be read")
            self._objects[oid] = rec
            return rec
        self._read_count += 1

        try:
            data = zlib.decompress(bytes(raw))
        except Exception:
            rec = _Object(oid, "error", error="loose object does not inflate as zlib")
            self._objects[oid] = rec
            return rec

        self._total_bytes += len(data)
        if self.max_total_bytes is not None and self._total_bytes > self.max_total_bytes:
            rec = _Object(oid, "error",
                          error=f"cumulative expanded-byte budget exceeded "
                                f"(limit {self.max_total_bytes})")
            self._objects[oid] = rec
            self._halted = True
            return rec
        if self.max_object_bytes is not None and len(data) > self.max_object_bytes:
            rec = _Object(oid, "error",
                          error=f"single-object expanded size {len(data)} exceeds "
                                f"budget {self.max_object_bytes}")
            self._objects[oid] = rec
            return rec

        nul = data.find(b"\0")
        if nul < 0:
            rec = _Object(oid, "error", error="loose header has no NUL terminator")
            self._objects[oid] = rec
            return rec
        header, payload = data[:nul], data[nul + 1:]
        parts = header.split(b" ")
        if len(parts) != 2:
            rec = _Object(oid, "error", error=f"malformed loose header {header!r}")
            self._objects[oid] = rec
            return rec
        try:
            otype = parts[0].decode("ascii")
        except UnicodeDecodeError:
            otype = ""
        if otype not in VALID_TYPES:
            rec = _Object(oid, "error", error=f"unknown object type {parts[0]!r}")
            self._objects[oid] = rec
            return rec
        if not parts[1].isdigit():
            rec = _Object(oid, "error", error=f"non-numeric header length {parts[1]!r}")
            self._objects[oid] = rec
            return rec
        declared = int(parts[1])
        if declared != len(payload):
            rec = _Object(oid, "error", otype=otype,
                          error=f"header length {declared} != payload length {len(payload)}")
            self._objects[oid] = rec
            return rec

        digest = hashlib.sha1(data).hexdigest()
        if digest != oid:
            rec = _Object(oid, "error", otype=otype,
                          error=f"sha1 mismatch: inflated content hashes to {digest}")
            self._objects[oid] = rec
            return rec

        children, gitlinks, err = self._parse(otype, payload)
        if err is not None:
            rec = _Object(oid, "error", otype=otype, size=len(data), error=err)
        else:
            rec = _Object(oid, "ok", otype=otype, size=len(data),
                          children=children, gitlinks=gitlinks)
        self._objects[oid] = rec
        return rec

    # --------------------------------------------------------------- parsing

    def _parse(self, otype: str, payload: bytes):
        if otype == "blob":
            return [], [], None
        if otype == "commit":
            return self._parse_commit(payload)
        if otype == "tree":
            return self._parse_tree(payload)
        return self._parse_tag(payload)

    @staticmethod
    def _hex_token(raw: bytes) -> Optional[str]:
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        return text if _OID_RE.match(text) else None

    def _parse_commit(self, payload: bytes):
        head = payload.split(b"\n\n", 1)[0]
        tree = None
        parents: List[str] = []
        for line in head.split(b"\n"):
            if line.startswith(b" "):  # continuation line (e.g. gpgsig)
                continue
            key, _, val = line.partition(b" ")
            if key == b"tree":
                if tree is not None:
                    return [], [], "commit has duplicate tree lines"
                tree = self._hex_token(val)
                if tree is None:
                    return [], [], "commit tree line is not a SHA-1 id"
            elif key == b"parent":
                parent = self._hex_token(val)
                if parent is None:
                    return [], [], "commit parent line is not a SHA-1 id"
                parents.append(parent)
        if tree is None:
            return [], [], "commit has no tree line"
        children = [(tree, "tree")] + [(p, "commit") for p in parents]
        return children, [], None

    def _parse_tree(self, payload: bytes):
        children: List[Tuple[str, Optional[str]]] = []
        gitlinks: List[str] = []
        seen = set()
        i, n = 0, len(payload)
        while i < n:
            sp = payload.find(b" ", i)
            if sp < 0:
                return [], [], "tree entry missing mode terminator"
            mode_b = payload[i:sp]
            if not mode_b or any(c not in b"01234567" for c in mode_b):
                return [], [], f"tree entry has bad mode {mode_b!r}"
            mode = int(mode_b, 8)
            nul = payload.find(b"\0", sp + 1)
            if nul < 0:
                return [], [], "tree entry name is not NUL terminated"
            name = payload[sp + 1:nul]
            if nul + 21 > n:
                return [], [], "tree entry id is truncated"
            oid = payload[nul + 1:nul + 21].hex()
            i = nul + 21
            if not name:
                return [], [], "tree entry has an empty file name"
            if b"/" in name:
                return [], [], f"tree entry name contains a path separator: {name!r}"
            if name in seen:
                return [], [], f"tree entry name is duplicated: {name!r}"
            seen.add(name)
            if mode in _FILE_MODES or mode == _LINK_MODE:
                children.append((oid, "blob"))
            elif mode == _DIR_MODE:
                children.append((oid, "tree"))
            elif mode == _GITLINK_MODE:
                # External repository commit id: kept as-is, never expanded.
                gitlinks.append(oid)
            else:
                return [], [], f"tree entry has unsupported mode {mode:o}"
        return children, gitlinks, None

    def _parse_tag(self, payload: bytes):
        head = payload.split(b"\n\n", 1)[0]
        target = None
        ttype = None
        for line in head.split(b"\n"):
            if line.startswith(b" "):
                continue
            key, _, val = line.partition(b" ")
            if key == b"object":
                target = self._hex_token(val)
                if target is None:
                    return [], [], "tag object line is not a SHA-1 id"
            elif key == b"type":
                try:
                    ttype = val.decode("ascii")
                except UnicodeDecodeError:
                    ttype = ""
        if target is None:
            return [], [], "tag has no object line"
        if ttype not in VALID_TYPES:
            return [], [], f"tag declares unknown target type {ttype!r}"
        return [(target, ttype)], [], None

    # --------------------------------------------------------- post-pass

    def _check_edge_types(self) -> None:
        for rec in self._objects.values():
            if rec.state != "ok":
                continue
            for child, expected in rec.children:
                if expected is None:
                    continue
                crec = self._objects.get(child)
                if crec is None or crec.state != "ok":
                    continue
                if crec.otype != expected:
                    rec.state = "error"
                    rec.error = (f"reference {child} must be {expected} "
                                 f"but is {crec.otype}")
                    break

    # --------------------------------------------------------------- report

    def _label(self, oid: str) -> str:
        rec = self._objects.get(oid)
        if rec is None:
            return f"object:{oid}"
        if rec.state == "trusted":
            return f"trusted:{oid}"
        if rec.missing:
            return f"missing:{oid}"
        return f"{rec.otype or 'object'}:{oid}"

    def _chain(self, head: str):
        """BFS for the shortest reference chain from head to any error."""
        seen = {head}
        queue: deque = deque([(head, [head])])
        while queue:
            oid, path = queue.popleft()
            rec = self._objects.get(oid)
            if rec is None:
                chain = [self._label(o) for o in path]
                chain.append("error: expansion halted before this object was verified")
                return False, chain
            if rec.state == "trusted":
                continue
            if rec.state == "error":
                chain = [self._label(o) for o in path]
                chain.append(f"error: {rec.error}")
                return False, chain
            for child, _expected in rec.children:
                if child not in seen:
                    seen.add(child)
                    queue.append((child, path + [child]))
        return True, None

    def _report(self, branch_heads: Mapping[str, str]) -> Report:
        branches: Dict[str, BranchVerdict] = {}
        for name, oid in branch_heads.items():
            if not _OID_RE.match(oid):
                branches[name] = BranchVerdict(
                    name, False, [f"error: invalid object id {oid!r}"])
                continue
            ok, chain = self._chain(oid)
            branches[name] = BranchVerdict(name, ok, chain)
        verified = sum(1 for r in self._objects.values() if r.state == "ok")
        trusted = sum(1 for r in self._objects.values() if r.state == "trusted")
        rejected = sum(1 for r in self._objects.values() if r.state == "error")
        return Report(
            branches=branches,
            objects_verified=verified,
            objects_trusted=trusted,
            objects_rejected=rejected,
            bytes_expanded=self._total_bytes,
            halted=self._halted,
        )


def check_quarantine(
    heads: Mapping[str, str],
    trusted: Iterable[str],
    read_object: Callable[[str], Optional[bytes]],
    *,
    max_object_bytes: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
    max_objects: Optional[int] = None,
) -> Report:
    """One-shot convenience wrapper around :class:`QuarantineChecker`."""
    checker = QuarantineChecker(
        read_object,
        max_object_bytes=max_object_bytes,
        max_total_bytes=max_total_bytes,
        max_objects=max_objects,
    )
    return checker.check(heads, trusted)
