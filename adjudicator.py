"""粘贴裁决核心模块。

输入（由系统适配器确认）：源工作区、目标工作区、数据分级、剪贴板版本、操作时间。
输出：动作（直接粘贴 / 仅以文本粘贴 / 阻止 / 用户取消）+ 可审计原因。

确认作用域：一次用户确认只对 (源工作区, 目标工作区, 剪贴板版本) 三元组有效。
确认之后若目标应用切换或剪贴板内容变化（版本号变化），必须重新判断。

审计约束：审计记录只引用剪贴板版本和数据分级，绝不记录剪贴板内容本身。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# 常量域
# ---------------------------------------------------------------------------

class WorkspaceKind:
    MANAGED = "managed"
    PERSONAL = "personal"


class ContentKind:
    TEXT = "text"
    IMAGE = "image"


class Classification:
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"

    ALL = (PUBLIC, INTERNAL, CONFIDENTIAL, SECRET)


class Decision(Enum):
    ALLOW = "allow"                      # 直接粘贴
    TEXT_ONLY = "text_only"              # 仅以文本粘贴（降级）
    BLOCK = "block"                      # 阻止
    REQUIRE_CONFIRM = "require_confirm"  # 等待用户确认


class Action(Enum):
    PASTE = "paste"               # 直接粘贴，完整保真
    PASTE_TEXT = "paste_text"     # 降级：只交付文本表示
    BLOCK = "block"               # 策略阻止
    CANCEL = "cancel"             # 用户在确认弹框中拒绝


# ---------------------------------------------------------------------------
# 输入快照
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    """一次粘贴请求时由系统适配器确认的输入。"""
    source_workspace: str
    target_workspace: str
    classification: str
    content_kind: str
    clipboard_version: int
    timestamp: float

    @classmethod
    def from_adapter(cls, adapter) -> "Snapshot":
        return cls(
            source_workspace=adapter.get_source_workspace(),
            target_workspace=adapter.get_target_workspace(),
            classification=adapter.get_classification(),
            content_kind=adapter.get_content_kind(),
            clipboard_version=adapter.get_clipboard_version(),
            timestamp=adapter.now(),
        )


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------

class Policy:
    """粘贴策略。区分图片与文本；文本降级时只把文本表示交给目标。"""

    def decide(self, snap: Snapshot) -> Tuple[Decision, str]:
        if snap.classification not in Classification.ALL:
            return Decision.BLOCK, "unknown-classification"

        # 同一信任域内（受管 -> 受管）：直接粘贴
        if snap.source_workspace == WorkspaceKind.MANAGED and \
                snap.target_workspace == WorkspaceKind.MANAGED:
            return Decision.ALLOW, "same-managed-domain"

        # 个人 -> 受管（流入受管域）：直接粘贴
        if snap.target_workspace == WorkspaceKind.MANAGED:
            return Decision.ALLOW, "inbound-to-managed"

        # 以下为 受管 -> 个人（跨出受管边界）
        if snap.classification == Classification.PUBLIC:
            return Decision.ALLOW, "public-data"

        if snap.classification == Classification.INTERNAL:
            if snap.content_kind == ContentKind.TEXT:
                # 文本可降级：只交付文本表示
                return Decision.TEXT_ONLY, "internal-text-downgraded"
            # 图片无法做文本降级，需要用户确认
            return Decision.REQUIRE_CONFIRM, "internal-image-cannot-downgrade"

        if snap.classification == Classification.CONFIDENTIAL:
            return Decision.REQUIRE_CONFIRM, "confidential-requires-confirm"

        # SECRET
        return Decision.BLOCK, "secret-blocked"


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditRecord:
    """审计记录：只引用剪贴板版本与数据分级，不含任何剪贴板内容。"""
    seq: int
    timestamp: float
    event: str                 # "decision" | "confirm" | "result"
    source_workspace: str
    target_workspace: str
    content_kind: str
    classification: str        # 数据分级（允许引用）
    clipboard_version: int     # 剪贴板版本（允许引用）
    decision: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "event": self.event,
            "source_workspace": self.source_workspace,
            "target_workspace": self.target_workspace,
            "content_kind": self.content_kind,
            "classification": self.classification,
            "clipboard_version": self.clipboard_version,
            "decision": self.decision,
            "reason": self.reason,
        }


class AuditLog:
    def __init__(self) -> None:
        self.records: List[AuditRecord] = []

    def record(self, event: str, snap: Snapshot, decision: str, reason: str) -> AuditRecord:
        rec = AuditRecord(
            seq=len(self.records) + 1,
            timestamp=snap.timestamp,
            event=event,
            source_workspace=snap.source_workspace,
            target_workspace=snap.target_workspace,
            content_kind=snap.content_kind,
            classification=snap.classification,
            clipboard_version=snap.clipboard_version,
            decision=decision,
            reason=reason,
        )
        self.records.append(rec)
        return rec

    def to_json_lines(self) -> str:
        return "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in self.records)


# ---------------------------------------------------------------------------
# 裁决结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PasteResult:
    action: Action
    reason: str
    snapshot: Snapshot
    delivered_payload: Optional[str]  # BLOCK/CANCEL 时为 None
    rounds: int = 1                   # 因上下文变化而重新判断的次数

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "delivered_payload": self.delivered_payload,
            "rounds": self.rounds,
            "snapshot": {
                "source_workspace": self.snapshot.source_workspace,
                "target_workspace": self.snapshot.target_workspace,
                "classification": self.snapshot.classification,
                "content_kind": self.snapshot.content_kind,
                "clipboard_version": self.snapshot.clipboard_version,
                "timestamp": self.snapshot.timestamp,
            },
        }


# ---------------------------------------------------------------------------
# 裁决器
# ---------------------------------------------------------------------------

class Adjudicator:
    """粘贴裁决器。确认只对 (源, 目标, 剪贴板版本) 有效。"""

    MAX_ROUNDS = 8  # 上下文反复变化时的兜底，防止弹框期间状态抖动造成死循环

    def __init__(self, policy: Optional[Policy] = None, audit: Optional[AuditLog] = None) -> None:
        self.policy = policy if policy is not None else Policy()
        self.audit = audit if audit is not None else AuditLog()
        # 确认键：(源工作区, 目标工作区, 剪贴板版本)
        self._confirmations: set = set()

    def handle_paste(self, adapter) -> PasteResult:
        rounds = 0
        while True:
            rounds += 1
            if rounds > self.MAX_ROUNDS:
                snap = Snapshot.from_adapter(adapter)
                self.audit.record("result", snap, Action.BLOCK.value, "context-unstable")
                return PasteResult(Action.BLOCK, "context-unstable", snap, None, rounds)

            snap = Snapshot.from_adapter(adapter)
            decision, reason = self.policy.decide(snap)
            self.audit.record("decision", snap, decision.value, reason)

            if decision is Decision.REQUIRE_CONFIRM:
                key = (snap.source_workspace, snap.target_workspace, snap.clipboard_version)
                if key in self._confirmations:
                    # 同一源、同一目标、同一剪贴板版本：确认仍然有效
                    self.audit.record("result", snap, Action.PASTE.value, "user-confirmed")
                    return PasteResult(Action.PASTE, "user-confirmed", snap,
                                       adapter.get_payload(), rounds)
                approved = adapter.confirm(
                    "允许将 {cls} 数据（剪贴板版本 v{ver}）从 {src} 粘贴到 {dst}？".format(
                        cls=snap.classification, ver=snap.clipboard_version,
                        src=snap.source_workspace, dst=snap.target_workspace))
                self.audit.record("confirm", snap,
                                  "approved" if approved else "declined", reason)
                if not approved:
                    self.audit.record("result", snap, Action.CANCEL.value, "user-declined")
                    return PasteResult(Action.CANCEL, "user-declined", snap, None, rounds)
                self._confirmations.add(key)
                # 弹框期间用户可能切换了目标应用或改动了剪贴板：
                # 回到循环顶部重新取快照、重新判断。
                continue

            if decision is Decision.ALLOW:
                self.audit.record("result", snap, Action.PASTE.value, reason)
                return PasteResult(Action.PASTE, reason, snap, adapter.get_payload(), rounds)

            if decision is Decision.TEXT_ONLY:
                # 文本降级：只把文本表示交给目标
                self.audit.record("result", snap, Action.PASTE_TEXT.value, reason)
                return PasteResult(Action.PASTE_TEXT, reason, snap,
                                   adapter.get_text_representation(), rounds)

            # BLOCK
            self.audit.record("result", snap, Action.BLOCK.value, reason)
            return PasteResult(Action.BLOCK, reason, snap, None, rounds)
