"""两级安全标签格：PUBLIC（公开） <= SECRET（秘密）。"""

from __future__ import annotations

from enum import IntEnum


class Level(IntEnum):
    """安全标签级别，数值越大越敏感。"""

    PUBLIC = 0
    SECRET = 1

    @property
    def name_label(self) -> str:
        return "L" if self is Level.PUBLIC else "H"


def join(a: Level, b: Level) -> Level:
    """最小上界（lub）：任一输入为 SECRET 则结果为 SECRET。"""
    return Level(max(int(a), int(b)))


# 语义别名
lub = join
