"""索引迁移模块。

把客户名索引从旧的精确匹配规则切换到新的排序规则（Collation）：

- 旧索引（OldIndex）在后台扫描期间继续服务，允许插入和改名；
  扫描完成之后发生的改动会记入增量日志（delta log）。
- 切换点（commit）把扫描结果和扫描之后的改动放进同一次唯一性判断：
  两个名字的新排序键完全相等（所有级别都相等）即冲突。
- 有冲突时迁移中止，旧索引保持可查；无冲突时切换到新索引。
"""

from __future__ import annotations

from typing import Any, Optional

from collation import Collation, SortKey


class OldIndex:
    """旧索引：按精确字符串匹配保证唯一性。"""

    def __init__(self):
        self._rows: dict[str, Any] = {}

    def insert(self, name: str, row: Any) -> None:
        if name in self._rows:
            raise KeyError(f"duplicate name: {name!r}")
        self._rows[name] = row

    def rename(self, old_name: str, new_name: str) -> None:
        if old_name not in self._rows:
            raise KeyError(f"unknown name: {old_name!r}")
        if new_name in self._rows and new_name != old_name:
            raise KeyError(f"duplicate name: {new_name!r}")
        self._rows[new_name] = self._rows.pop(old_name)

    def lookup(self, name: str) -> Any:
        return self._rows.get(name)

    def names(self) -> list[str]:
        return list(self._rows)

    def __contains__(self, name: str) -> bool:
        return name in self._rows

    def __len__(self) -> int:
        return len(self._rows)


class NewIndex:
    """新索引：按新排序规则组织的唯一性索引。"""

    def __init__(self, rows: dict[str, Any], name_to_key: dict[str, SortKey]):
        self._rows = dict(rows)
        self._name_to_key = dict(name_to_key)
        self._key_to_name = {key: name for name, key in name_to_key.items()}

    def lookup(self, name: str) -> Any:
        return self._rows.get(name)

    def sort_key_of(self, name: str) -> Optional[SortKey]:
        return self._name_to_key.get(name)

    def names(self) -> list[str]:
        return list(self._rows)

    def sorted_names(self) -> list[str]:
        """按新排序规则排序的全部名字。"""
        return sorted(self._name_to_key, key=lambda n: self._name_to_key[n])

    def __contains__(self, name: str) -> bool:
        return name in self._rows

    def __len__(self) -> int:
        return len(self._rows)


class Migration:
    """一次索引迁移。

    用法：
        mig = Migration(old_index, collation, chunk_size=1)
        mig.begin_scan()            # 后台扫描当前全部名字
        mig.insert("nuevo", row)    # 扫描后仍允许写入，记入增量日志
        mig.rename("viejo", "nuevo2")
        ok = mig.commit()           # 切换点：扫描 + 增量一起做唯一性判断
    """

    IDLE = "idle"
    SCANNING = "scanning"
    SWITCHED = "switched"
    ABORTED = "aborted"

    def __init__(
        self,
        old_index: OldIndex,
        collation: Collation,
        chunk_size: Optional[int] = None,
    ):
        if chunk_size is not None and chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._old = old_index
        self._collation = collation
        self._chunk_size = chunk_size
        self._scanned: dict[str, SortKey] = {}
        self._delta: list[tuple] = []
        self._state = self.IDLE
        self._new_index: Optional[NewIndex] = None
        self.conflicts: list[tuple[str, str]] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_index(self):
        """当前应对外提供查询的索引：切换成功后是新索引，否则是旧索引。"""
        return self._new_index if self._state == self.SWITCHED else self._old

    def _key_of(self, name: str) -> SortKey:
        if self._chunk_size is None:
            return self._collation.sort_key(name)
        # 模拟分块输入：名字被切成若干块喂给构建器，
        # 收缩序列可能跨过块边界。
        builder = self._collation.key_builder()
        for i in range(0, len(name), self._chunk_size):
            builder.feed(name[i : i + self._chunk_size])
        return builder.finish()

    # ---- 后台扫描 -------------------------------------------------

    def begin_scan(self) -> None:
        """对旧索引当前内容做后台扫描，计算每个名字的新排序键。"""
        if self._state != self.IDLE:
            raise RuntimeError(f"cannot scan in state {self._state}")
        for name in self._old.names():
            self._scanned[name] = self._key_of(name)
        self._state = self.SCANNING

    # ---- 扫描期间的并发写入（始终先落到旧索引） --------------------

    def insert(self, name: str, row: Any) -> None:
        self._old.insert(name, row)
        if self._state == self.SCANNING:
            self._delta.append(("insert", name))

    def rename(self, old_name: str, new_name: str) -> None:
        self._old.rename(old_name, new_name)
        if self._state == self.SCANNING:
            self._delta.append(("rename", old_name, new_name))

    # ---- 切换点 ---------------------------------------------------

    def commit(self) -> bool:
        """把扫描结果与扫描后的改动合并做唯一性判断。

        返回 True 表示切换到新索引；返回 False 表示发现冲突、迁移中止，
        旧索引保持可查（active_index 仍是旧索引）。
        """
        if self._state != self.SCANNING:
            raise RuntimeError(f"cannot commit in state {self._state}")

        key_to_name: dict[SortKey, str] = {}
        name_to_key: dict[str, SortKey] = {}
        conflicts: list[tuple[str, str]] = []

        def add(name: str, key: SortKey) -> None:
            other = key_to_name.get(key)
            if other is not None and other != name:
                conflicts.append((other, name))
            else:
                key_to_name[key] = name
                name_to_key[name] = key

        # 1) 扫描时已有的名字。
        for name, key in self._scanned.items():
            add(name, key)

        # 2) 扫描之后的改动，纳入同一次唯一性判断。
        for op in self._delta:
            if op[0] == "insert":
                add(op[1], self._key_of(op[1]))
            else:  # rename
                _, old_name, new_name = op
                old_key = name_to_key.pop(old_name, None)
                if old_key is not None and key_to_name.get(old_key) == old_name:
                    del key_to_name[old_key]
                add(new_name, self._key_of(new_name))

        if conflicts:
            self.conflicts = conflicts
            self._state = self.ABORTED
            return False

        self._new_index = NewIndex(dict(self._old._rows), name_to_key)
        self._state = self.SWITCHED
        return True
