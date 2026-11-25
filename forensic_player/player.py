"""取证回放器对外门面。

典型用法::

    from forensic_player import ForensicPlayer, WidthTable

    widths = WidthTable({"好": 2, "́": 0})
    player = ForensicPlayer(rows=4, cols=10, width_table=widths)
    player.feed(b"abc")          # 可分多次喂入，允许截在半字符/半序列处
    snap = player.screen_at(2)   # 按完整流的字节偏移查询
    snap.text()                  # 当时的屏幕
    snap.cell(0, 0).last_write_range  # 该格最后一次改写的字节来源区间
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .stream_parser import Action, StreamParser
from .terminal import CellInfo, CellState, Terminal
from .width import WidthTable


@dataclass(frozen=True)
class ScreenSnapshot:
    """某个字节偏移时刻的整屏只读快照。"""

    offset: int
    rows: int
    cols: int
    cursor: tuple[int, int]
    cells: List[List[CellInfo]]

    def cell(self, row: int, col: int) -> CellInfo:
        """取 0 基坐标单元格。"""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError(
                f"坐标越界: ({row}, {col})，屏幕为 {self.rows}x{self.cols}"
            )
        return self.cells[row][col]

    def line(self, row: int) -> str:
        """渲染单行。"""
        parts: list[str] = []
        for info in self.cells[row]:
            if info.state == CellState.CONTENT and not info.is_continuation:
                parts.append(info.text)
            else:
                parts.append(" ")
        return "".join(parts)

    def text(self) -> str:
        """渲染整屏，行之间用 ``\\n`` 连接。"""
        return "\n".join(self.line(r) for r in range(self.rows))


class ForensicPlayer:
    """接收录制流分片并支持按字节偏移回放查询的取证回放器。"""

    def __init__(self, rows: int, cols: int, width_table: WidthTable) -> None:
        self.rows = rows
        self.cols = cols
        self.width_table = width_table
        self._parser = StreamParser()
        self._actions: List[Action] = []
        self._total_bytes = 0

    # ------------------------------------------------------------------
    # 数据喂入
    # ------------------------------------------------------------------
    def feed(self, fragment: bytes) -> List[Action]:
        """喂入一段录制流分片，返回该分片新完成的动作。

        分片允许截在多字节 UTF-8 字符或 CSI 控制序列中间，未完成的
        字节由内部解析器保留，等待后续分片。
        """
        if not isinstance(fragment, (bytes, bytearray)):
            raise TypeError("feed 只接受 bytes/bytearray")
        actions = self._parser.feed(bytes(fragment))
        self._actions.extend(actions)
        self._total_bytes += len(fragment)
        return actions

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        rows: int,
        cols: int,
        width_table: WidthTable,
    ) -> "ForensicPlayer":
        """一次性载入完整字节流的便捷构造。"""
        player = cls(rows, cols, width_table)
        player.feed(data)
        return player

    @property
    def total_bytes(self) -> int:
        """已喂入的原始字节总数。"""
        return self._total_bytes

    @property
    def actions(self) -> Sequence[Action]:
        """目前已解析出的全部已完成动作。"""
        return tuple(self._actions)

    @property
    def pending_bytes(self) -> int:
        """截在半字符/半序列中、尚未形成动作的字节数。"""
        return self._parser.pending_bytes

    # ------------------------------------------------------------------
    # 偏移查询
    # ------------------------------------------------------------------
    def screen_at(self, offset: int) -> ScreenSnapshot:
        """回放至字节偏移 ``offset`` 处的屏幕状态。

        只应用 ``end <= offset`` 的已完成动作，因此截在半个字符或半截
        控制序列处的查询只会显示此前已完成的动作。``offset`` 的合法
        范围是 ``[0, total_bytes]``。
        """
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise TypeError("offset 必须是 int")
        if offset < 0 or offset > self._total_bytes:
            raise ValueError(
                f"offset 越界: {offset}，合法范围 [0, {self._total_bytes}]"
            )
        terminal = Terminal(self.rows, self.cols, self.width_table)
        for action in self._actions:
            if action.end > offset:
                break
            terminal.apply(action)
        return ScreenSnapshot(
            offset=offset,
            rows=self.rows,
            cols=self.cols,
            cursor=(terminal.cursor_row, terminal.cursor_col),
            cells=terminal.snapshot_cells(),
        )

    def cell_at(
        self, offset: int, row: int, col: int
    ) -> CellInfo:
        """查询某偏移、某格的取证信息（含最后改写的字节来源区间）。"""
        return self.screen_at(offset).cell(row, col)
