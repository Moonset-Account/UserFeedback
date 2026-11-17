"""非 root 主体的五集合能力状态。

集合含义与 capabilities(7) 一致：
    P (permitted)、E (effective)、I (inheritable)、A (ambient)、B (bounding)。
状态为不可变值对象，可哈希、可作为 BFS 的去重键。
"""
from __future__ import annotations

from collections.abc import Iterable

from .caps import ALL_MASK, to_mask, to_names


class CapState:
    """以位掩码保存 P/E/I/A/B 五个集合的不可变状态。"""

    __slots__ = ("p", "e", "i", "a", "b")

    def __init__(
        self,
        permitted: Iterable[str] | int = 0,
        effective: Iterable[str] | int = 0,
        inheritable: Iterable[str] | int = 0,
        ambient: Iterable[str] | int = 0,
        bounding: Iterable[str] | int = ALL_MASK,
    ) -> None:
        p = to_mask(permitted)
        e = to_mask(effective)
        i = to_mask(inheritable)
        a = to_mask(ambient)
        b = to_mask(bounding)
        # 放松的结构校验：A 必须同时在 P/I 中（cap_ambient 的内核前提），
        # 其余超集关系（P⊆B、E⊆P、I⊆B）由调用方/转换维持，不在构造时强制，
        # 以便推演过程中能观察到中间形态。
        if a & ~(p & i):
            raise ValueError("ambient 中的能力必须同时位于 permitted 与 inheritable")
        object.__setattr__(self, "p", p)
        object.__setattr__(self, "e", e)
        object.__setattr__(self, "i", i)
        object.__setattr__(self, "a", a)
        object.__setattr__(self, "b", b)

    # --- 派生视图 -----------------------------------------------------------
    def sets(self) -> dict[str, int]:
        return {"P": self.p, "E": self.e, "I": self.i, "A": self.a, "B": self.b}

    def live_mask(self) -> int:
        """进程实际持有的能力：P ∪ E ∪ A（I/B 为约束集合，不计入）。"""
        return self.p | self.e | self.a

    def names(self) -> dict[str, list[str]]:
        return {key: to_names(mask) for key, mask in self.sets().items()}

    # --- 不可变/值语义 --------------------------------------------------------
    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("CapState 不可变；请经 transitions 构造新状态")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CapState):
            return NotImplemented
        return (self.p, self.e, self.i, self.a, self.b) == (
            other.p,
            other.e,
            other.i,
            other.a,
            other.b,
        )

    def __hash__(self) -> int:
        return hash((self.p, self.e, self.i, self.a, self.b))

    def __repr__(self) -> str:
        n = self.names()
        return (
            "CapState("
            f"P={n['P']}, E={n['E']}, I={n['I']}, A={n['A']}, B={n['B']})"
        )

    def to_dict(self) -> dict[str, list[str]]:
        """JSON 友好的能力名表示。"""
        return self.names()
