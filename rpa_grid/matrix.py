"""实验矩阵：把解析后的 TSV 与列类型声明组合成类型化矩阵。

样例文件布局（采集器只能通过 HTTP 公开接口读取 TSV，不能读取 truth）：

    <scenario>/matrix.tsv        —— 采集输入，第一行表头，第二行列类型声明，
                                    第三行起为数据行；字段缺失用连续制表符表达，
                                    缺失单元格用专门的 ``∅`` 标记列或行尾截断表达。
    <scenario>/truth.json        —— 独立真值，仅供测试比对，采集器禁止读取。
    <scenario>/grid_config.json  —— 网格结构（冻结列、只读格、隐藏列、合并标题）。

TSV 中「缺失」与「空字符串」的编码约定（在样例 README 与生成器中一致）：
- 字段位置存在但内容为空        -> 空字符串（empty）
- 行尾截断少字段 / 显式 ``<NA>`` -> 缺失值（missing）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .tsv_parser import ParsedTable, TsvParseError, parse_delimited
from .value_model import TypedValue, ValueKind, classify_value

MISSING_TOKEN = "<NA>"  # 显式缺失标记（行尾截断也是缺失）


@dataclass(frozen=True)
class ColumnSpec:
    key: str           # 列标题（可见列标题，自动化据此定位）
    declared: str      # ValueKind 的 value


@dataclass(frozen=True)
class CellRecord:
    """一条采集到的单元格记录（按行/列定位的逻辑坐标）。"""

    row_index: int          # 数据行序号，从 0 开始
    column: ColumnSpec
    value: TypedValue


@dataclass
class ExperimentMatrix:
    scenario: str
    columns: List[ColumnSpec]
    rows: List[List[CellRecord]]
    source_ref: str  # 来源证据（URL 或文件路径），用于网页展示

    @property
    def n_data_rows(self) -> int:
        return len(self.rows)

    def walk_cells(self):
        for r_idx, row in enumerate(self.rows):
            for cell in row:
                yield r_idx, cell


def build_matrix_from_tsv(
    tsv_text: str,
    scenario: str,
    source_ref: str,
    delimiter: str = "\t",
) -> ExperimentMatrix:
    """把 TSV 文本构建为类型化矩阵。

    约定：第 1 行可见列标题；第 2 行类型声明（ValueKind value）；
    第 3 行起为数据。
    """
    table: ParsedTable = parse_delimited(tsv_text, delimiter=delimiter)
    if table.n_rows < 1:
        raise TsvParseError("矩阵至少需要一行类型声明行")
    if len(table.rows[0]) != table.n_cols:
        raise TsvParseError("类型声明行列数与表头不一致")

    type_row = table.rows[0]
    columns: List[ColumnSpec] = []
    for header, declared in zip(table.headers, type_row):
        try:
            ValueKind(declared)
        except ValueError as exc:
            raise TsvParseError(
                f"列 {header!r} 的类型声明 {declared!r} 不受支持"
            ) from exc
        columns.append(ColumnSpec(key=header, declared=declared))

    data_rows: List[List[CellRecord]] = []
    for r_idx, raw_row in enumerate(table.rows[1:]):
        records: List[CellRecord] = []
        for c_idx, col in enumerate(columns):
            if c_idx >= len(raw_row):
                raw: Optional[str] = None  # 行尾截断 => 缺失
            else:
                token = raw_row[c_idx]
                if token == MISSING_TOKEN:
                    raw = None
                else:
                    raw = token
            typed = classify_value(raw, col.declared)
            records.append(CellRecord(r_idx, col, typed))
        data_rows.append(records)

    return ExperimentMatrix(
        scenario=scenario,
        columns=columns,
        rows=data_rows,
        source_ref=source_ref,
    )


@dataclass
class GridConfig:
    """本地电子表格式控件的结构配置（虚构，样例生成器产出）。"""

    title: str
    frozen_col_count: int
    hidden_columns: List[str] = field(default_factory=list)
    readonly_cells: List[Dict[str, int]] = field(default_factory=list)
    merged_headers: List[Dict[str, object]] = field(default_factory=list)
    row_label_header: str = "行号"

    @classmethod
    def load(cls, path: Path) -> "GridConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)
