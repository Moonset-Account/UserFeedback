"""匹配算法：预计算、输入校验与记忆化回溯。

在题面给定的小样本规模（最多 4 卷、每卷 8 枚）下，枚举 **全部** 合法分配，
再汇总每箱在所有合法分配中可能出现的（卷, 位置）。不做相似度挑选。
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from .model import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    MAX_ROLLS,
    MAX_SEALS_PER_ROLL,
    STATUS_CONFLICT,
    STATUS_NO_CANDIDATE,
    STATUS_OK,
    _DIRECTIONS,
    BoxRecord,
    BoxResult,
    MatchResult,
    Roll,
    SealRef,
)


class _RollState:
    """一卷的预计算状态。"""

    def __init__(self, roll: Roll) -> None:
        self.roll = roll
        n = len(roll.seals)
        # 使用顺序下的物理索引序列：usage_order[k] = 第 k+1 枚使用封条的物理索引。
        if roll.direction == DIRECTION_FORWARD:
            usage_order = tuple(range(n))
        else:
            usage_order = tuple(range(n - 1, -1, -1))
        self.usage_order = usage_order

        # 物理索引 -> 1 起使用位置
        self._usage_pos = [0] * n
        for usage_idx, physical_idx in enumerate(usage_order, start=1):
            self._usage_pos[physical_idx] = usage_idx

        # 作废的物理索引集合；数字号段按数值比较，其余按字符串比较。
        void_phys: set[int] = set()
        for lo, hi in roll.void_ranges:
            if str(lo).isdigit() and str(hi).isdigit():
                lo_v, hi_v = int(lo), int(hi)
                for physical_idx, num in enumerate(roll.seals):
                    if str(num).isdigit() and lo_v <= int(num) <= hi_v:
                        void_phys.add(physical_idx)
            else:
                for physical_idx, num in enumerate(roll.seals):
                    if lo <= num <= hi:
                        void_phys.add(physical_idx)
        self.void_phys = frozenset(void_phys)

        # 使用顺序上的非作废封条（可被箱消耗的封条），元素为物理索引。
        self.usable_usage: tuple[int, ...] = tuple(
            physical_idx
            for physical_idx in usage_order
            if physical_idx not in void_phys
        )

    def ref_for_usage_slot(self, usage_slot: int) -> SealRef:
        """usable_usage 的第 ``usage_slot`` 个（0 起）非作废封条引用。"""
        physical_idx = self.usable_usage[usage_slot]
        return SealRef(
            roll_id=self.roll.roll_id,
            usage_position=self._usage_pos[physical_idx],
            physical_position=physical_idx + 1,
            seal_number=self.roll.seals[physical_idx],
        )


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """把抄录局部号码编译为整串匹配的正则。

    只允许数字、``?``（单个数字）和 ``*``（任意长度数字串，含空串）。
    """

    if not isinstance(pattern, str) or pattern == "":
        raise ValueError("partial_number 不能为空串；未拍照箱请用 None")
    out: list[str] = []
    for ch in pattern:
        if ch == "?":
            out.append(r"\d")
        elif ch == "*":
            out.append(r"\d*")
        elif ch.isdigit():
            out.append(re.escape(ch))
        else:
            raise ValueError(
                f"局部号码 {pattern!r} 含非法字符 {ch!r}：只允许数字、? 和 *"
            )
    return re.compile("^" + "".join(out) + "$")


def _validate(rolls: Sequence[Roll], boxes: Sequence[BoxRecord]) -> None:
    if not isinstance(rolls, Sequence) or not isinstance(boxes, Sequence):
        raise TypeError("rolls 和 boxes 必须是序列")
    if len(rolls) == 0:
        raise ValueError("至少需要一卷封条")
    if len(rolls) > MAX_ROLLS:
        raise ValueError(f"样本上限为 {MAX_ROLLS} 卷，收到 {len(rolls)} 卷")

    seen_roll_ids: set[str] = set()
    for roll in rolls:
        if not isinstance(roll, Roll):
            raise TypeError("rolls 的元素必须是 Roll 实例")
        if roll.roll_id in seen_roll_ids:
            raise ValueError(f"卷号重复：{roll.roll_id!r}")
        seen_roll_ids.add(roll.roll_id)
        if len(roll.seals) == 0:
            raise ValueError(f"卷 {roll.roll_id!r} 没有封条")
        if len(roll.seals) > MAX_SEALS_PER_ROLL:
            raise ValueError(
                f"卷 {roll.roll_id!r} 有 {len(roll.seals)} 枚封条，"
                f"超过每卷 {MAX_SEALS_PER_ROLL} 枚的上限"
            )
        if roll.direction not in _DIRECTIONS:
            raise ValueError(f"卷 {roll.roll_id!r} 开卷方向非法：{roll.direction!r}")
        if len(set(roll.seals)) != len(roll.seals):
            raise ValueError(f"卷 {roll.roll_id!r} 内部封条号码重复")
        if any(not str(num).isdigit() for num in roll.seals):
            raise ValueError(f"卷 {roll.roll_id!r} 的封条号码必须全部为数字")
        for rng in roll.void_ranges:
            if (
                not isinstance(rng, tuple)
                or len(rng) != 2
                or not all(isinstance(x, str) for x in rng)
            ):
                raise ValueError(
                    f"卷 {roll.roll_id!r} 的作废号段必须是 (起号, 止号) 字符串对"
                )
            lo, hi = rng
            if lo not in roll.seals or hi not in roll.seals:
                raise ValueError(
                    f"卷 {roll.roll_id!r} 的作废号段 ({lo!r}, {hi!r}) "
                    "端点不是本卷号码"
                )

    seen_box_ids: set[str] = set()
    for b in boxes:
        if not isinstance(b, BoxRecord):
            raise TypeError("boxes 的元素必须是 BoxRecord 实例")
        if b.box_id == "":
            raise ValueError("box_id 不能为空")
        if b.box_id in seen_box_ids:
            raise ValueError(f"箱号重复：{b.box_id!r}")
        seen_box_ids.add(b.box_id)
        if b.partial_number is not None:
            compile_pattern(b.partial_number)  # 提前校验非法字符


def match_seals(rolls: Sequence[Roll], boxes: Sequence[BoxRecord]) -> MatchResult:
    """核对领出封条卷与封箱记录，返回每箱在全部合法分配中的可能归属。

    合法分配的判定：

    1. 每个箱消耗恰好一枚非作废封条，单枚封条不跨箱复用；
    2. 同卷各箱消耗的封条必须严格遵循开卷使用顺序——某卷已被 ``k`` 个箱
       占用时，下一个使用该卷的箱只能拿该卷第 ``k+1`` 枚非作废封条。
       中间的非作废封条不得跳过，必须已被更早的箱（含未拍照箱）消耗；
       作废封条由作废号段解释，自动跳过；
    3. 拍照箱的抄录号码（支持 ``?`` / ``*``）须与封条号码整串匹配，
       未拍照箱（``partial_number=None``）可消耗任意非作废封条；
    4. 卷尾未使用的封条不需要解释。

    :param rolls: 领出的封条卷（含开卷方向与作废号段）。
    :param boxes: 按封箱先后排列的登记记录。
    :returns: :class:`MatchResult`，含整体状态与逐箱候选。
    :raises ValueError: 输入违反样本上限或结构约束。
    """

    _validate(rolls, boxes)
    states = [_RollState(roll) for roll in rolls]
    r_count = len(rolls)
    b_count = len(boxes)

    patterns: list[Optional[re.Pattern[str]]] = [
        None if b.partial_number is None else compile_pattern(b.partial_number)
        for b in boxes
    ]

    # candidates[b] = [(卷索引, 非作废使用槽位 0 起), ...]，仅按号码匹配与
    # “未作废”筛选；顺序/唯一性约束在回溯中统一判定，不做局部挑选。
    candidates: list[list[tuple[int, int]]] = []
    for b in range(b_count):
        opts: list[tuple[int, int]] = []
        for r, state in enumerate(states):
            for slot, physical_idx in enumerate(state.usable_usage):
                number = state.roll.seals[physical_idx]
                if patterns[b] is None or patterns[b].match(number):
                    opts.append((r, slot))
        candidates.append(opts)

    # 记忆化回溯：ways(b, used) = 从第 b 个箱起、各卷已占用 used[r] 枚
    # 非作废封条时，剩余箱的合法分配数。
    ways_cache: dict[tuple[int, tuple[int, ...]], int] = {}

    def ways(b: int, used: tuple[int, ...]) -> int:
        if b == b_count:
            return 1
        key = (b, used)
        cached = ways_cache.get(key)
        if cached is not None:
            return cached
        total = 0
        for r, slot in candidates[b]:
            # 顺序约束：本卷下一枚可用非作废封条的槽位必须恰为 used[r]。
            if slot == used[r]:
                next_used = list(used)
                next_used[r] += 1
                total += ways(b + 1, tuple(next_used))
        ways_cache[key] = total
        return total

    total_ways = ways(0, tuple([0] * r_count))

    # 局部候选：只看号码匹配与“未作废”，不看顺序/唯一性，用于区分
    # no_candidate（某箱连局部候选都没有）与 conflict（都有候选但无法同时分配）。
    local_refs: list[list[SealRef]] = []
    for b in range(b_count):
        refs = [
            states[r].ref_for_usage_slot(slot) for r, slot in candidates[b]
        ]
        refs.sort(key=lambda x: (x.roll_id, x.usage_position))
        local_refs.append(refs)
    any_without_candidate = any(not opts for opts in candidates)

    # 依据缓存重建每箱在“可扩展为完整合法分配”的前缀中出现过的归属。
    per_box_refs: list[list[SealRef]] = [[] for _ in range(b_count)]
    per_box_seen: list[set[tuple[str, int]]] = [set() for _ in range(b_count)]

    def collect(b: int, used: tuple[int, ...]) -> None:
        if b == b_count:
            return
        for r, slot in candidates[b]:
            if slot != used[r]:
                continue
            next_used = list(used)
            next_used[r] += 1
            next_used_t = tuple(next_used)
            if ways(b + 1, next_used_t) > 0:
                ref = states[r].ref_for_usage_slot(slot)
                dedup_key = (ref.roll_id, ref.usage_position)
                if dedup_key not in per_box_seen[b]:
                    per_box_seen[b].add(dedup_key)
                    per_box_refs[b].append(ref)
                collect(b + 1, next_used_t)

    if total_ways > 0:
        collect(0, tuple([0] * r_count))

    results: list[BoxResult] = []
    for b, box in enumerate(boxes):
        if total_ways > 0:
            # ok：只保留能出现在某套完整合法分配中的归属。
            refs = tuple(
                sorted(per_box_refs[b], key=lambda x: (x.roll_id, x.usage_position))
            )
        else:
            # conflict / no_candidate：逐箱如实列出局部候选（号码匹配且未作废），
            # 无候选的箱自然为空；由整体 status 标明无法组成完整分配。
            refs = tuple(local_refs[b])
        results.append(
            BoxResult(box_id=box.box_id, possibilities=refs, determined=len(refs) == 1)
        )

    if total_ways > 0:
        status = STATUS_OK
    elif any_without_candidate:
        status = STATUS_NO_CANDIDATE
    else:
        status = STATUS_CONFLICT

    return MatchResult(
        status=status, boxes=tuple(results), allocation_count=total_ways
    )
