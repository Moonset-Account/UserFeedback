"""收集与合并模块的核心实现。

语义概述
========

1. ``report_attempt`` 收集每个分片（``shard_id``）的各次尝试
   （``attempt``），每次尝试携带其覆盖的半开区间、归约好的复合变换
   以及成功标志。分片区间可以乱序到达，合并时再排序。

2. ``report_replacement`` 收集执行器确认的重试替换关系：一次重试尝试
   把旧区间拆成若干新片段，``segments`` 指向这些片段对应的（分片，
   尝试）。只有当引用的全部片段都已上报且成功时，这次替换才启用；
   片段已上报但失败的，替换不启用，此时旧分片自身的成功尝试可作为
   可用回退；片段尚未上报属于缺口，结果保持 waiting 继续等待。

3. ``merge`` 按原区间从前往后（区间 lo 升序）解析每个根分片实际生效
   的片段序列，再把这些片段的复合变换按同一顺序叠加成可执行的完整
   变换。区间有缺口（片段未到齐）时结果状态为 ``waiting`` 并列出缺口，
   不提前输出不完整的变换。

状态取值
--------
- ``ready``：整个合并区间连续覆盖，完整变换可直接执行。
- ``waiting``：存在尚未到齐的引用或区间缺口，继续等待缺失片段。
- ``failed``：结构完整但有分片（含回退尝试）无法产出成功结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .transforms import Transform, compose, identity


@dataclass(frozen=True)
class Interval:
    """半开区间 [lo, hi)。"""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise ValueError(f"区间端点必须为整数: {self!r}")
        if self.lo < 0 or self.hi < 0:
            raise ValueError(f"区间端点不能为负: {self!r}")
        if self.lo >= self.hi:
            raise ValueError(f"空区间或反向区间非法: {self!r}")

    @property
    def length(self) -> int:
        return self.hi - self.lo


@dataclass(frozen=True)
class ShardAttempt:
    """一个分片的一次尝试。

    Attributes:
        shard_id: 分片标识。
        attempt: 尝试编号，同一分片内编号越大越新。
        interval: 本次尝试覆盖的半开区间。
        transform: 该分片连续行归约出的复合变换。
        success: 本次尝试是否执行成功。
    """

    shard_id: str
    attempt: int
    interval: Interval
    transform: Transform
    success: bool


@dataclass(frozen=True)
class Replacement:
    """执行器确认的重试替换关系。

    旧分片（``shard_id`` 的 ``attempt`` 次尝试）的区间被拆成
    ``segments`` 中的若干新片段，片段按原区间从左到右排列，必须连续
    且无重叠地恰好铺满旧区间。片段以 (shard_id, attempt) 引用具体的
    尝试报告。
    """

    shard_id: str
    attempt: int
    segments: list[tuple[str, int]]


@dataclass
class MergeResult:
    """合并结果。

    Attributes:
        status: ready / waiting / failed。
        full_transform: 可执行的完整变换；非 ready 时为恒等变换占位。
        used_segments: 实际使用的片段序列，元素为 (shard_id, attempt)，
            顺序即原区间从前往后的应用顺序。
        missing: 尚在等待的区间缺口列表（已合并相邻缺口）。
        gap: 顶层覆盖中相邻区间之间的空隙（missing 的子集，单独暴露
            便于区分“引用未到齐”和“区间本身有洞”）。
        failures: 无法产出成功结果的 (shard_id, attempt) 列表。
    """

    status: str
    full_transform: Transform
    used_segments: list[tuple[str, int]] = field(default_factory=list)
    missing: list[Interval] = field(default_factory=list)
    gap: list[Interval] = field(default_factory=list)
    failures: list[tuple[str, int]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class Collector:
    """收集分片尝试与重试替换关系，并执行顺序合并。"""

    def __init__(self) -> None:
        # (shard_id, attempt) -> ShardAttempt
        self._attempts: dict[tuple[str, int], ShardAttempt] = {}
        # (shard_id, attempt) -> Replacement（执行器确认）
        self._replacements: dict[tuple[str, int], Replacement] = {}

    # ------------------------------------------------------------------
    # 收集侧
    # ------------------------------------------------------------------
    def report_attempt(self, attempt: ShardAttempt) -> None:
        """上报一次分片尝试。

        对同一 (shard_id, attempt) 的重复上报必须幂等：区间、成功标志
        与变换均一致时直接忽略；区间或成功标志冲突时抛 ValueError。
        乱序上报是允许的，合并阶段统一排序。
        """
        key = (attempt.shard_id, attempt.attempt)
        existing = self._attempts.get(key)
        if existing is not None:
            if existing.interval != attempt.interval:
                raise ValueError(
                    f"同一尝试 {key} 上报了冲突的区间: "
                    f"{existing.interval} != {attempt.interval}"
                )
            if existing.success != attempt.success:
                raise ValueError(
                    f"同一尝试 {key} 上报了冲突的成功标志: "
                    f"{existing.success} != {attempt.success}"
                )
            # 幂等重放：字段一致，忽略。
            return
        if attempt.attempt < 0:
            raise ValueError(f"尝试编号不能为负: {attempt.attempt}")
        self._attempts[key] = attempt
        # 该尝试可能是某个已登记替换关系的延迟到齐片段，补做结构校验。
        for replacement in self._replacements.values():
            if key not in replacement.segments:
                continue
            old = self._attempts[(replacement.shard_id, replacement.attempt)]
            if not (
                old.interval.lo <= attempt.interval.lo
                and attempt.interval.hi <= old.interval.hi
            ):
                raise ValueError(
                    f"替换关系 {(replacement.shard_id, replacement.attempt)} "
                    f"的片段 {key} 越出旧区间 {old.interval}: "
                    f"{attempt.interval}"
                )
            if all(
                ref in self._attempts for ref in replacement.segments
            ):
                self._validate_tiling(
                    (replacement.shard_id, replacement.attempt), replacement
                )

    def report_replacement(self, replacement: Replacement) -> None:
        """登记执行器确认的重试替换关系。

        前置条件：被替换的旧尝试必须已经上报（需要其区间校验拆段）。
        结构约束（旧区间拆段规则）：片段引用不得重复；已上报片段的
        区间必须落在旧区间内；当所有片段都已上报时，必须连续、无重叠
        且恰好铺满旧区间，否则抛 ValueError。片段尚未上报属于“等待
        缺片段”，登记时不报错，完整校验推迟到片段到齐后的合并阶段。
        """
        key = (replacement.shard_id, replacement.attempt)
        if key in self._replacements:
            raise ValueError(f"替换关系重复登记: {key}")
        old = self._attempts.get(key)
        if old is None:
            raise ValueError(
                f"替换关系 {key} 对应的旧尝试尚未上报，无法校验拆段结构"
            )
        if not replacement.segments:
            raise ValueError(f"替换关系 {key} 至少要包含一个新片段")
        if len(set(replacement.segments)) != len(replacement.segments):
            raise ValueError(f"替换关系 {key} 存在重复的片段引用")

        for ref in replacement.segments:
            seg = self._attempts.get(ref)
            if seg is None:
                continue
            if not (
                old.interval.lo <= seg.interval.lo
                and seg.interval.hi <= old.interval.hi
            ):
                raise ValueError(
                    f"替换关系 {key} 的片段 {ref} 越出旧区间 "
                    f"{old.interval}: {seg.interval}"
                )

        if all(ref in self._attempts for ref in replacement.segments):
            self._validate_tiling(key, replacement)

        self._replacements[key] = replacement

    def _validate_tiling(
        self, key: tuple[str, int], replacement: Replacement
    ) -> None:
        """校验片段连续、无重叠且恰好铺满旧尝试区间。"""
        old = self._attempts[key]
        cursor = old.interval.lo
        for ref in replacement.segments:
            seg = self._attempts[ref]
            if seg.interval.lo != cursor:
                raise ValueError(
                    f"替换关系 {key} 的片段 {ref} 未连续拼接: "
                    f"期望起点 {cursor}，实际 {seg.interval.lo}"
                )
            cursor = seg.interval.hi
        if cursor != old.interval.hi:
            raise ValueError(
                f"替换关系 {key} 的片段未铺满旧区间 {old.interval}，"
                f"实际末端 {cursor}"
            )

    def _detect_cycles(self) -> None:
        """对替换引用图做 DFS，存在环则抛 ValueError。"""
        graph: dict[tuple[str, int], list[tuple[str, int]]] = {
            key: list(replacement.segments)
            for key, replacement in self._replacements.items()
        }
        state: dict[tuple[str, int], int] = {}  # 0=未访问 1=在栈中 2=完成

        def visit(node: tuple[str, int], path: list[tuple[str, int]]) -> None:
            mark = state.get(node, 0)
            if mark == 1:
                cycle_start = path.index(node)
                raise ValueError(
                    "替换关系中存在循环引用: "
                    + " -> ".join(str(n) for n in path[cycle_start:] + [node])
                )
            if mark == 2:
                return
            state[node] = 1
            for neighbor in graph.get(node, []):
                visit(neighbor, path + [node])
            state[node] = 2

        for node in graph:
            if state.get(node, 0) == 0:
                visit(node, [])

    # ------------------------------------------------------------------
    # 合并侧
    # ------------------------------------------------------------------
    def merge(self) -> MergeResult:
        """按原区间顺序解析并合并所有已收集的分片。"""
        if not self._attempts:
            return MergeResult(
                status="waiting",
                full_transform=identity(),
            )

        # 所有片段到齐的替换关系在解析前做完整拆段校验。
        for key, replacement in self._replacements.items():
            if all(ref in self._attempts for ref in replacement.segments):
                self._validate_tiling(key, replacement)

        # 循环引用独立检测：互相引用的分片可能没有任何根节点，
        # 无法通过根分片遍历到达，必须显式扫描全部替换关系。
        self._detect_cycles()

        # 根分片：没有被任何已登记替换关系引用的尝试所属分片。
        referenced: set[tuple[str, int]] = set()
        for replacement in self._replacements.values():
            referenced.update(replacement.segments)
        root_shards = sorted(
            {key[0] for key in self._attempts if key not in referenced}
        )

        resolution = _Resolution(self._attempts, self._replacements)
        leaves: list[tuple[str, int]] = []
        missing: list[Interval] = []
        failures: list[tuple[str, int]] = []

        for shard_id in root_shards:
            attempts = sorted(
                (
                    attempt
                    for (sid, _no), attempt in self._attempts.items()
                    if sid == shard_id
                ),
                key=lambda attempt: attempt.attempt,
                reverse=True,
            )
            chosen, shard_missing, shard_failed = self._pick_root_attempt(
                attempts, resolution
            )
            missing.extend(shard_missing)
            failures.extend(shard_failed)
            if chosen is not None:
                leaves.extend(chosen)

        # 顶层区间缺口检测：叶子按区间 lo 排序后必须首尾相接。
        leaves.sort(key=lambda ref: self._attempts[ref].interval.lo)
        top_gaps: list[Interval] = []
        prev_hi: Optional[int] = None
        prev_ref: Optional[tuple[str, int]] = None
        for ref in leaves:
            interval = self._attempts[ref].interval
            if prev_hi is None:
                prev_hi = interval.hi
                prev_ref = ref
                continue
            if interval.lo < prev_hi:
                raise ValueError(
                    f"片段区间重叠: {prev_ref} 末端 {prev_hi} > "
                    f"{ref} 起点 {interval.lo}"
                )
            if interval.lo > prev_hi:
                top_gaps.append(Interval(prev_hi, interval.lo))
            prev_hi = interval.hi
            prev_ref = ref

        missing = _merge_adjacent(missing + top_gaps)

        if missing:
            status = "waiting"
        elif failures:
            status = "failed"
        else:
            status = "ready"

        if status == "ready":
            ordered = [self._attempts[ref] for ref in leaves]
            full_transform = compose([attempt.transform for attempt in ordered])
            used_segments = list(leaves)
        else:
            # 缺口未补齐或存在失败：不输出不完整的变换。
            full_transform = identity()
            used_segments = []

        return MergeResult(
            status=status,
            full_transform=full_transform,
            used_segments=used_segments,
            missing=missing,
            gap=top_gaps,
            failures=_dedupe(failures),
        )

    def _pick_root_attempt(
        self, attempts: list[ShardAttempt], resolution: "_Resolution"
    ) -> tuple[
        Optional[list[tuple[str, int]]],
        list[Interval],
        list[tuple[str, int]],
    ]:
        """选择根分片实际生效的尝试（最新优先，失败则回退旧尝试）。

        返回 (叶子引用或 None, 等待缺口, 失败引用)。
        """
        for candidate in attempts:
            key = (candidate.shard_id, candidate.attempt)
            refs, pending, miss, failed = resolution.resolve(key)

            if not pending and not miss and not failed:
                # 替换完整启用，或该尝试本身就是成功叶子。
                return refs, [], []

            if pending or miss:
                # 片段尚未到齐：继续等待缺失片段，不提前做永久回退。
                return None, miss, []

            # 片段已到齐但存在失败：替换不可启用，尝试回退。
            if candidate.success:
                return [key], [], []
            # 旧尝试也失败：继续尝试更旧的尝试。

        latest = attempts[0]
        return None, [], [(latest.shard_id, latest.attempt)]


class _Resolution:
    """替换引用的递归解析器（纯函数式、带记忆化缓存）。

    失败与缺口只通过 resolve 的返回值向上传播，不做全局累加：
    某一层替换因片段失败而不启用、但旧分片自身成功可回退时，该层
    返回的 failed 被清空，父层不会看到已经被回退吸收掉的失败。
    """

    def __init__(
        self,
        attempts: dict[tuple[str, int], ShardAttempt],
        replacements: dict[tuple[str, int], Replacement],
    ) -> None:
        self._attempts = attempts
        self._replacements = replacements
        self._cache: dict[
            tuple[str, int],
            tuple[list, list, list, list],
        ] = {}

    def resolve(
        self, key: tuple[str, int]
    ) -> tuple[list, list, list, list]:
        """解析一个尝试引用。

        返回 (叶子引用, 等待引用, 缺失区间, 失败引用)：
        - 存在已确认替换关系时递归解析全部片段，全部成功才启用替换；
        - 片段未到齐（pending）时结果保持等待，并依据已上报邻片推断
          精确的缺失区间；
        - 片段到齐但失败时替换不启用，旧尝试自身成功则回退为叶子并
          清除失败，旧尝试也失败则保留失败交由上层继续回退；
        - 无替换关系时该尝试自身作为叶子，失败则记入 failed。
        """
        if key in self._cache:
            return self._cache[key]

        pending: list[tuple[str, int]] = []
        missing: list[Interval] = []
        failed: list[tuple[str, int]] = []
        leaves: list[tuple[str, int]] = []

        replacement = self._replacements.get(key)
        if replacement is None:
            attempt = self._attempts.get(key)
            if attempt is None:
                pending.append(key)
            elif attempt.success:
                leaves.append(key)
            else:
                failed.append(key)
        else:
            for ref in replacement.segments:
                child_leaves, child_pending, child_missing, child_failed = (
                    self.resolve(ref)
                )
                leaves.extend(child_leaves)
                pending.extend(child_pending)
                missing.extend(child_missing)
                failed.extend(child_failed)

            if pending or missing or failed:
                # 新片段未全部成功：替换不启用。
                old = self._attempts.get(key)
                if pending or missing:
                    # 缺口：依据已上报邻片推断精确的等待区间。
                    if old is not None:
                        missing = _merge_adjacent(
                            missing
                            + _infer_missing(
                                old.interval,
                                replacement.segments,
                                self._attempts,
                            )
                        )
                    leaves = []
                elif old is not None and old.success:
                    # 片段到齐但失败：旧分片成功，作为可用回退，
                    # 子层失败被本次回退吸收，不再向上传播。
                    leaves = [key]
                    failed = []
                else:
                    leaves = []

        result = (leaves, pending, missing, failed)
        self._cache[key] = result
        return result


def _infer_missing(
    old_interval: Interval,
    segments: list[tuple[str, int]],
    attempts: dict[tuple[str, int], ShardAttempt],
) -> list[Interval]:
    """根据已上报的邻片推断未上报片段占据的精确缺口区间。

    片段顺序即区间顺序：未上报片段位于两个已上报片段之间时，缺口为
    (前片末端, 后片起点)；位于首尾时用旧区间端点补界。
    """
    gaps: list[Interval] = []
    cursor = old_interval.lo
    for ref in segments:
        seg = attempts.get(ref)
        if seg is None:
            continue
        if seg.interval.lo > cursor:
            gaps.append(Interval(cursor, seg.interval.lo))
        cursor = seg.interval.hi
    if cursor < old_interval.hi:
        gaps.append(Interval(cursor, old_interval.hi))
    return gaps


def _merge_adjacent(intervals: list[Interval]) -> list[Interval]:
    """合并相交或首尾相接的区间，返回去重后的有序缺口列表。"""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv.lo, iv.hi))
    merged: list[Interval] = []
    for interval in ordered:
        if merged and interval.lo <= merged[-1].hi:
            last = merged[-1]
            if interval.hi > last.hi:
                merged[-1] = Interval(last.lo, interval.hi)
        else:
            merged.append(interval)
    return merged


def _dedupe(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    result: list[tuple[str, int]] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
