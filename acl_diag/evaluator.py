"""ACL evaluation engine.

Priority model (highest to lowest), evaluated separately for each right
(modify_file / replace_entry):

    1. explicit deny      (ACE attached directly to the object)
    2. explicit allow     (ACE attached directly to the object)
    3. inherited deny     (ACE inherited from an ancestor directory)
    4. inherited allow    (ACE inherited from an ancestor directory)

An explicit entry therefore always beats an inherited one, and within
the same origin a deny beats an allow.  This directly implements the
required rule that an explicit deny overrides an inherited allow.

Missing-record handling (never assume a default deny on absent data):

    * The target file/directory is absent from the snapshot, or present
      but marked exported=false -> "unverified" (待核实).
    * The parent directory is absent or its permissions were not
      exported -> replace_entry is "unverified".
    * The subject id exists in no account record -> every decision for
      it is "unverified" (the identity cannot be resolved).
    * The object exists and is exported but no collected ACE applies to
      the subject for the requested right -> "unverified": the export
      alone does not prove which access the subject should have.
"""

import posixpath
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .io_utils import load_json
from .membership import expand_members
from .models import (
    ACE,
    ACTIONS,
    ALLOW,
    DENY,
    MODIFY_FILE,
    REPLACE_ENTRY,
    UNVERIFIED,
    PermissionSnapshot,
    ServiceConfig,
    SubjectDirectory,
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_snapshot(path: str) -> PermissionSnapshot:
    raw = load_json(path)
    objects: Dict[str, dict] = {}
    for obj_path, info in raw.get("objects", {}).items():
        obj_path = str(obj_path)
        exported = bool(info.get("exported", True))
        obj_type = str(info.get("type", "file"))
        aces = [
            ACE.from_dict(ace, obj_path) for ace in info.get("aces", [])
        ] if exported else []
        # Inheritable ACEs attached to a directory: propagate to
        # descendants at evaluation time rather than mutating the store.
        inheritable = [
            ACE.from_dict(ace, obj_path)
            for ace in info.get("inheritable_aces", [])
        ] if exported else []
        objects[obj_path] = {
            "type": obj_type,
            "exported": exported,
            "aces": aces,
            "inheritable_aces": inheritable,
        }
    return PermissionSnapshot(objects=objects, version=str(raw.get("version", "")))


def load_inventory(path: str) -> List[ServiceConfig]:
    raw = load_json(path)
    return [ServiceConfig.from_dict(item) for item in raw.get("services", [])]


# ---------------------------------------------------------------------------
# Path helpers (synthetic POSIX paths only; no real filesystem access)
# ---------------------------------------------------------------------------
def ancestor_dirs(path: str) -> List[str]:
    """Return ancestor directory paths, nearest first, excluding the
    path itself."""

    chain = []
    parent = posixpath.dirname(path.rstrip("/"))
    while parent and parent != "/":
        chain.append(parent)
        parent = posixpath.dirname(parent)
    if parent == "/":
        chain.append("/")
    return chain


def is_exported(obj: Optional[dict]) -> bool:
    return obj is not None and obj.get("exported", True)


# ---------------------------------------------------------------------------
# ACE collection
# ---------------------------------------------------------------------------
def _matching_aces(
    aces: List[ACE], effective_principal_ids: frozenset, action: str
) -> List[ACE]:
    return [
        ace
        for ace in aces
        if ace.principal in effective_principal_ids and action in ace.rights
    ]


def collect_aces_for_object(
    target_path: str,
    snapshot: PermissionSnapshot,
    effective_principal_ids: frozenset,
    action: str,
) -> Tuple[List[ACE], List[ACE]]:
    """Collect (explicit, inherited) ACEs matching the subject/action.

    Explicit entries come from the target object itself.  Inherited
    entries are either recorded on the object with inherited=true, or
    propagated from inheritable ACEs attached to exported ancestor
    directories (nearest ancestor first).
    """

    obj = snapshot.get(target_path)
    explicit: List[ACE] = []
    inherited: List[ACE] = []
    if obj is None or not is_exported(obj):
        return explicit, inherited

    for ace in _matching_aces(obj["aces"], effective_principal_ids, action):
        if ace.inherited:
            # An entry recorded as inherited stays inherited even when it
            # is attached to the target object (materialized inheritance).
            inherited.append(ace)
        else:
            explicit.append(ace)

    if obj.get("type") == "directory":
        # A directory's inheritable entries also govern operations on the
        # directory itself (entry replacement for its children).  They are
        # explicit with respect to the directory target: the grant is
        # attached directly to that directory object.
        for ace in _matching_aces(
            obj.get("inheritable_aces", []), effective_principal_ids, action
        ):
            explicit.append(
                ACE(
                    ace_type=ace.ace_type,
                    principal=ace.principal,
                    rights=ace.rights,
                    inherited=False,
                    source_path=ace.source_path,
                )
            )

    # The object's own non-inherited ACEs are the full explicit set when
    # the object is the permission target (e.g. a directory's own write
    # ACE decides replace_entry for entries inside it).  Only entries on
    # *ancestors* are inherited; propagate those below.
    seen = {(a.ace_type, a.principal, frozenset(a.rights), a.source_path) for a in inherited}
    for ancestor in ancestor_dirs(target_path):
        anc_obj = snapshot.get(ancestor)
        if not is_exported(anc_obj):
            # Missing/unexported ancestor: its permissions are unknown.
            continue
        for ace in _matching_aces(
            anc_obj["inheritable_aces"], effective_principal_ids, action
        ):
            key = (ace.ace_type, ace.principal, frozenset(ace.rights), ace.source_path)
            if key not in seen:
                seen.add(key)
                # An inheritable entry attached to an ancestor becomes an
                # inherited entry on every descendant.
                inherited.append(
                    ACE(
                        ace_type=ace.ace_type,
                        principal=ace.principal,
                        rights=ace.rights,
                        inherited=True,
                        source_path=ace.source_path,
                    )
                )
    return explicit, inherited


def decide(explicit: List[ACE], inherited: List[ACE]) -> Tuple[str, Optional[ACE]]:
    """Apply the priority model and return (decision, deciding ACE)."""

    for ace in explicit:
        if ace.ace_type == DENY:
            return DENY, ace
    for ace in explicit:
        if ace.ace_type == ALLOW:
            return ALLOW, ace
    for ace in inherited:
        if ace.ace_type == DENY:
            return DENY, ace
    for ace in inherited:
        if ace.ace_type == ALLOW:
            return ALLOW, ace
    return UNVERIFIED, None


# ---------------------------------------------------------------------------
# Per-subject evaluation
# ---------------------------------------------------------------------------
@dataclass
class ActionResult:
    action: str
    decision: str
    target_object: str
    object_present: bool
    object_exported: bool
    deciding_ace: Optional[ACE]
    explicit_candidates: List[ACE] = field(default_factory=list)
    inherited_candidates: List[ACE] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "decision": self.decision,
            "target_object": self.target_object,
            "object_present": self.object_present,
            "object_exported": self.object_exported,
            "deciding_ace": self.deciding_ace.to_dict() if self.deciding_ace else None,
            "explicit_candidates": [a.to_dict() for a in self.explicit_candidates],
            "inherited_candidates": [a.to_dict() for a in self.inherited_candidates],
            "reasons": list(self.reasons),
        }


@dataclass
class Finding:
    service: str
    path: str
    subject_id: str
    subject_resolved: bool
    privileged: bool
    results: Dict[str, ActionResult]

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "path": self.path,
            "subject_id": self.subject_id,
            "subject_resolved": self.subject_resolved,
            "privileged": self.privileged,
            "results": {action: self.results[action].to_dict() for action in ACTIONS},
        }


def _evaluate_action(
    action: str,
    target_path: str,
    snapshot: PermissionSnapshot,
    effective_principal_ids: frozenset,
) -> ActionResult:
    target_obj = snapshot.get(target_path)
    present = target_obj is not None
    exported = is_exported(target_obj)
    reasons: List[str] = []

    if not present:
        reasons.append(
            "记录缺失：路径 %s 不在权限快照中，无法核实（待核实）" % target_path
        )
        return ActionResult(action, UNVERIFIED, target_path, False, False, None, reasons=reasons)
    if not exported:
        reasons.append(
            "记录不足：路径 %s 的权限未导出（exported=false），无法核实（待核实）" % target_path
        )
        return ActionResult(action, UNVERIFIED, target_path, True, False, None, reasons=reasons)

    explicit, inherited = collect_aces_for_object(
        target_path, snapshot, effective_principal_ids, action
    )
    decision, deciding = decide(explicit, inherited)
    if decision == UNVERIFIED:
        reasons.append(
            "记录不足：%s 上没有适用于该主体 %s 权限的有效 ACE，无法核实（待核实）"
            % (target_path, action)
        )
    else:
        origin = "显式" if (deciding and not deciding.inherited) else "继承"
        reasons.append(
            "%s%s ACE（principal=%s, source=%s）按优先级规则决定为 %s"
            % (origin, deciding.ace_type, deciding.principal,
               deciding.source_path, decision)
        )
    return ActionResult(
        action, decision, target_path, True, True, deciding,
        explicit, inherited, reasons,
    )


def evaluate_subject(
    service: str,
    path: str,
    subject_id: str,
    directory: SubjectDirectory,
    snapshot: PermissionSnapshot,
) -> Finding:
    """Evaluate both rights for one subject against one service config."""

    resolved = subject_id in directory.accounts
    privileged = resolved and _privileged(subject_id, directory)
    if not resolved:
        effective = frozenset({subject_id})
    else:
        effective = expand_members(subject_id, directory)

    results: Dict[str, ActionResult] = {}

    # Right 1: modify file contents -> write on the file itself.
    results[MODIFY_FILE] = _evaluate_action(MODIFY_FILE, path, snapshot, effective)
    if not resolved:
        results[MODIFY_FILE].reasons.insert(
            0, "记录缺失：主体 %s 在账号清单中不存在，身份无法解析（待核实）" % subject_id
        )
        results[MODIFY_FILE].decision = UNVERIFIED
        results[MODIFY_FILE].deciding_ace = None

    # Right 2: replace the directory entry -> write on the parent dir.
    parent = posixpath.dirname(path.rstrip("/")) or "/"
    parent_result = _evaluate_action(REPLACE_ENTRY, parent, snapshot, effective)
    parent_result.target_object = parent
    if not resolved:
        parent_result.reasons.insert(
            0, "记录缺失：主体 %s 在账号清单中不存在，身份无法解析（待核实）" % subject_id
        )
        parent_result.decision = UNVERIFIED
        parent_result.deciding_ace = None
    results[REPLACE_ENTRY] = parent_result

    return Finding(service, path, subject_id, resolved, privileged, results)


def _privileged(subject_id: str, directory: SubjectDirectory) -> bool:
    info = directory.accounts.get(subject_id)
    return bool(info and info.get("privileged"))


def subjects_for_service(
    config: ServiceConfig, directory: SubjectDirectory
) -> List[str]:
    """Subjects to evaluate: legacy subjects plus every non-privileged
    account in the directory (the 'ordinary accounts' of the audit)."""

    subjects = list(dict.fromkeys(
        item.subject_id for item in config.legacy
    ))
    for acc_id, info in directory.accounts.items():
        if not info.get("privileged") and acc_id not in subjects:
            subjects.append(acc_id)
    return subjects


def evaluate_inventory(
    inventory: List[ServiceConfig],
    directory: SubjectDirectory,
    snapshot: PermissionSnapshot,
) -> List[Finding]:
    findings: List[Finding] = []
    for config in inventory:
        for subject_id in subjects_for_service(config, directory):
            findings.append(
                evaluate_subject(
                    config.service, config.path, subject_id, directory, snapshot
                )
            )
    return findings
