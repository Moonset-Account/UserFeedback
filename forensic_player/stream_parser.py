"""录制流字节解析器。

把原始字节流解析成一系列“已完成动作” :class:`Action`，每个动作携带它在
完整录制流中的绝对字节区间 ``[start, end)``。

设计要点：

* 解析器是增量式的，:meth:`StreamParser.feed` 可任意次数调用，分片可以
  截在多字节 UTF-8 字符或 CSI 控制序列中间，未完成的部分保留在内部
  缓冲区，等后续字节到达后再判定；
* 只有“已完成”的动作会被吐出。若查询点停在半个字符/半截序列处，
  对应动作尚未生成，回放时自然不会被应用；
* 所有字节偏移均相对 *拼接后* 的完整流（第一次 feed 从 0 开始）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ActionType(Enum):
    PRINT = "print"        # 打印一个已解码字符
    BACKSPACE = "bs"       # 退格 BS (0x08)
    CARRIAGE_RETURN = "cr"  # 回车 CR (0x0D)
    LINE_FEED = "lf"       # 换行 LF (0x0A)
    CUP = "cup"            # 光标绝对定位 CSI row;col H
    EL = "el"              # 行末擦除（含全部/行首变体） CSI n K


@dataclass(frozen=True)
class Action:
    """一个已完成的终端动作及其录制流字节来源。"""

    type: ActionType
    start: int
    end: int
    char: Optional[str] = None      # PRINT 时的字符
    row: Optional[int] = None       # CUP 的 1 基行号
    col: Optional[int] = None       # CUP 的 1 基列号
    mode: Optional[int] = None      # EL 模式 0/1/2


# UTF-8 首字节 -> 该字符总字节数
_UTF8_LENGTH = {
    n: 1 for n in range(0x00, 0x80)
}
_UTF8_LENGTH.update({n: 2 for n in range(0xC2, 0xE0)})
_UTF8_LENGTH.update({n: 3 for n in range(0xE0, 0xF0)})
_UTF8_LENGTH.update({n: 4 for n in range(0xF0, 0xF5)})

_REPLACEMENT = "�"


def _decode_utf8(data: bytes, pos: int) -> tuple[str, int, bool]:
    """从 ``data[pos]`` 解码一个 UTF-8 字符。

    返回 ``(字符, 下一位置, 是否完整)``。字节不足时 ``is_complete`` 为
    ``False``，调用方应保留这些字节等待后续分片。非法编码按 U+FFFD
    替换处理，消耗首字节及其后紧跟的连续续字节（至多声明长度-1 个）。
    """
    lead = data[pos]
    length = _UTF8_LENGTH.get(lead)
    if length is None:
        # 孤立续字节或非法首字节：消耗一个字节。
        return _REPLACEMENT, pos + 1, True
    if length == 1:
        return chr(lead), pos + 1, True
    if len(data) - pos < length:
        # 可能只是分片截断，保留等待。
        return "", len(data), False
    chunk = data[pos:pos + length]
    for b in chunk[1:]:
        if not 0x80 <= b <= 0xBF:
            # 非法：消耗首字节 + 紧跟的连续续字节。
            consumed = 2
            while consumed < length and 0x80 <= data[pos + consumed] <= 0xBF:
                consumed += 1
            return _REPLACEMENT, pos + consumed, True
    if length == 2:
        codepoint = ((chunk[0] & 0x1F) << 6) | (chunk[1] & 0x3F)
    elif length == 3:
        codepoint = (
            ((chunk[0] & 0x0F) << 12)
            | ((chunk[1] & 0x3F) << 6)
            | (chunk[2] & 0x3F)
        )
    else:
        codepoint = (
            ((chunk[0] & 0x07) << 18)
            | ((chunk[1] & 0x3F) << 12)
            | ((chunk[2] & 0x3F) << 6)
            | (chunk[3] & 0x3F)
        )
    # 代理码点或超界一律视为非法。
    if 0xD800 <= codepoint <= 0xDFFF or codepoint > 0x10FFFF:
        return _REPLACEMENT, pos + length, True
    return chr(codepoint), pos + length, True


def _parse_csi_params(param_bytes: bytes) -> Optional[list[int]]:
    """解析 CSI 参数字节，空参数按默认 1 处理；非法返回 None。"""
    text = param_bytes.decode("ascii")
    if text == "":
        return []
    params: list[int] = []
    for part in text.split(";"):
        if part == "":
            params.append(1)
            continue
        if not part.isdigit():
            return None
        params.append(int(part))
    return params


def _build_csi_action(
    param_bytes: bytes,
    final_byte: int,
    start: int,
    end: int,
) -> Optional[Action]:
    """把已完整接收的 CSI 序列构造成动作，不支持的序列返回 None。"""
    final_char = chr(final_byte)
    if final_char == "H":
        params = _parse_csi_params(param_bytes)
        if params is None:
            return None
        row = params[0] if len(params) >= 1 else 1
        col = params[1] if len(params) >= 2 else 1
        return Action(ActionType.CUP, start, end, row=row, col=col)
    if final_char == "K":
        params = _parse_csi_params(param_bytes)
        if params is None:
            return None
        mode = params[0] if params else 0
        if mode not in (0, 1, 2):
            return None
        return Action(ActionType.EL, start, end, mode=mode)
    return None


class StreamParser:
    """增量字节流解析器。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._base_offset = 0  # 缓冲区首字节在完整流中的绝对偏移
        self._in_escape = False
        self._in_csi = False
        self._csi_params = bytearray()
        self._escape_start = 0  # ESC 首字节在完整流中的绝对偏移

    @property
    def pending_bytes(self) -> int:
        """尚未形成完整动作、暂存在内部的字节数。"""
        return len(self._buffer)

    def feed(self, data: bytes) -> List[Action]:
        """喂入一个分片，返回本次新完成的动作列表。"""
        start_offset = self._base_offset
        self._buffer.extend(data)
        actions: list[Action] = []
        pos = 0
        buf = self._buffer
        # 缓冲区开头可能保留着上一次未完成的 ESC/CSI。重放前先清状态，
        # 让保留的 ESC 字节在下面的主循环里重新触发状态机；_escape_start
        # 保留不动，因此生成动作时绝对字节区间仍然正确。
        self._in_escape = False
        self._in_csi = False
        self._csi_params = bytearray()

        while pos < len(buf):
            abs_pos = start_offset + pos
            b = buf[pos]

            if self._in_csi:
                if 0x30 <= b <= 0x3F:
                    # 参数字节：数字、分号等。
                    self._csi_params.append(b)
                    pos += 1
                    continue
                if 0x20 <= b <= 0x2F:
                    # 中间字节，本实现不支持，放弃整个 CSI。
                    self._reset_escape()
                    pos += 1
                    continue
                if 0x40 <= b <= 0x7E:
                    # 终结字节。序列起点是当初记录的 ESC。
                    action = _build_csi_action(
                        bytes(self._csi_params),
                        b,
                        self._escape_start,
                        abs_pos + 1,
                    )
                    if action is not None:
                        actions.append(action)
                    self._reset_escape()
                    pos += 1
                    continue
                # 非法字节，放弃该转义序列。
                self._reset_escape()
                pos += 1
                continue

            if self._in_escape:
                if b == 0x5B:  # '[' 引入 CSI
                    self._in_csi = True
                    self._csi_params = bytearray()
                    pos += 1
                    continue
                # 其他 ESC 序列（如 ESC c）本实现不支持，静默跳过两字节。
                self._reset_escape()
                pos += 1
                continue

            if b == 0x1B:  # ESC
                self._in_escape = True
                self._escape_start = abs_pos
                pos += 1
                continue
            if b == 0x08:
                actions.append(Action(ActionType.BACKSPACE, abs_pos, abs_pos + 1))
                pos += 1
                continue
            if b == 0x0D:
                actions.append(
                    Action(ActionType.CARRIAGE_RETURN, abs_pos, abs_pos + 1)
                )
                pos += 1
                continue
            if b == 0x0A:
                actions.append(Action(ActionType.LINE_FEED, abs_pos, abs_pos + 1))
                pos += 1
                continue
            if b < 0x20 or b == 0x7F:
                # 其余 C0 控制字符不支持，按无动作消耗。
                pos += 1
                continue

            char, next_pos, complete = _decode_utf8(buf, pos)
            if not complete:
                # 分片截在多字节字符中，保留到下一次 feed。
                break
            actions.append(
                Action(
                    ActionType.PRINT,
                    abs_pos,
                    start_offset + next_pos,
                    char=char,
                )
            )
            pos = next_pos

        # 未完成的 ESC/CSI 序列也要整体保留：回退到 ESC 起点，
        # 让序列跨分片保持连续（参数已存在状态里，这里只需留字节）。
        if self._in_escape or self._in_csi:
            escape_pos = self._escape_start - start_offset
            if escape_pos < pos:
                pos = escape_pos

        # 释放已消费字节，保留未完成的尾部。
        del self._buffer[:pos]
        self._base_offset = start_offset + pos
        return actions

    def _reset_escape(self) -> None:
        self._in_escape = False
        self._in_csi = False
        self._csi_params = bytearray()


def parse_bytes(data: bytes) -> List[Action]:
    """一次性解析完整字节流的便捷函数。"""
    parser = StreamParser()
    return parser.feed(data)
