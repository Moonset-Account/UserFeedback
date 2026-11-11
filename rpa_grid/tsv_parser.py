"""TSV / CSV 解析器。

支持的协议子集（明确声明，非通用表格管理系统）：
- 分隔符：TSV 用水平制表符 ``\\t``；CSV 用逗号 ``,``（本项目默认 TSV）。
- 引号：遵循 RFC 4180 的双引号规则——字段可用双引号包裹，引号内允许出现
  分隔符、换行（``\\r\\n``/``\\n``/``\\r``）与转义双引号（``""`` 表示一个字面 ``"``）。
- 编码：UTF-8（带 BOM 时自动去掉）。
- 行尾：``\\r\\n``、``\\n``、``\\r`` 均接受；输入末尾单个换行不行产生额外空行。
- 空字符串与缺失值的区分：
  * 两个相邻分隔符之间为「空字符串」（``""``）；
  * 空文件或仅含行尾的文件解析为空表；
  * 行间字段数不补齐，由调用方按列名语义处理。

不支持的特性（超出声明子集）：类型推断、公式求值、自定义转义字符、
多字符分隔符、自定义转义字符、多字符分隔符、非 UTF-8 编码。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


class TsvParseError(ValueError):
    """协议格式错误：未闭合的引号、裸引号等。"""


@dataclass(frozen=True)
class ParsedTable:
    """解析结果。rows 中每个字段均为 ``str``（空字段即空字符串）。"""

    headers: List[str]
    rows: List[List[str]]
    delimiter: str

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.headers)


def parse_delimited(text: str, delimiter: str = "\t") -> ParsedTable:
    """解析带表头的分隔文本。第一行必须是表头。"""
    if delimiter not in ("\t", ","):
        raise ValueError(f"不支持的分隔符: {delimiter!r}，仅声明支持制表符与逗号")
    if text.startswith("﻿"):
        text = text[1:]

    records = _split_records(text, delimiter)
    if not records:
        raise TsvParseError("输入为空，缺少表头行")

    headers = records[0]
    if any(h == "" for h in headers):
        # 表头缺名会让“按可见标题定位”产生歧义，直接拒绝。
        raise TsvParseError("表头中存在空列名，无法按列标题定位数据区")
    rows = records[1:]
    return ParsedTable(headers=headers, rows=rows, delimiter=delimiter)


def _split_records(text: str, delimiter: str) -> List[List[str]]:
    """RFC 4180 子集状态机：quote / field / 行尾处理。"""
    records: List[List[str]] = []
    field: List[str] = []
    row: List[str] = []
    in_quotes = False
    i = 0
    n = len(text)
    started = False  # 是否出现过任何字段内容（用于区分纯空输入）

    def end_field() -> None:
        row.append("".join(field))
        field.clear()

    def end_row() -> None:
        end_field()
        records.append(row.copy())
        row.clear()

    while i < n:
        ch = text[i]
        started = True
        if in_quotes:
            if ch == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field.append('"')
                    i += 2
                    continue
                in_quotes = False
                i += 1
                continue
            field.append(ch)
            i += 1
            continue

        if ch == '"':
            # 引号只能出现在字段开头（RFC 4180）。
            if field:
                raise TsvParseError(f"位置 {i}：字段中间出现未转义的裸引号")
            in_quotes = True
            i += 1
        elif ch == delimiter:
            end_field()
            i += 1
        elif ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                i += 1
            end_row()
            i += 1
        elif ch == "\n":
            end_row()
            i += 1
        else:
            field.append(ch)
            i += 1

    if in_quotes:
        raise TsvParseError("未闭合的双引号字段")

    if started:
        # 最后一个字段/行：无行尾也要收尾；有行尾时 end_row 已处理。
        if row or field:
            end_row()
    return records
