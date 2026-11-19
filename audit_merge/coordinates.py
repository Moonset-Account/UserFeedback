"""坐标归一：把两种插件坐标转换成 Python 字符 (str) 索引区间。

字符索引基于 ``str`` 中的 Unicode 码位位置，行/列均为 0 基。

* ``utf8_bytes``：整文件内的 UTF-8 字节偏移；
* ``line_utf16``：行号 + 该行内的 UTF-16 码元（code unit）列偏移，
  BMP 字符占 1 个码元，星平面字符（含多数 emoji）占代理对即 2 个码元。

换行边界按原始字节解码后的文本切分，CRLF 视为一个边界并整体保留在该行，
这样两种坐标看到的“行”完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass


class SpanError(ValueError):
    """坐标无法映射到合法字符区间（越界、落在编码中间等）。"""


@dataclass(frozen=True)
class LineSpan:
    """某一行在整段文本中的字符索引范围（含行尾换行序列）。"""

    number: int
    start: int
    end: int
    text: str

    @property
    def content_end(self) -> int:
        """去掉行尾换行序列后的行尾字符索引。"""

        body = self.text
        if body.endswith("\r\n"):
            return self.end - 2
        if body.endswith("\n") or body.endswith("\r"):
            return self.end - 1
        return self.end


def split_lines_preserving(text: str) -> list[LineSpan]:
    """按原始换行切分，换行符保留在行文本内，行号 0 基。

    ``\\r\\n`` 作为一个整体边界处理，孤立 ``\\r`` 同样视为换行。
    """

    lines: list[LineSpan] = []
    start = 0
    number = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                end = i + 2
                i += 2
            else:
                end = i + 1
                i += 1
            lines.append(LineSpan(number, start, end, text[start:end]))
            start = end
            number += 1
        elif ch == "\n":
            end = i + 1
            i += 1
            lines.append(LineSpan(number, start, end, text[start:end]))
            start = end
            number += 1
        else:
            i += 1
    if start < n or not lines:
        lines.append(LineSpan(number, start, n, text[start:n]))
    return lines


def utf8_offsets_to_indices(text: str, start_byte: int, end_byte: int) -> tuple[int, int]:
    """UTF-8 字节偏移 -> (字符起始索引, 字符结束索引)。

    偏移必须落在 UTF-8 字符边界上，且 ``0 <= start < end``。
    """

    if not isinstance(start_byte, int) or not isinstance(end_byte, int):
        raise SpanError("UTF-8 字节偏移必须是整数")
    if start_byte < 0 or end_byte < start_byte:
        raise SpanError("UTF-8 字节偏移非法")

    char_start: int | None = None
    byte_pos = 0
    for index, ch in enumerate(text):
        if byte_pos == start_byte:
            char_start = index
        byte_pos += len(ch.encode("utf-8"))
        if byte_pos == end_byte:
            if char_start is None:
                raise SpanError("起始偏移落在 UTF-8 字符中间")
            return char_start, index + 1
        if byte_pos > end_byte:
            raise SpanError("结束偏移落在 UTF-8 字符中间或越界")
    # end_byte 指向文件末尾（零宽区间已被 start < end 排除）。
    if byte_pos == end_byte and char_start is not None and start_byte != end_byte:
        return char_start, len(text)
    raise SpanError("UTF-8 字节偏移越过文件边界")


def _utf16_units(line_text: str) -> list[int]:
    """返回每个码位起始位置对应的 UTF-16 码元列。"""

    units = [0]
    total = 0
    for ch in line_text:
        total += 2 if ord(ch) > 0xFFFF else 1
        units.append(total)
    return units


def line_utf16_to_indices(
    text: str,
    line: int,
    start_unit: int,
    end_unit: int,
    lines: list[LineSpan] | None = None,
) -> tuple[int, int]:
    """行号 + 行内 UTF-16 码元列 -> (字符起始索引, 字符结束索引)。

    行号与列均为 0 基；列相对该行（含换行符）起始位置计算。
    列必须落在 UTF-16 码元边界上，不允许从代理对中间切开。
    """

    if not isinstance(line, int) or not isinstance(start_unit, int) or not isinstance(end_unit, int):
        raise SpanError("行号/UTF-16 列必须是整数")
    if line < 0 or start_unit < 0 or end_unit < start_unit:
        raise SpanError("行号或 UTF-16 列非法")

    table = lines if lines is not None else split_lines_preserving(text)
    if line >= len(table):
        raise SpanError(f"行号 {line} 越界，共 {len(table)} 行")
    span = table[line]
    units = _utf16_units(span.text)
    line_unit_count = units[-1]
    if start_unit > line_unit_count or end_unit > line_unit_count:
        raise SpanError("UTF-16 列越过该行边界")
    if start_unit not in units or end_unit not in units:
        raise SpanError("UTF-16 列落在代理对中间")
    start_index = span.start + units.index(start_unit)
    end_index = span.start + units.index(end_unit)
    if end_index <= start_index:
        raise SpanError("替换区间必须为非空")
    return start_index, end_index
