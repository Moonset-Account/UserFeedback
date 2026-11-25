"""字符宽度表。

宽度约定：

* ``1`` 普通单宽字，占据一格；
* ``2`` 双宽字，占据相邻两格；
* ``0`` 组合标记（combining mark），本身不占格，附着到当前基字；
* 未登记字符的宽度由 :attr:`WidthTable.default_width` 决定（默认为 1）。

题面要求“字符宽度表随样本给出”，因此本表不内置任何 Unicode 范围猜测，
所有非默认宽度都必须显式登记。
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Union

WidthInput = Union[Mapping[str, int], Iterable[tuple[str, int]]]


class WidthTable:
    """码点 -> 显示宽度 的显式映射表。"""

    def __init__(
        self,
        widths: WidthInput | None = None,
        default_width: int = 1,
    ) -> None:
        if default_width not in (0, 1, 2):
            raise ValueError("default_width 只能是 0、1 或 2")
        self._widths: Dict[str, int] = {}
        if widths is not None:
            items = widths.items() if isinstance(widths, Mapping) else widths
            for char, width in items:
                if len(char) != 1:
                    raise ValueError(f"宽度表的键必须是单个字符: {char!r}")
                if width not in (0, 1, 2):
                    raise ValueError(f"宽度只能是 0、1 或 2: {char!r} -> {width}")
                self._widths[char] = width
        self.default_width = default_width

    def width_of(self, char: str) -> int:
        """返回单个字符的显示宽度。"""
        if len(char) != 1:
            raise ValueError("width_of 只接受单个字符")
        return self._widths.get(char, self.default_width)

    def is_combining(self, char: str) -> bool:
        """该字符是否登记为零宽组合标记。"""
        return self.width_of(char) == 0

    def is_wide(self, char: str) -> bool:
        """该字符是否登记为双宽字。"""
        return self.width_of(char) == 2

    def __contains__(self, char: str) -> bool:
        return char in self._widths

    def __repr__(self) -> str:
        return (
            f"WidthTable({self._widths!r}, default_width={self.default_width})"
        )
