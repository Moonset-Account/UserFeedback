"""束管理器：候选排序选择、稳定编号、KV 页生命周期与逐步执行。

选择规则（全部可复现，不依赖后端返回顺序）：

1. 候选按 ``(累计分数降序, 父束稳定编号升序, token 升序, 后端列表下标升序)``
   排序，因此分数并列时由稳定编号而不是易变的父束数组位置决定次序。
2. 单遍扫描全部排名结果：
   * 非结束候选依次成为存活束，直到达到固定宽度 ``width``；
   * 结束候选只有落在前 ``width`` 个排名位（本可占据固定宽度名额）内，
     才被收入结束假设（单独维护，不占用存活束槽位），其名额由后续
     非结束候选按排名递补；其余结束候选丢弃；
   * 候选不足时存活束可以少于宽度。
3. 存活束按入选顺序取得稳定编号与家槽 ``0..k-1``。

页规则：

* 同父多孩 -> 写时复制：共享只读历史页（含父末页），每个孩子的新末页
  先 :class:`CopyPage` 到独立可写位置再 :class:`AppendPage`。
* 独占孩子直接继承父末页（仅在需要换槽时 :class:`MovePage`）。
* 父末页写满（达到 ``block_size``）时，孩子分配全新页（FRESH+APPEND）。
* 规划与执行分离：:meth:`BeamManager.plan_step` 只产出计划，物理内存不变；
  :meth:`BeamManager.execute_plan` 逐操作执行并提交；:meth:`step` 二者合一。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .planner import (
    AppendPage,
    CapacityError,
    ChildSpec,
    CopyPage,
    DropPage,
    MovePage,
    PageOp,
    PhysicalMemory,
    build_step_plan,
)

__all__ = ["Candidate", "Beam", "StepPlan", "BeamManager", "CapacityError"]

ROOT_BEAM_ID = 0


@dataclass(frozen=True)
class Candidate:
    """后端一个时间步返回的单个候选。

    ``parent_beam_id`` 是父束的稳定编号（与父束在后端数组中的位置无关）。
    """

    parent_beam_id: int
    token: int
    score: float
    ended: bool = False


@dataclass(frozen=True)
class Beam:
    """一个束假设（存活或已结束）。"""

    beam_id: int
    parent_beam_id: int
    token: Optional[int]          # 根束为 None
    score: float
    ended: bool
    page_ids: Tuple[int, ...]     # 从根到末页的逻辑 KV 页链
    home_slot: Optional[int]      # 存活束的家槽；结束束为 None

    def trace_tokens(self, registry: "Dict[int, Beam]") -> Tuple[int, ...]:
        """沿稳定父链回溯完整 token 序列。"""
        tokens: List[int] = []
        node: Optional[Beam] = self
        while node is not None and node.token is not None:
            tokens.append(node.token)
            node = registry.get(node.parent_beam_id)
        return tuple(reversed(tokens))


@dataclass
class StepPlan:
    """一个时间步的可执行计划（执行前物理内存不发生任何变化）。"""

    step: int
    ops: List[PageOp]
    active_beams: List[Beam] = field(default_factory=list)
    finished_beams: List[Beam] = field(default_factory=list)
    ranked_candidates: List[Candidate] = field(default_factory=list)
    admitted_indices: List[int] = field(default_factory=list)
    slots_before: Dict[int, int] = field(default_factory=dict)
    slots_after: Dict[int, int] = field(default_factory=dict)


@dataclass
class _Pending:
    """入选候选的临时构造信息（不触碰不可变 Beam）。"""

    candidate: Candidate
    home: Optional[int]
    parent_tail: int
    tail_full: bool


class BeamManager:
    """固定宽度束搜索 + 分页 KV 搬移调度管理器。"""

    def __init__(
        self,
        width: int,
        heap_slots: int,
        block_size: int = 16,
    ) -> None:
        if width <= 0:
            raise ValueError("width 必须为正整数")
        if block_size <= 0:
            raise ValueError("block_size 必须为正整数")
        self.width = width
        self.block_size = block_size
        self.memory = PhysicalMemory(width=width, heap_slots=heap_slots)

        # 稳定编号与页编号均单调递增、永不复用
        self._next_beam_id = 1
        self._next_pid = 1

        # 根束：持有一个空的初始末页，放在 0 号家槽
        root_pid = self._new_pid()
        self.memory.create_empty(pid=root_pid, slot=0)
        root = Beam(
            beam_id=ROOT_BEAM_ID,
            parent_beam_id=-1,
            token=None,
            score=0.0,
            ended=False,
            page_ids=(root_pid,),
            home_slot=0,
        )
        self.beams: Dict[int, Beam] = {ROOT_BEAM_ID: root}
        self.active_beam_ids: List[int] = [ROOT_BEAM_ID]
        self.finished: List[Beam] = []
        self.step_count = 0

    # ------------------------------------------------------------------ #
    # 公开只读视图
    # ------------------------------------------------------------------ #
    @property
    def active_beams(self) -> List[Beam]:
        return [self.beams[b] for b in self.active_beam_ids]

    def trace(self, beam_id: int) -> Tuple[int, ...]:
        """返回某束从根到当前的完整 token 序列。"""
        return self.beams[beam_id].trace_tokens(self.beams)

    def page_tokens(self, pid: int) -> Tuple[int, ...]:
        return self.memory.page_tokens(pid)

    def slot_of(self, pid: int) -> int:
        return self.memory.slot_for(pid)

    # ------------------------------------------------------------------ #
    # 规划
    # ------------------------------------------------------------------ #
    def plan_step(self, candidates: Sequence[Candidate]) -> StepPlan:
        self._validate_candidates(candidates)
        order, ranked = self._rank(candidates)
        active_picks, finished_picks, admitted = self._admit(ranked, order)

        # 1) 为入选候选收集临时构造信息
        pending: List[_Pending] = []
        for home, (_, cand) in enumerate(active_picks):
            parent = self.beams[cand.parent_beam_id]
            tail = parent.page_ids[-1]
            pending.append(
                _Pending(
                    candidate=cand,
                    home=home,
                    parent_tail=tail,
                    tail_full=len(self.memory.pages[tail]) >= self.block_size,
                )
            )
        for _, cand in finished_picks:
            parent = self.beams[cand.parent_beam_id]
            tail = parent.page_ids[-1]
            pending.append(
                _Pending(
                    candidate=cand,
                    home=None,
                    parent_tail=tail,
                    tail_full=len(self.memory.pages[tail]) >= self.block_size,
                )
            )

        # 2) 按父末页分组，决定独占继承 / 写时复制 / 全新页，并构造 Beam
        groups: Dict[int, List[_Pending]] = {}
        for item in pending:
            groups.setdefault(item.parent_tail, []).append(item)

        child_specs: List[ChildSpec] = []
        new_beams: List[Beam] = []
        new_finished: List[Beam] = []

        for item in pending:
            cand = item.candidate
            parent = self.beams[cand.parent_beam_id]
            siblings = groups[item.parent_tail]

            if item.tail_full:
                # 父末页已满：每个孩子分配全新可写页（FRESH+APPEND）
                new_pid: Optional[int] = self._new_pid()
                source_pid: Optional[int] = None
                chain = parent.page_ids + (new_pid,)
            elif len(siblings) == 1:
                # 独占：直接继承父末页（可能随后被 MOVE 到孩子的家槽）
                new_pid = None
                source_pid = item.parent_tail
                chain = parent.page_ids
            else:
                # 同父分叉：写时复制，共享只读历史，末页独立可写
                new_pid = self._new_pid()
                source_pid = item.parent_tail
                chain = parent.page_ids + (new_pid,)

            beam = Beam(
                beam_id=self._next_beam_id,
                parent_beam_id=parent.beam_id,
                token=cand.token,
                score=cand.score,
                ended=cand.ended,
                page_ids=chain,
                home_slot=item.home,
            )
            self._next_beam_id += 1
            child_specs.append(
                ChildSpec(
                    new_pid=new_pid,
                    source_pid=source_pid,
                    home=item.home,
                    token=cand.token,
                )
            )
            if item.home is None:
                new_finished.append(beam)
            else:
                new_beams.append(beam)

        # 3) 存活旧页 / 结束束永久保留页 / 死页
        surviving_pids = {
            pid for beam in new_beams + new_finished for pid in beam.page_ids
        }
        retained_pids = {
            pid for beam in self.finished for pid in beam.page_ids
        }
        old_pids = set(self.memory.slot_of_pid)
        dead_pids = sorted(
            old_pids - surviving_pids - retained_pids
        )
        existing_pids = sorted(old_pids & surviving_pids)

        slots_before = dict(self.memory.slot_of_pid)
        ops = build_step_plan(
            width=self.width,
            heap_slots=self.memory.heap_slots,
            slots=slots_before,
            existing_pids=existing_pids,
            dead_pids=dead_pids,
            retained_pids=sorted(retained_pids),
            children=child_specs,
        )
        slots_after = self._preview_slots(slots_before, ops)

        return StepPlan(
            step=self.step_count,
            ops=list(ops),
            active_beams=new_beams,
            finished_beams=new_finished,
            ranked_candidates=ranked,
            admitted_indices=admitted,
            slots_before=slots_before,
            slots_after=slots_after,
        )

    # ------------------------------------------------------------------ #
    # 执行与提交
    # ------------------------------------------------------------------ #
    def execute_plan(self, plan: StepPlan) -> None:
        if plan.step != self.step_count:
            raise ValueError("计划与当前时间步不匹配（可能已被执行）")
        # 逐操作执行；任何前提失败都会由 PhysicalMemory 抛 AssertionError，
        # 此时内存停在失败点，束状态尚未提交。
        self.memory.apply_all(plan.ops)

        for beam in plan.active_beams + plan.finished_beams:
            self.beams[beam.beam_id] = beam
        self.active_beam_ids = [b.beam_id for b in plan.active_beams]
        self.finished.extend(plan.finished_beams)
        self.finished.sort(key=lambda b: (-b.score, b.beam_id))
        self.step_count += 1

    def step(self, candidates: Sequence[Candidate]) -> StepPlan:
        plan = self.plan_step(candidates)
        self.execute_plan(plan)
        return plan

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _new_pid(self) -> int:
        pid = self._next_pid
        self._next_pid += 1
        return pid

    def _validate_candidates(self, candidates: Sequence[Candidate]) -> None:
        if not candidates:
            raise ValueError("候选列表为空")
        active = set(self.active_beam_ids)
        for i, cand in enumerate(candidates):
            if cand.parent_beam_id not in self.beams:
                raise ValueError(
                    f"候选 {i} 引用了不存在的父束 {cand.parent_beam_id}"
                )
            if cand.parent_beam_id not in active:
                raise ValueError(
                    f"候选 {i} 的父束 {cand.parent_beam_id} 不是存活束"
                )

    def _rank(
        self, candidates: Sequence[Candidate]
    ) -> Tuple[List[int], List[Candidate]]:
        """返回 (排序后的后端原始下标, 排序后的候选)。"""
        order = sorted(
            range(len(candidates)),
            key=lambda i: (
                -candidates[i].score,
                candidates[i].parent_beam_id,
                candidates[i].token,
                i,
            ),
        )
        return order, [candidates[i] for i in order]

    def _admit(
        self,
        ranked: Sequence[Candidate],
        order: Sequence[int],
    ) -> Tuple[
        List[Tuple[int, Candidate]],
        List[Tuple[int, Candidate]],
        List[int],
    ]:
        """单遍录取，保证存活束尽量补满固定宽度。

        * 结束候选只有落在前 ``width`` 个排名位（本可占据固定宽度名额）时
          才被收入结束假设，且不占存活槽位；
        * 结束名额让出来的存活槽位由后续非结束候选按排名递补，直到存活束
          达到 ``width``；候选整体不足时存活束可以少于宽度。
        """
        active_picks: List[Tuple[int, Candidate]] = []
        finished_picks: List[Tuple[int, Candidate]] = []
        admitted: List[int] = []

        for rank_pos, cand in enumerate(ranked):
            if rank_pos >= self.width and len(active_picks) >= self.width:
                break
            if cand.ended:
                if rank_pos < self.width:
                    finished_picks.append((rank_pos, cand))
                    admitted.append(order[rank_pos])
                continue
            if len(active_picks) < self.width:
                active_picks.append((rank_pos, cand))
                admitted.append(order[rank_pos])
        return active_picks, finished_picks, admitted

    @staticmethod
    def _preview_slots(
        slots_before: Dict[int, int],
        ops: Sequence[PageOp],
    ) -> Dict[int, int]:
        view = dict(slots_before)
        for op in ops:
            if isinstance(op, DropPage):
                del view[op.pid]
            elif isinstance(op, MovePage):
                view[op.pid] = op.dst
            elif isinstance(op, CopyPage):
                view[op.pid] = op.dst
            elif isinstance(op, AppendPage):
                if op.fresh:
                    view[op.pid] = op.dst
        return view
