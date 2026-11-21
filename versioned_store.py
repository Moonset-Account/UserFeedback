"""Dynamo 风格的多版本键值存储。

核心能力：
- 多版本读取：同一对象的并发版本（siblings）并列返回。
- 带读取上下文的写入/删除：每次更新只替代调用者确实读过的版本；
  与上下文并发的版本作为兄弟保留。
- 删除以墓碑（tombstone）版本表示，参与向量时钟比较，防止已删除值复活。
- 两个（或多个）节点交换状态后按向量时钟支配关系合并，
  被支配的旧版本被剪除，并发版本并列保留。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


class VectorClock:
    """向量时钟：node_id -> 逻辑计数。"""

    def __init__(self, entries: Optional[Dict[str, int]] = None):
        self.entries: Dict[str, int] = dict(entries or {})

    def copy(self) -> "VectorClock":
        return VectorClock(self.entries)

    def increment(self, node: str) -> None:
        self.entries[node] = self.entries.get(node, 0) + 1

    def merge(self, other: "VectorClock") -> "VectorClock":
        for node, counter in other.entries.items():
            if counter > self.entries.get(node, 0):
                self.entries[node] = counter
        return self

    def merged(self, other: "VectorClock") -> "VectorClock":
        return self.copy().merge(other)

    def dominates_or_equal(self, other: "VectorClock") -> bool:
        """self 在因果上覆盖 other（other 的每个条目都不超过 self）。"""
        return all(
            counter <= self.entries.get(node, 0)
            for node, counter in other.entries.items()
        )

    def dominates(self, other: "VectorClock") -> bool:
        """严格支配：覆盖 other 且不被 other 覆盖（至少一个条目更大）。"""
        return self.dominates_or_equal(other) and not other.dominates_or_equal(self)

    def is_concurrent(self, other: "VectorClock") -> bool:
        return not self.dominates_or_equal(other) and not other.dominates_or_equal(self)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VectorClock) and self.entries == other.entries

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"VectorClock({self.entries!r})"


@dataclass
class Version:
    """对象的一个版本。deleted=True 表示墓碑（删除）。"""

    value: Any
    clock: VectorClock
    deleted: bool = False


class VersionedStore:
    """单节点的多版本存储，可与其他节点合并状态。"""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self._clock = VectorClock()
        self._store: Dict[str, List[Version]] = {}

    # ---------- 读取 ----------

    def get(self, key: str) -> Tuple[List[Any], VectorClock]:
        """返回 (存活值列表, 读取上下文)。并发版本并列返回。"""
        versions = self._store.get(key, [])
        context = VectorClock()
        for v in versions:
            context.merge(v.clock)
        values = [v.value for v in versions if not v.deleted]
        return values, context

    def has_concurrent_delete(self, key: str) -> bool:
        """识别并发删除：墓碑与某个存活版本互不支配。"""
        versions = self._store.get(key, [])
        tombstones = [v for v in versions if v.deleted]
        live = [v for v in versions if not v.deleted]
        return any(
            t.clock.is_concurrent(v.clock) for t in tombstones for v in live
        )

    def versions(self, key: str) -> List[Version]:
        """返回当前保留的全部版本（含墓碑），供测试与调试。"""
        return list(self._store.get(key, []))

    # ---------- 写入 / 删除 ----------

    def put(
        self, key: str, value: Any, context: Optional[VectorClock] = None
    ) -> VectorClock:
        """带读取上下文写入。只替代上下文覆盖的版本，并发版本保留为兄弟。"""
        return self._apply(key, value, context, deleted=False)

    def delete(self, key: str, context: Optional[VectorClock] = None) -> VectorClock:
        """带读取上下文删除，生成墓碑版本。语义与 put 一致。"""
        return self._apply(key, None, context, deleted=True)

    def _apply(
        self, key: str, value: Any, context: Optional[VectorClock], deleted: bool
    ) -> VectorClock:
        context = context.copy() if context is not None else VectorClock()
        existing = self._store.get(key, [])
        # 只保留上下文未覆盖的版本（并发或更新）；被覆盖版本被本次更新替代。
        survivors = [v for v in existing if not context.dominates_or_equal(v.clock)]

        self._clock.increment(self.node_id)
        new_clock = context.merged(
            VectorClock({self.node_id: self._clock.entries[self.node_id]})
        )
        survivors.append(Version(value=value, clock=new_clock, deleted=deleted))
        self._store[key] = self._prune(survivors)
        self._clock.merge(new_clock)
        return new_clock.copy()

    # ---------- 合并 ----------

    def merge_from(self, other: "VersionedStore") -> None:
        """把另一节点的状态合并进本节点（取版本并集后按支配关系剪枝）。"""
        for key, other_versions in other._store.items():
            combined = self._store.get(key, []) + [
                Version(v.value, v.clock.copy(), v.deleted) for v in other_versions
            ]
            self._store[key] = self._prune(combined)
        self._clock.merge(other._clock)

    def snapshot(self) -> Dict[str, List[Version]]:
        """导出当前状态副本，可用于构造“迟到的旧副本”。"""
        return {
            key: [Version(v.value, v.clock.copy(), v.deleted) for v in versions]
            for key, versions in self._store.items()
        }

    def merge_snapshot(self, snapshot: Dict[str, List[Version]]) -> None:
        """合并一份外部快照（例如某节点此前保存的旧状态）。"""
        for key, versions in snapshot.items():
            combined = self._store.get(key, []) + [
                Version(v.value, v.clock.copy(), v.deleted) for v in versions
            ]
            self._store[key] = self._prune(combined)

    # ---------- 内部 ----------

    @staticmethod
    def _prune(versions: List[Version]) -> List[Version]:
        """去掉被其他版本严格支配的版本，并对相同时钟去重。"""
        kept: List[Version] = []
        for v in versions:
            if any(u.clock.dominates(v.clock) for u in versions):
                continue
            if any(u.clock == v.clock and u.deleted == v.deleted for u in kept):
                continue
            kept.append(v)
        return kept
