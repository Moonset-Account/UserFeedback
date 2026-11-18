"""初始化覆盖审查（must-analysis）。

语义要点：
- 写入只覆盖实际区间（字段按布局区间、原始写入按 offset/length），不做对齐放大；
- 分支汇合时取交集：只有所有前驱路径都已初始化的区间才被视为安全；
- 复制指令把缓冲区的一部分复制到外部诊断接口，仅当整个输出区间在
  所有到达路径上均已初始化时判定为 SAFE；
- 覆盖不全时列出每个未初始化输出区间，以及未覆盖它的具体路径；
- 复制长度为动态值且缺少缓冲区布局时，标记 PENDING（留待核实）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import intervals as iv
from .model import Spec

PATH_CAP = 128  # 路径枚举上限，超出后报告标记为截断

SAFE = "SAFE"
UNSAFE = "UNSAFE"
PENDING = "PENDING"

ACCEPTABLE = "ACCEPTABLE"
REJECTED = "REJECTED"
VERDICT_PENDING = "PENDING"


@dataclass
class UnsafeGap:
    start: int
    end: int | None
    uninit_paths: list = field(default_factory=list)  # 每条路径是节点 id 序列
    paths_truncated: bool = False

    def to_dict(self):
        return {
            "start": self.start,
            "end": self.end,
            "uninitialized_paths": [list(p) for p in self.uninit_paths],
            "paths_truncated": self.paths_truncated,
        }


@dataclass
class CopyFinding:
    node: str
    op_index: int
    offset: int
    length: object  # int，或 None 表示动态长度
    status: str
    reason: str
    safe_intervals: list = field(default_factory=list)
    unsafe_gaps: list = field(default_factory=list)

    def to_dict(self):
        return {
            "node": self.node,
            "op_index": self.op_index,
            "offset": self.offset,
            "length": "dynamic" if self.length is None else self.length,
            "status": self.status,
            "reason": self.reason,
            "safe_intervals": [[s, e] for s, e in self.safe_intervals],
            "unsafe_gaps": [g.to_dict() for g in self.unsafe_gaps],
        }


@dataclass
class Report:
    verdict: str
    findings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
        }


class Analyzer:
    def __init__(self, spec: Spec):
        self.spec = spec
        self.nodes = {n.id: n for n in spec.nodes}
        self.fields = {f.name: f for f in spec.buffer.fields}
        self.preds = {n.id: [] for n in spec.nodes}
        for n in spec.nodes:
            for s in n.succ:
                self.preds[s].append(n.id)

    # ---- 状态转移 -------------------------------------------------------

    def _apply_write(self, state, op):
        if op.kind == "write_field":
            f = self.fields[op.args["field"]]
            return iv.add(state, f.offset, f.offset + f.size)
        if op.kind == "write":
            return iv.add(state, op.args["offset"],
                          op.args["offset"] + op.args["length"])
        if op.kind == "zero_buffer":
            # 缓冲区长度为动态值时，清零覆盖 [0, +∞)
            return iv.add(state, 0, self.spec.buffer.size)
        return state

    def _transfer(self, state, node):
        for op in node.ops:
            if op.kind != "copy":
                state = self._apply_write(state, op)
        return state

    # ---- 汇合取交集的 must-analysis（不动点迭代，支持回边） -------------

    def _must_analysis(self):
        inn = {}
        out = {nid: [] for nid in self.nodes}
        for _ in range(1000):
            changed = False
            for nid, node in self.nodes.items():
                if nid == self.spec.entry or not self.preds[nid]:
                    merged = []
                else:
                    merged = None
                    for p in self.preds[nid]:
                        merged = out[p] if merged is None else iv.intersect(merged, out[p])
                inn[nid] = merged
                new_out = self._transfer(merged, node)
                if new_out != out[nid]:
                    out[nid] = new_out
                    changed = True
            if not changed:
                return inn
        raise RuntimeError("must-analysis 未收敛")

    # ---- 路径枚举（用于报告未覆盖区间对应的路径） ------------------------

    def _enumerate_paths(self, target):
        cap = PATH_CAP + 1
        paths = []
        stack = [(self.spec.entry, [self.spec.entry])]
        while stack and len(paths) < cap:
            nid, path = stack.pop()
            if nid == target:
                paths.append(path)
                continue
            for s in self.nodes[nid].succ:
                if s not in path:  # 只枚举简单路径
                    stack.append((s, path + [s]))
        truncated = len(paths) > PATH_CAP
        return paths[:PATH_CAP], truncated

    def _state_along(self, path, copy_op_index):
        """沿一条具体路径模拟，返回复制指令执行前的已初始化区间。"""
        state = []
        for nid in path[:-1]:
            state = self._transfer(state, self.nodes[nid])
        for op in self.nodes[path[-1]].ops[:copy_op_index]:
            if op.kind != "copy":
                state = self._apply_write(state, op)
        return state

    # ---- 复制指令检查 ----------------------------------------------------

    def _check_copy(self, node, op_index, op, state):
        off = op.args["offset"]
        length = op.args["length"]
        if length == "dynamic":
            if self.spec.buffer.size is None:
                return CopyFinding(
                    node.id, op_index, off, None, PENDING,
                    "复制长度为动态值且缺少缓冲区布局，留待核实")
            end = self.spec.buffer.size
            length_val = None
        else:
            end = off + length
            length_val = length

        want = [(off, end)]
        safe = iv.intersect(want, state)
        gaps = iv.subtract(want, state)
        if not gaps:
            return CopyFinding(
                node.id, op_index, off, length_val, SAFE,
                "输出区间在所有路径上均已初始化", safe_intervals=safe)

        paths, truncated = self._enumerate_paths(node.id)
        unsafe = []
        for gs, ge in gaps:
            bad = [p for p in paths
                   if not iv.covers(self._state_along(p, op_index), gs, ge)]
            note = UnsafeGap(gs, ge, bad, truncated)
            unsafe.append(note)
        reason = "输出区间存在未在所有路径上初始化的部分"
        if not paths:
            reason += "（复制节点不可达）"
        return CopyFinding(
            node.id, op_index, off, length_val, UNSAFE, reason,
            safe_intervals=safe, unsafe_gaps=unsafe)

    # ---- 入口 ------------------------------------------------------------

    def run(self):
        inn = self._must_analysis()
        findings = []
        for nid, node in self.nodes.items():
            state = inn[nid]
            for idx, op in enumerate(node.ops):
                if op.kind == "copy":
                    findings.append(self._check_copy(node, idx, op, state))
                else:
                    state = self._apply_write(state, op)
        if any(f.status == UNSAFE for f in findings):
            verdict = REJECTED
        elif any(f.status == PENDING for f in findings):
            verdict = VERDICT_PENDING
        else:
            verdict = ACCEPTABLE
        return Report(verdict, findings)
