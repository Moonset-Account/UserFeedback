"""Minimal permission remediation and re-verification.

Remediation rule (kept deliberately small and local):

* An ALLOW produced by an **explicit** allow ACE attached to the target
  object is fixed by removing that one ACE.  The entry itself is wrong
  on a service configuration path.
* An ALLOW produced by an **inherited** allow ACE is fixed by adding a
  single explicit deny ACE on the target object.  Per the priority
  model an explicit deny overrides an inherited allow, and ancestor
  directories (which may legitimately serve other paths) are untouched.

Only findings for resolved, non-privileged ("ordinary") accounts are
remediated; unverified rows cannot be fixed by changing ACEs and are
reported instead as evidence gaps.
"""

import copy
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

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
from .evaluator import evaluate_inventory


@dataclass
class RemediationAction:
    kind: str                      # "remove_ace" | "add_explicit_deny"
    object_path: str
    action: str
    target_subject: str
    ace: Optional[ACE]
    rationale: str

    def key(self) -> tuple:
        if self.kind == "remove_ace":
            return (
                self.kind, self.object_path, self.action,
                self.ace.ace_type, self.ace.principal, self.ace.source_path,
            )
        return (self.kind, self.object_path, self.action, self.target_subject)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "object_path": self.object_path,
            "action": self.action,
            "target_subject": self.target_subject,
            "ace": self.ace.to_dict() if self.ace else None,
            "rationale": self.rationale,
        }


@dataclass
class RemediationPlan:
    actions: List[RemediationAction] = field(default_factory=list)
    unresolved: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "unresolved": list(self.unresolved),
        }


def _is_directory_inheritable(
    snapshot: PermissionSnapshot, target_path: str, ace: ACE
) -> bool:
    obj = snapshot.get(target_path)
    if obj is None or obj.get("type") != "directory":
        return False
    return any(
        candidate.ace_type == ace.ace_type
        and candidate.principal == ace.principal
        and candidate.rights == ace.rights
        and candidate.source_path == ace.source_path
        for candidate in obj.get("inheritable_aces", [])
    )


def build_plan(
    inventory: List[ServiceConfig],
    directory: SubjectDirectory,
    snapshot: PermissionSnapshot,
    findings,
) -> RemediationPlan:
    plan = RemediationPlan()
    seen: Set[tuple] = set()

    for finding in findings:
        if not finding.subject_resolved or finding.privileged:
            continue
        for action in ACTIONS:
            result = finding.results[action]
            if result.decision == UNVERIFIED:
                plan.unresolved.append(
                    {
                        "service": finding.service,
                        "path": finding.path,
                        "subject_id": finding.subject_id,
                        "action": action,
                        "reason": (
                            "结论为待核实，缺少权限记录；整改前必须先补全 "
                            "%s 的权限导出，不能用新增 ACE 掩盖证据缺口"
                            % result.target_object
                        ),
                    }
                )
                continue
            if result.decision != ALLOW or result.deciding_ace is None:
                continue
            ace = result.deciding_ace
            target = result.target_object
            if ace.inherited:
                rec = RemediationAction(
                    kind="add_explicit_deny",
                    object_path=target,
                    action=action,
                    target_subject=finding.subject_id,
                    ace=ACE(
                        ace_type=DENY,
                        principal=finding.subject_id,
                        rights=frozenset((action,)),
                        inherited=False,
                        source_path=target,
                    ),
                    rationale=(
                        "允许来自 %s 的继承允许 ACE；在 %s 上为 %s 增加显式拒绝，"
                        "按优先级显式拒绝覆盖继承允许，且不改动祖先目录"
                        % (ace.source_path, target, finding.subject_id)
                    ),
                )
            elif action == REPLACE_ENTRY and _is_directory_inheritable(snapshot, target, ace):
                # The allow is the directory's own inheritable grant, which
                # may legitimately serve other children.  Narrow it with an
                # explicit deny on the directory instead of deleting it.
                rec = RemediationAction(
                    kind="add_explicit_deny",
                    object_path=target,
                    action=action,
                    target_subject=finding.subject_id,
                    ace=ACE(
                        ace_type=DENY,
                        principal=finding.subject_id,
                        rights=frozenset((action,)),
                        inherited=False,
                        source_path=target,
                    ),
                    rationale=(
                        "允许来自目录 %s 自身的可继承授权（principal=%s），该授权可能"
                        "服务于其他子项；改为在目录上为 %s 增加显式拒绝，避免扩大影响面"
                        % (target, ace.principal, finding.subject_id)
                    ),
                )
            else:
                rec = RemediationAction(
                    kind="remove_ace",
                    object_path=target,
                    action=action,
                    target_subject=finding.subject_id,
                    ace=ace,
                    rationale=(
                        "允许来自 %s 上的显式允许 ACE（principal=%s）；"
                        "直接移除该 ACE 是最小改动"
                        % (target, ace.principal)
                    ),
                )
            if rec.key() not in seen:
                seen.add(rec.key())
                plan.actions.append(rec)
    return plan


def apply_remediation(
    snapshot: PermissionSnapshot, plan: RemediationPlan
) -> PermissionSnapshot:
    """Return a new snapshot with the plan applied (the input is untouched)."""

    fixed = PermissionSnapshot(
        objects=copy.deepcopy(snapshot.objects), version=snapshot.version
    )
    for rec in plan.actions:
        obj = fixed.objects.get(rec.object_path)
        if obj is None or not obj.get("exported", True):
            continue
        if rec.kind == "remove_ace":
            obj["aces"] = [
                ace
                for ace in obj["aces"]
                if not (
                    ace.ace_type == rec.ace.ace_type
                    and ace.principal == rec.ace.principal
                    and ace.rights == rec.ace.rights
                    and ace.inherited == rec.ace.inherited
                    and ace.source_path == rec.ace.source_path
                )
            ]
        elif rec.kind == "add_explicit_deny":
            deny = rec.ace
            duplicate = any(
                ace.ace_type == DENY
                and ace.principal == deny.principal
                and action in ace.rights
                and not ace.inherited
                for ace in obj["aces"]
                for action in (rec.action,)
            )
            if not duplicate:
                obj["aces"].append(deny)
    return fixed


def snapshot_to_dict(snapshot: PermissionSnapshot) -> dict:
    """Serialize a snapshot back into the input JSON schema."""

    objects = {}
    for path, obj in snapshot.objects.items():
        entry: dict = {"type": obj["type"], "exported": obj["exported"]}
        if not obj["exported"]:
            objects[path] = entry
            continue
        entry["aces"] = [ace.to_dict() for ace in obj["aces"]]
        entry["inheritable_aces"] = [
            ace.to_dict() for ace in obj.get("inheritable_aces", [])
        ]
        objects[path] = entry
    return {"version": snapshot.version + "-remediated" if snapshot.version else "remediated",
            "objects": objects}


def reverify(
    inventory: List[ServiceConfig],
    directory: SubjectDirectory,
    fixed_snapshot: PermissionSnapshot,
) -> List[dict]:
    """Re-run the full analysis on the fixed snapshot and report, for
    every ordinary (resolved, non-privileged) account, whether it can
    still modify file contents or replace directory entries."""

    findings = evaluate_inventory(inventory, directory, fixed_snapshot)
    rows = []
    for finding in findings:
        if not finding.subject_resolved or finding.privileged:
            continue
        for action in ACTIONS:
            result = finding.results[action]
            rows.append(
                {
                    "service": finding.service,
                    "path": finding.path,
                    "subject_id": finding.subject_id,
                    "action": action,
                    "post_fix_decision": result.decision,
                    "still_allowed": result.decision == ALLOW,
                    "basis": list(result.reasons),
                }
            )
    return rows


def remediate(
    inventory: List[ServiceConfig],
    directory: SubjectDirectory,
    snapshot: PermissionSnapshot,
    findings,
) -> Tuple[RemediationPlan, PermissionSnapshot, List[dict]]:
    plan = build_plan(inventory, directory, snapshot, findings)
    fixed = apply_remediation(snapshot, plan)
    recheck = reverify(inventory, directory, fixed)
    return plan, fixed, recheck
