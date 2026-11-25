"""取证终端回放器。

公开 API 见 :class:`forensic_player.player.ForensicPlayer`、
:class:`forensic_player.width.WidthTable` 与
:class:`forensic_player.terminal.CellInfo`。
"""

from .player import ForensicPlayer, ScreenSnapshot
from .terminal import CellInfo, CellState
from .width import WidthTable

__all__ = [
    "ForensicPlayer",
    "ScreenSnapshot",
    "CellInfo",
    "CellState",
    "WidthTable",
]
