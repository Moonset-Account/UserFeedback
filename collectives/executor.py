"""单进程驱动的集合通信执行器。

执行器在一个进程内以协作式轮询模拟多 rank 的非阻塞集合通信：

* 每轮按 rank 编号升序依次给每个 *未结束且未阻塞* 的 rank 执行一条
  指令；发起（``Initiate``）永远不阻塞，等待（``Wait``）在对应配对
  位置尚未收齐时让该 rank 阻塞，同轮其他 rank 继续前进。
* 每个通信组各自维护配对次序：rank 在某组的第 k 次发起，与同组其他
  成员各自的第 k 次发起落在同一个配对位置（slot），与组的声明顺序、
  发起先后均无关。成员名单相同的两个组拥有彼此独立的次序。
* 同一配对位置收齐前会检查标签，标签不一致立即抛出
  :class:`TagMismatchError`。
* 当一整轮没有任何 rank 能前进时判定停滞：若有阻塞 rank 所等的成员
  已经结束，抛出 :class:`MemberExitedError`；否则所有未结束 rank 都
  在等待且等待关系成环，抛出 :class:`DeadlockError` 并给出闭合等待链。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .errors import (
    DeadlockError,
    MemberExitedError,
    TagMismatchError,
    WaitEdge,
)
from .events import (
    Event,
    Initiated,
    RankBlocked,
    RankEnded,
    RankUnblocked,
    SlotCompleted,
)
from .groups import Group
from .program import Handle, Initiate, Instruction, Program, Wait
from .result import OpRecord, RunResult

_ACTIVE = "active"
_BLOCKED = "blocked"
_ENDED = "ended"


class Executor:
    """在单个进程内驱动多个 rank 指令序列的集合通信执行器。"""

    def __init__(
        self,
        groups: Sequence[Group],
        programs: Mapping[int, Program | Sequence[Instruction]],
    ) -> None:
        group_names = [group.name for group in groups]
        if len(group_names) != len(set(group_names)):
            raise ValueError("通信组名必须唯一")
        self._groups: dict[str, Group] = {group.name: group for group in groups}

        if any(not isinstance(rank, int) or isinstance(rank, bool) for rank in programs):
            raise ValueError("programs 的键必须是 int 类型的 rank 编号")
        if len(programs) != len(set(programs)):
            raise ValueError("programs 中存在重复的 rank 键")

        self._programs: dict[int, Sequence[Instruction]] = {}
        for rank, program in programs.items():
            if isinstance(program, Program):
                if program.rank != rank:
                    raise ValueError(
                        f"Program 属于 rank {program.rank}, 却被登记到 rank {rank}"
                    )
                self._programs[rank] = program.instructions
            else:
                self._programs[rank] = tuple(program)

        # 静态校验：所有指令引用的组与句柄必须合法。
        for rank, instructions in self._programs.items():
            known_handles: set[Handle] = set()
            for instruction in instructions:
                if isinstance(instruction, Initiate):
                    if instruction.group not in self._groups:
                        raise ValueError(
                            f"rank {rank} 向未知通信组 {instruction.group!r} 发起操作"
                        )
                    if rank not in self._groups[instruction.group].member_set:
                        raise ValueError(
                            f"rank {rank} 不是通信组 {instruction.group!r} 的成员, "
                            "不能向该组发起操作"
                        )
                    if instruction.handle.rank != rank:
                        raise ValueError(
                            f"rank {rank} 的发起指令携带了属于 rank "
                            f"{instruction.handle.rank} 的句柄"
                        )
                    known_handles.add(instruction.handle)
                elif isinstance(instruction, Wait):
                    if instruction.handle not in known_handles:
                        raise ValueError(
                            f"rank {rank} 等待了一个并非由本序列此前发起的句柄: "
                            f"{instruction.handle}"
                        )
                else:  # pragma: no cover - 类型由上面的分支穷尽
                    raise ValueError(f"rank {rank} 存在不支持的指令类型: {instruction!r}")

        for group in self._groups.values():
            missing = sorted(set(group.members) - set(self._programs))
            if missing:
                raise ValueError(
                    f"通信组 {group.name!r} 的成员 {missing} 缺少对应的 Program"
                )

    @property
    def groups(self) -> dict[str, Group]:
        return dict(self._groups)

    def run(self) -> RunResult:
        """驱动全部 rank 直至全部结束，或抛出三类错误之一。"""
        ranks = sorted(self._programs)
        pcs: dict[int, int] = {rank: 0 for rank in ranks}
        status: dict[int, str] = {rank: _ACTIVE for rank in ranks}
        waiting: dict[int, Handle] = {}

        # 每个 rank 在每个组内的下一次发起序号（1 起算）。
        next_ordinal: dict[tuple[str, int], int] = {}
        # 配对位置 (group, ordinal) 的运行时状态。
        slot_tag: dict[tuple[str, int], object] = {}
        slot_arrived: dict[tuple[str, int], set[int]] = {}
        slot_waiters: dict[tuple[str, int], set[int]] = {}
        completed_slots: set[tuple[str, int]] = set()
        # 句柄 -> (group, ordinal)。
        handle_slot: dict[Handle, tuple[str, int]] = {}
        ops_by_rank: dict[int, list[OpRecord]] = {rank: [] for rank in ranks}
        events: list[Event] = []

        def complete_slot(key: tuple[str, int]) -> None:
            group_name, ordinal = key
            group = self._groups[group_name]
            completed_slots.add(key)
            tag = slot_tag[key]
            members = group.sorted_members
            events.append(
                SlotCompleted(
                    group=group_name, ordinal=ordinal, tag=tag, members=members
                )
            )
            for waiter_rank in sorted(slot_waiters.get(key, ())):
                if status.get(waiter_rank) == _BLOCKED and waiting.get(waiter_rank):
                    del waiting[waiter_rank]
                    status[waiter_rank] = _ACTIVE
                    events.append(
                        RankUnblocked(
                            rank=waiter_rank, group=group_name, ordinal=ordinal
                        )
                    )
            slot_waiters[key] = set()

        def submit(rank: int, instruction: Initiate) -> None:
            group_name = instruction.group
            group = self._groups[group_name]
            ordinal = next_ordinal.get((group_name, rank), 1)
            next_ordinal[(group_name, rank)] = ordinal + 1
            key = (group_name, ordinal)
            handle_slot[instruction.handle] = key

            if key in slot_tag:
                existing_tag = slot_tag[key]
                if existing_tag != instruction.tag:
                    raise TagMismatchError(
                        group=group_name,
                        ordinal=ordinal,
                        rank=rank,
                        tag=instruction.tag,
                        existing_tag=existing_tag,
                        members=group.sorted_members,
                    )
            else:
                slot_tag[key] = instruction.tag
            slot_arrived.setdefault(key, set()).add(rank)
            events.append(
                Initiated(
                    rank=rank, group=group_name, ordinal=ordinal, tag=instruction.tag
                )
            )

            if slot_arrived[key] == group.member_set:
                complete_slot(key)
                for member in group.sorted_members:
                    # 该位置上每个成员的发起句柄都已登记，按 rank 顺序补记录。
                    member_handle = _handle_of(handle_slot, group_name, ordinal, member)
                    ops_by_rank[member].append(
                        OpRecord(
                            handle=member_handle,
                            rank=member,
                            group=group_name,
                            ordinal=ordinal,
                            tag=instruction.tag,
                            members=group.sorted_members,
                        )
                    )

        def do_wait(rank: int, instruction: Wait) -> None:
            key = handle_slot[instruction.handle]
            group_name, ordinal = key
            if key in completed_slots:
                # 等待时操作已经完成，不产生阻塞事件。
                return
            status[rank] = _BLOCKED
            waiting[rank] = instruction.handle
            slot_waiters.setdefault(key, set()).add(rank)
            events.append(RankBlocked(rank=rank, group=group_name, ordinal=ordinal))

        while True:
            if all(status[rank] == _ENDED for rank in ranks):
                break

            progress = False
            for rank in ranks:
                if status[rank] != _ACTIVE:
                    continue
                instructions = self._programs[rank]
                pc = pcs[rank]
                if pc >= len(instructions):
                    status[rank] = _ENDED
                    events.append(RankEnded(rank=rank))
                    progress = True
                    continue

                instruction = instructions[pc]
                pcs[rank] = pc + 1
                progress = True
                if isinstance(instruction, Initiate):
                    submit(rank, instruction)
                else:
                    do_wait(rank, instruction)

            if not progress:
                self._handle_stall(
                    ranks=ranks,
                    status=status,
                    waiting=waiting,
                    handle_slot=handle_slot,
                    slot_arrived=slot_arrived,
                )

        return RunResult(
            ops_by_rank={
                rank: tuple(records) for rank, records in ops_by_rank.items()
            },
            events=tuple(events),
        )

    def _handle_stall(
        self,
        *,
        ranks: list[int],
        status: dict[int, str],
        waiting: dict[int, Handle],
        handle_slot: dict[Handle, tuple[str, int]],
        slot_arrived: dict[tuple[str, int], set[int]],
    ) -> None:
        """停滞时区分“成员已结束却缺席”与“闭合等待链死锁”。"""
        blocked_ranks = sorted(
            rank for rank in ranks if status[rank] == _BLOCKED and rank in waiting
        )
        # 理论上停滞时未结束的 rank 必然全部阻塞；防御性兜底。
        if not blocked_ranks:  # pragma: no cover
            raise DeadlockError(chain=())

        # 优先判定成员缺席：阻塞 rank 所等位置中，只要有成员已结束，
        # 该位置永远不可能收齐。
        for waiter in blocked_ranks:
            group_name, ordinal = handle_slot[waiting[waiter]]
            group = self._groups[group_name]
            arrived = slot_arrived.get((group_name, ordinal), set())
            missing = sorted(group.member_set - arrived)
            exited = [
                rank
                for rank in missing
                if status.get(rank) == _ENDED
            ]
            if exited:
                raise MemberExitedError(
                    waiter=waiter,
                    group=group_name,
                    ordinal=ordinal,
                    missing_rank=exited[0],
                    missing_members=tuple(missing),
                    members=group.sorted_members,
                )

        # 没有成员结束：所有未结束 rank 都在等待，构建等待图并找闭合环。
        edges: list[WaitEdge] = []
        for waiter in blocked_ranks:
            group_name, ordinal = handle_slot[waiting[waiter]]
            group = self._groups[group_name]
            arrived = slot_arrived.get((group_name, ordinal), set())
            missing = sorted(group.member_set - arrived)
            # 停滞且无成员结束时，每个等待位置至少有一个同样在阻塞的成员。
            target = next(rank for rank in missing if status[rank] == _BLOCKED)
            edges.append(
                WaitEdge(
                    waiter=waiter,
                    group=group_name,
                    ordinal=ordinal,
                    missing_rank=target,
                )
            )

        chain = _find_cycle(edges)
        raise DeadlockError(chain=chain)


def _handle_of(
    handle_slot: dict[Handle, tuple[str, int]],
    group_name: str,
    ordinal: int,
    member: int,
) -> Handle:
    """在配对收齐时取回某个成员在该位置的发起句柄。"""
    for handle, key in handle_slot.items():
        if key == (group_name, ordinal) and handle.rank == member:
            return handle
    raise KeyError(  # pragma: no cover - 收齐意味着句柄必然已登记
        f"组 {group_name!r} 第 {ordinal} 次发起缺少 rank {member} 的句柄"
    )


def _find_cycle(edges: list[WaitEdge]) -> tuple[WaitEdge, ...]:
    """在“每个节点恰好一条出边”的等待图上确定性地找出一个闭合环。

    从编号最小的阻塞 rank 出发沿出边行走，首次重复访问的节点即为环的
    起点；起点之后再次回到起点所经过的边构成闭合等待链。
    """
    outgoing = {edge.waiter: edge for edge in edges}
    start = min(outgoing)
    order: list[int] = []
    index: dict[int, int] = {}
    node = start
    while node not in index:
        index[node] = len(order)
        order.append(node)
        node = outgoing[node].missing_rank
    cycle_start = index[node]
    cycle_nodes = order[cycle_start:]
    return tuple(outgoing[cycle_node] for cycle_node in cycle_nodes)
