"""采集引擎：维护暂存树（staging）并物化不可变的发布快照（published）。

设计要点见 docs/PROTOCOL.md：
- 暂存树与发布结果严格分开，外部只能拿到 publish() 的深拷贝快照；
- 子补丁先于父节点时进入待解析队列，父节点出现后按 seq 重放；
- 列表插入按 (index, seq) 稳定排序、按 item_id 去重后重建，天然容忍乱序/重复；
- 局部错误保留来源（source_seq + 原始错误体），并可被后续补丁恢复；
- 未收到 end（或 end 后仍有未解析项）时暂存树永远不被标成完整。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .parser import ParseError, ParsedMessage, parse_line
from .paths import PathError, ensure_list_for_insert, ensure_writable, resolve_node, resolve_parent

ERROR_MARKER = "__error__"


@dataclass
class IngestDecision:
    """每条输入消息（含坏行）对应一条可审计决策，即页面上的“来源证据”。"""

    seq: int | None
    kind: str
    accepted: bool
    action: str
    reason: str | None
    path: str | None
    raw: Any
    snapshot_no: int


@dataclass
class _Insert:
    seq: int
    path: str
    index: int
    item: Any
    item_id: str


@dataclass
class _Pending:
    message: ParsedMessage
    reason: str


class CollectorEngine:
    def __init__(self) -> None:
        # ---- 暂存区（私有工作状态，绝不直接外发）----
        self._tree: dict[str, Any] | None = None
        self._inserts: list[_Insert] = []
        self._errors: dict[str, dict[str, Any]] = {}  # path -> 错误登记
        self._pending: list[_Pending] = []
        self._seen_seq: set[int] = set()
        self._ended = False
        self._interrupted = False
        self._interrupt_reason: str | None = None

        # ---- 审计与发布 ----
        self.decisions: list[IngestDecision] = []
        self.snapshots: list[dict[str, Any]] = []
        self.stats = {
            "received": 0,
            "accepted": 0,
            "rejected": 0,
            "pending": 0,
            "duplicates": 0,
            "errors": 0,
            "inserts": 0,
        }
        # 0 号快照：采集尚未开始的空状态
        self._publish("init")

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def ingest_line(self, line: str) -> IngestDecision:
        """喂入一行 NDJSON（含坏行），返回审计决策并刷新发布快照。"""
        self.stats["received"] += 1
        try:
            message = parse_line(line)
        except ParseError as exc:
            return self._reject(
                seq=None, kind="unparseable", action="reject",
                reason=exc.code, path=None, raw=line,
            )
        return self.ingest_message(message)

    def ingest_message(self, msg: ParsedMessage) -> IngestDecision:
        """已解析消息的统一入口。"""
        if self._interrupted:
            return self._reject(msg.seq, msg.kind, "reject", "after_interrupt",
                                msg.path, msg.raw)
        if self._ended:
            return self._reject(msg.seq, msg.kind, "reject", "after_end",
                                msg.path, msg.raw)

        if msg.kind == "initial":
            if self._tree is not None:
                return self._reject(msg.seq, msg.kind, "reject",
                                    "duplicate_initial", None, msg.raw)
            self._tree = copy.deepcopy(msg.data)
            return self._accept(msg, "initial", None)

        # initial 之前不允许任何增量消息
        if self._tree is None:
            return self._reject(msg.seq, msg.kind, "reject", "before_initial",
                                msg.path, msg.raw)

        # 全流 seq 去重：同 seq 消息（无论类型）只生效一次
        if msg.seq in self._seen_seq:
            self.stats["duplicates"] += 1
            return self._reject(msg.seq, msg.kind, "reject", "duplicate_seq",
                                msg.path, msg.raw)

        if msg.kind == "end":
            self._seen_seq.add(msg.seq)
            self._ended = True
            return self._accept(msg, "end", None)

        decision = self._apply(msg)
        if decision is None:
            # 成功落入暂存树
            self._seen_seq.add(msg.seq)
            self._replay_pending()
            return self._accept(msg, "apply", None)
        if decision == "pending":
            # 父节点缺失：挂起，不占 seq，允许后续随重放生效
            self._pending.append(_Pending(msg, "missing_parent"))
            return self._accept(msg, "pending", "missing_parent")
        # 明确非法（路径语法/越界/类型错误）：永久拒绝
        return self._reject(msg.seq, msg.kind, "reject", decision, msg.path, msg.raw)

    def mark_interrupted(self, reason: str) -> None:
        """传输层中断/文件意外结束时调用：冻结暂存树为不完整状态。"""
        if self._ended or self._interrupted:
            return
        self._interrupted = True
        self._interrupt_reason = reason
        self._publish("interrupt")

    # ------------------------------------------------------------------
    # 暂存区写入
    # ------------------------------------------------------------------
    def _apply(self, msg: ParsedMessage) -> str | None:
        """返回 None 表示成功；'pending' 表示需挂起；其它字符串为拒绝原因码。"""
        try:
            if msg.kind == "patch":
                parent, key = ensure_writable(self._tree, msg.path)  # type: ignore[arg-type]
                parent[key] = copy.deepcopy(msg.value)
                self._errors.pop(msg.path, None)  # 真实值到达，错误恢复
                return None

            if msg.kind == "error":
                # 错误同样要求父节点存在；父缺失则挂起等待
                parent, key = resolve_parent(self._tree, msg.path)  # type: ignore[arg-type]
                if isinstance(parent, list) and not (0 <= int(key) < len(parent)):
                    return "out_of_range"
                self._errors[msg.path] = {
                    "code": msg.error["code"],  # type: ignore[index]
                    "message": msg.error.get("message", ""),  # type: ignore[union-attr]
                    "source_seq": msg.seq,
                    "raw": msg.error,
                }
                return None

            if msg.kind == "insert":
                ensure_list_for_insert(self._tree, msg.path, msg.index)  # type: ignore[arg-type]
                item_id = msg.item_id or f"seq-{msg.seq}"
                if any(ins.path == msg.path and ins.item_id == item_id
                       for ins in self._inserts):
                    return "duplicate_item"
                self._inserts.append(_Insert(
                    seq=msg.seq, path=msg.path, index=msg.index,  # type: ignore[arg-type]
                    item=copy.deepcopy(msg.item), item_id=item_id,
                ))
                return None
        except PathError as exc:
            if exc.code == "missing_parent":
                return "pending"
            return exc.code

        return "unknown_type"

    def _replay_pending(self) -> None:
        """父节点可能已被新补丁创建，按 seq 顺序重放挂起消息，直到无进展。"""
        if not self._pending:
            return
        progressed = True
        while progressed and self._pending:
            progressed = False
            for pending in sorted(self._pending, key=lambda p: (p.message.seq or 0)):
                msg = pending.message
                outcome = self._apply(msg)
                if outcome is None:
                    self._seen_seq.add(msg.seq)
                    self._pending.remove(pending)
                    self.decisions.append(IngestDecision(
                        seq=msg.seq, kind=msg.kind, accepted=True,
                        action="replay", reason=None, path=msg.path,
                        raw=msg.raw, snapshot_no=len(self.snapshots),
                    ))
                    self.stats["accepted"] += 1
                    progressed = True
                elif outcome != "pending":
                    # 重放时发现路径其实非法（例如父节点类型不对），永久拒绝
                    self._pending.remove(pending)
                    self._reject(msg.seq, msg.kind, "reject", outcome,
                                 msg.path, msg.raw)
                    progressed = True
        self.stats["pending"] = len(self._pending)

    # ------------------------------------------------------------------
    # 发布：从暂存区物化不可变快照
    # ------------------------------------------------------------------
    def _publish(self, trigger: str) -> dict[str, Any]:
        if self._tree is None:
            tree_out: Any = None
        else:
            tree_out = copy.deepcopy(self._tree)
            self._materialize_inserts(tree_out)
            self._materialize_errors(tree_out)

        unresolved = [
            {"seq": p.message.seq, "kind": p.message.kind,
             "path": p.message.path, "reason": p.reason}
            for p in sorted(self._pending, key=lambda p: (p.message.seq or 0))
        ]
        errors_out = [
            {"path": path, **copy.deepcopy(info)}
            for path, info in sorted(self._errors.items())
        ]

        if self._interrupted:
            status = "interrupted"
            complete = False
        elif self._ended and not self._pending:
            status = "complete"
            complete = True
        elif self._ended:
            status = "ended_unresolved"
            complete = False
        else:
            status = "streaming"
            complete = False

        snapshot = {
            "snapshot_no": len(self.snapshots),
            "trigger": trigger,
            "status": status,
            "complete": complete,
            "tree": tree_out,
            "errors": errors_out,
            "unresolved": unresolved,
            "stats": dict(self.stats),
        }
        self.snapshots.append(snapshot)
        return snapshot

    def _materialize_inserts(self, tree_out: Any) -> None:
        """按路径分组，(index, seq) 稳定排序 + item_id 去重后重建每个列表。"""
        by_path: dict[str, list[_Insert]] = {}
        for ins in self._inserts:
            by_path.setdefault(ins.path, []).append(ins)
        for path, inserts in by_path.items():
            try:
                target = resolve_node(tree_out, path)
            except PathError:
                continue  # 列表宿主尚未物化（理论上不会发生，防御性处理）
            if not isinstance(target, list):
                continue
            ordered = sorted(inserts, key=lambda i: (i.index, i.seq))
            seen: set[str] = set()
            rebuilt: list[Any] = []
            for ins in ordered:
                if ins.item_id in seen:
                    continue
                seen.add(ins.item_id)
                rebuilt.append(copy.deepcopy(ins.item))
            target.clear()
            target.extend(rebuilt)

    def _materialize_errors(self, tree_out: Any) -> None:
        for path, info in self._errors.items():
            try:
                parent, key = resolve_parent(tree_out, path)
            except PathError:
                continue
            if isinstance(parent, list) and not (0 <= int(key) < len(parent)):
                continue
            parent[key] = {
                ERROR_MARKER: True,
                "code": info["code"],
                "message": info["message"],
                "source_seq": info["source_seq"],
            }

    # ------------------------------------------------------------------
    # 审计辅助
    # ------------------------------------------------------------------
    def _accept(self, msg: ParsedMessage, action: str, reason: str | None) -> IngestDecision:
        self.stats["accepted"] += 1
        if msg.kind == "insert":
            self.stats["inserts"] += 1
        if msg.kind == "error":
            self.stats["errors"] += 1
        if action == "pending":
            self.stats["pending"] = len(self._pending)
        snapshot = self._publish(action)
        decision = IngestDecision(
            seq=msg.seq, kind=msg.kind, accepted=True, action=action,
            reason=reason, path=msg.path, raw=msg.raw,
            snapshot_no=snapshot["snapshot_no"],
        )
        self.decisions.append(decision)
        return decision

    def _reject(self, seq: int | None, kind: str, action: str, reason: str,
                path: str | None, raw: Any) -> IngestDecision:
        self.stats["rejected"] += 1
        if reason in ("duplicate_seq", "duplicate_item"):
            self.stats["duplicates"] += 1
        snapshot = self._publish("reject")
        decision = IngestDecision(
            seq=seq, kind=kind, accepted=False, action=action,
            reason=reason, path=path, raw=raw,
            snapshot_no=snapshot["snapshot_no"],
        )
        self.decisions.append(decision)
        return decision

    # ------------------------------------------------------------------
    # 结果摘要
    # ------------------------------------------------------------------
    def result(self) -> dict[str, Any]:
        status = self.snapshots[-1]["status"]
        return {
            "status": status,
            "complete": status == "complete",
            "interrupted": self._interrupted,
            "interrupt_reason": self._interrupt_reason,
            "pending_count": len(self._pending),
            "snapshot_count": len(self.snapshots),
            "decision_count": len(self.decisions),
            "stats": dict(self.stats),
            "final_snapshot": self.snapshots[-1],
            "snapshots": self.snapshots,
            "decisions": [d.__dict__ for d in self.decisions],
        }
