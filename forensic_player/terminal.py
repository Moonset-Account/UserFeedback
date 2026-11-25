"""终端屏幕模型与动作回放。

屏幕由固定行列的单元格 :class:`Cell` 组成，每个单元格记录：

* 当前状态（从未写入 / 有内容 / 已被擦成空白）；
* 基字与附着其上的组合标记序列（组合标记不占格）；
* 最后一次改写本格的录制流字节来源区间；
* 若本格因擦除/覆盖变空，记录使其变空的那次动作的字节区间。

双宽字占据相邻两格，左格存实际字符、右格标记为 continuation。
改写双宽字覆盖的任意一格时，整个旧双宽字的两格都被清掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence

from .stream_parser import Action, ActionType
from .width import WidthTable


class CellState(Enum):
    VIRGIN = "virgin"      # 从未被任何动作触及
    CONTENT = "content"    # 持有基字（含组合标记），或为双宽字右半
    BLANK = "blank"        # 曾被写入/擦除，当前为空白


@dataclass
class CombiningMark:
    """附着在基字上的组合标记及其字节来源。"""

    char: str
    start: int
    end: int


@dataclass
class Cell:
    """可变的屏幕单元格。"""

    state: CellState = CellState.VIRGIN
    base: Optional[str] = None
    marks: List[CombiningMark] = field(default_factory=list)
    is_continuation: bool = False  # 双宽字的右半格
    last_write: Optional[tuple[int, int]] = None
    clear_range: Optional[tuple[int, int]] = None

    def clone(self) -> "Cell":
        return Cell(
            state=self.state,
            base=self.base,
            marks=[CombiningMark(m.char, m.start, m.end) for m in self.marks],
            is_continuation=self.is_continuation,
            last_write=self.last_write,
            clear_range=self.clear_range,
        )


@dataclass(frozen=True)
class CellInfo:
    """查询时刻某一单元格的只读取证视图。"""

    row: int
    col: int
    state: CellState
    base: Optional[str]
    marks: Sequence[tuple[str, int, int]]
    is_continuation: bool
    last_write_range: Optional[tuple[int, int]]
    clear_range: Optional[tuple[int, int]]

    @property
    def text(self) -> str:
        """该格的可打印文本（基字 + 组合标记；右半格与空白格为空串）。"""
        if self.is_continuation or self.base is None:
            return ""
        return self.base + "".join(char for char, _, _ in self.marks)

    @property
    def is_erased(self) -> bool:
        """是否处于“实际擦除造成的空白”状态。"""
        return self.state == CellState.BLANK

    @property
    def ever_written(self) -> bool:
        """是否曾被任何动作触及。"""
        return self.state != CellState.VIRGIN


class Terminal:
    """固定尺寸、带有限行数上卷的终端屏幕。"""

    def __init__(self, rows: int, cols: int, width_table: WidthTable) -> None:
        if rows <= 0 or cols <= 0:
            raise ValueError("rows 和 cols 必须为正整数")
        self.rows = rows
        self.cols = cols
        self.width_table = width_table
        self._grid: List[List[Cell]] = [
            [Cell() for _ in range(cols)] for _ in range(rows)
        ]
        self.cursor_row = 0
        self.cursor_col = 0

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def cell_info(self, row: int, col: int) -> CellInfo:
        """取 0 基坐标单元格的只读快照。"""
        self._check_bounds(row, col)
        cell = self._grid[row][col]
        return CellInfo(
            row=row,
            col=col,
            state=cell.state,
            base=cell.base,
            marks=tuple((m.char, m.start, m.end) for m in cell.marks),
            is_continuation=cell.is_continuation,
            last_write_range=cell.last_write,
            clear_range=cell.clear_range,
        )

    def render_line(self, row: int) -> str:
        """渲染一行为纯文本（不含换行符）。"""
        parts: list[str] = []
        for cell in self._grid[row]:
            if cell.state == CellState.CONTENT and not cell.is_continuation:
                parts.append(cell.base or "")
                parts.extend(m.char for m in cell.marks)
            else:
                parts.append(" ")
        return "".join(parts)

    def render_screen(self) -> str:
        """渲染整屏，行之间用 ``\\n`` 连接。"""
        return "\n".join(self.render_line(r) for r in range(self.rows))

    def snapshot_cells(self) -> List[List[CellInfo]]:
        """整屏单元格的只读快照矩阵。"""
        return [
            [self.cell_info(r, c) for c in range(self.cols)]
            for r in range(self.rows)
        ]

    # ------------------------------------------------------------------
    # 动作回放
    # ------------------------------------------------------------------
    def apply(self, action: Action) -> None:
        if action.type is ActionType.PRINT:
            self._print(action)
        elif action.type is ActionType.BACKSPACE:
            if self.cursor_col > 0:
                self.cursor_col -= 1
        elif action.type is ActionType.CARRIAGE_RETURN:
            self.cursor_col = 0
        elif action.type is ActionType.LINE_FEED:
            self._line_feed()
        elif action.type is ActionType.CUP:
            self._cup(action)
        elif action.type is ActionType.EL:
            self._erase_line(action)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _check_bounds(self, row: int, col: int) -> None:
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError(
                f"坐标越界: ({row}, {col})，屏幕为 {self.rows}x{self.cols}"
            )

    def _combining_target_col(self, r: int, c: int) -> int:
        """组合标记应附着的列：光标左侧最近的基字格。"""
        if c == 0:
            return 0
        left = self._grid[r][c - 1]
        # 光标左侧是双宽字的右半：基字在再往左一格。
        if left.is_continuation and c >= 2:
            return c - 2
        # 左侧持有基字（普通字或双宽字左半）。
        if left.base is not None:
            return c - 1
        # 左侧没有基字：附着到当前格（孤立标记）。
        return c

    def _line_feed(self) -> None:
        if self.cursor_row == self.rows - 1:
            # 上卷：丢弃顶行，底部补一行从未写入的新行。
            self._grid.pop(0)
            self._grid.append([Cell() for _ in range(self.cols)])
        else:
            self.cursor_row += 1
        # LF 只移动行，列保持不变（常见终端行为）。

    def _cup(self, action: Action) -> None:
        assert action.row is not None and action.col is not None
        # CUP 参数为 1 基，钳制到屏幕范围。
        self.cursor_row = min(max(action.row - 1, 0), self.rows - 1)
        self.cursor_col = min(max(action.col - 1, 0), self.cols - 1)

    def _print(self, action: Action) -> None:
        assert action.char is not None
        char = action.char
        r, c = self.cursor_row, self.cursor_col
        width = self.width_table.width_of(char)

        if width == 0:
            # 组合标记（零宽）：不移动光标，附着到“当前基字”。基字位于
            # 光标左侧：普通基字在 c-1；双宽基字的左半在 c-2。若左侧
            # 没有基字（行首孤立标记），则附着到光标当前格，状态从
            # VIRGIN 变为 BLANK，仍保留各自的字节来源。
            target_col = self._combining_target_col(r, c)
            cell = self._grid[r][target_col]
            if cell.state == CellState.VIRGIN:
                cell.state = CellState.BLANK
                cell.clear_range = (action.start, action.end)
            cell.is_continuation = False
            cell.marks.append(CombiningMark(char, action.start, action.end))
            cell.last_write = (action.start, action.end)
            return

        if width == 2:
            if c + 1 >= self.cols:
                # 双宽字无法完整放入最后一列：降级按单宽放在末列。
                self._prepare_single(r, c, action)
                self._set_base(r, c, char, action, continuation=False)
                self.cursor_col = c + 1 if c + 1 < self.cols else c
                return
            self._prepare_wide(r, c, action)
            self._set_base(r, c, char, action, continuation=False)
            right = self._grid[r][c + 1]
            right.state = CellState.CONTENT
            right.base = None
            right.marks = []
            right.is_continuation = True
            right.last_write = (action.start, action.end)
            right.clear_range = None
            self.cursor_col = c + 2
            return

        # 单宽字
        self._prepare_single(r, c, action)
        self._set_base(r, c, char, action, continuation=False)
        self.cursor_col = min(c + 1, self.cols - 1)

    def _prepare_single(self, r: int, c: int, action: Action) -> None:
        """在 (r,c) 写入单宽字符前，解决与旧双宽字的冲突。"""
        cell = self._grid[r][c]
        # 当前格是某双宽字的右半：清掉左边的旧字。
        if cell.is_continuation and c > 0:
            self._clear_wide_pair(r, c - 1, action)
            return
        # 当前格是双宽字左半：清掉这一对。
        if (
            cell.state == CellState.CONTENT
            and cell.base is not None
            and self.width_table.is_wide(cell.base)
        ):
            self._clear_wide_pair(r, c, action)

    def _prepare_wide(self, r: int, c: int, action: Action) -> None:
        """在 (r,c) 写入双宽字前，清掉两格涉及的所有旧双宽字。"""
        # 先收集需要清除的旧双宽字左列，再统一清除：清除会改变相邻格
        # 状态，边遍历边清会漏判与另一格相邻的第二个双宽字。
        left_cols: set[int] = set()
        for target in (c, c + 1):
            cell = self._grid[r][target]
            if cell.is_continuation and target > 0:
                left_cols.add(target - 1)
            elif (
                cell.state == CellState.CONTENT
                and cell.base is not None
                and self.width_table.is_wide(cell.base)
            ):
                left_cols.add(target)
        for left_col in left_cols:
            self._clear_wide_pair(r, left_col, action)

    def _clear_wide_pair(
        self, r: int, left_col: int, action: Action
    ) -> None:
        """把占据 (r,left_col)/(r,left_col+1) 的旧双宽字整体清掉。"""
        for col in (left_col, left_col + 1):
            if 0 <= col < self.cols:
                self._reset_cell_to_blank(self._grid[r][col], action)

    @staticmethod
    def _reset_cell_to_blank(cell: Cell, action: Action) -> None:
        cell.state = CellState.BLANK
        cell.base = None
        cell.marks = []
        cell.is_continuation = False
        cell.last_write = (action.start, action.end)
        cell.clear_range = (action.start, action.end)

    def _set_base(
        self,
        r: int,
        c: int,
        char: str,
        action: Action,
        continuation: bool,
    ) -> None:
        cell = self._grid[r][c]
        # 覆盖“只有孤立组合标记的空白格”时保留这些标记及其字节来源；
        # 覆盖真正的旧基字时标记随旧字一起丢弃。
        preserved_marks = (
            list(cell.marks) if cell.base is None and not continuation else []
        )
        cell.state = CellState.CONTENT
        cell.base = char
        cell.marks = preserved_marks
        cell.is_continuation = continuation
        cell.last_write = (action.start, action.end)
        cell.clear_range = None

    def _erase_line(self, action: Action) -> None:
        assert action.mode is not None
        r = self.cursor_row
        if action.mode == 0:
            cols = range(self.cursor_col, self.cols)
        elif action.mode == 1:
            cols = range(0, self.cursor_col + 1)
        else:  # mode == 2，整行
            cols = range(0, self.cols)
        for c in cols:
            self._reset_cell_to_blank(self._grid[r][c], action)
