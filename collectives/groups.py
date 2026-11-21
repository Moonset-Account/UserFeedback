"""通信组定义。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    """一个集合通信组。

    ``members`` 为 rank 编号序列；顺序仅用于错误信息的稳定展示，配对
    语义与顺序无关。两个成员名单完全相同的组仍是不同的组，各自维护
    独立的发起配对次序，因此组名 ``name`` 必须在一个执行器内唯一。
    """

    name: str
    members: tuple[int, ...]

    def __init__(self, name: str, members: tuple[int, ...] | list[int]) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("通信组名必须是非空字符串")
        members = tuple(members)
        if not members:
            raise ValueError(f"通信组 {name!r} 至少需要一个成员")
        if len(set(members)) != len(members):
            raise ValueError(f"通信组 {name!r} 的成员名单存在重复 rank: {list(members)}")
        if any(not isinstance(rank, int) or isinstance(rank, bool) for rank in members):
            raise ValueError(f"通信组 {name!r} 的成员必须是 int 类型的 rank 编号")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "members", members)

    @property
    def member_set(self) -> frozenset[int]:
        return frozenset(self.members)

    @property
    def sorted_members(self) -> tuple[int, ...]:
        """按 rank 排序的成员名单，用于稳定的错误信息与内部排序。"""
        return tuple(sorted(self.members))
