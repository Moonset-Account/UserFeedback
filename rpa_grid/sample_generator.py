"""样例生成器：生成虚构实验矩阵、独立真值与网格结构配置。

职责边界（与采集器严格分离）：
- 本模块只负责「造数据」，输出到 data/samples/<场景>/ 下；
- 采集器（rpa_grid.collector）只能经 HTTP 公开接口获取 matrix.tsv，
  本模块生成的 truth.json 不通过任何 HTTP 路由暴露，仅供 tests/ 读取比对；
- 全部数据为虚构，不涉及真实账户或外部服务。

运行：python3 -m rpa_grid.sample_generator
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .matrix import MISSING_TOKEN
from .value_model import ValueKind

# 列定义：键为可见列标题；hidden=True 的列在网格中隐藏。
COLUMNS = [
    {"key": "实验编号", "declared": ValueKind.ID_TEXT.value, "frozen": True},
    {"key": "样本编码", "declared": ValueKind.ID_TEXT.value, "frozen": True},
    {"key": "测定值", "declared": ValueKind.DECIMAL.value},
    {"key": "测定日期", "declared": ValueKind.DATE_TEXT.value},
    {"key": "备注", "declared": ValueKind.TEXT.value},
    {"key": "操作员", "declared": ValueKind.TEXT.value, "hidden": True},
    {"key": "审核状态", "declared": ValueKind.TEXT.value},
]

MERGED_HEADERS = [
    {"label": "标识信息", "from": "实验编号", "to": "样本编码"},
    {"label": "测量结果", "from": "测定值", "to": "测定日期"},
    {"label": "管理字段", "from": "备注", "to": "审核状态"},
]


@dataclass
class ScenarioSpec:
    name: str
    title: str
    # 每行：列标题 -> 原始字符串；None=缺失；""=空字符串
    rows: List[Dict[str, Optional[str]]]
    readonly_cells: List[Dict[str, int]]
    description: str


def _build_specs() -> List[ScenarioSpec]:
    normal = ScenarioSpec(
        name="demo-normal",
        title="演示场景 A：常规录入（小样例）",
        description="前导零标识、十进制数、日期文本、空字符串与缺失值同时出现。",
        rows=[
            {
                "实验编号": "EXP-001",
                "样本编码": "007",          # 前导零
                "测定值": "1.20",           # 十进制，尾零
                "测定日期": "2026-11-02",
                "备注": "常规对照样本",
                "操作员": "虚拟操作员甲",
                "审核状态": "待审核",
            },
            {
                "实验编号": "EXP-002",
                "样本编码": "042",
                "测定值": "0.008",
                "测定日期": "2026-11-03",
                "备注": "",                # 显式空字符串
                "操作员": "虚拟操作员乙",
                "审核状态": "待审核",
            },
            {
                "实验编号": "EXP-003",
                "样本编码": "108",
                "测定值": "12.5",
                "测定日期": "2026-11-04",
                "备注": "复测一次",
                # 操作员缺失：行尾截断
                "审核状态": MISSING_TOKEN,  # 显式缺失标记
            },
        ],
        readonly_cells=[],
    )

    edge = ScenarioSpec(
        name="demo-edge",
        title="演示场景 B：边界与危险输入（小样例）",
        description=(
            "公式样式字符串、含制表符/换行的文本、跨只读格粘贴、隐藏列、"
            "空串与缺失并存。"
        ),
        rows=[
            {
                "实验编号": "EXP-101",
                "样本编码": "0001",
                "测定值": "3.14",
                "测定日期": "2026-11-05",
                # 制表符 + 换行（TSV 中会被双引号包裹）
                "备注": "批次A\t第一行\n第二行",
                "操作员": "虚拟操作员丙",
                # 危险公式样式字符串：必须按文本保真录入，禁止公式求值
                "审核状态": "=1+1",
            },
            {
                "实验编号": "EXP-102",
                "样本编码": "0002",
                "测定值": "-2.75",
                "测定日期": "2026-11-06",
                # CSV 注入经典样式（虚构，不执行任何外部程序）
                "备注": "=cmd|' /C notepad'!A1",
                "操作员": "虚拟操作员丁",
                "审核状态": "@SUM(1,2)",
            },
            {
                "实验编号": "EXP-103",
                "样本编码": "0003",
                "测定值": "100.00",
                "测定日期": "2026-11-07",
                "备注": "含\"引号\"与\r回车的文本",
                # 操作员缺失
                "审核状态": "+1-2",  # 危险前缀样式
            },
        ],
        # 只读格：第 2 数据行（index=1）的「审核状态」只读。
        # RPA 必须拒绝该格并解释，其余格照常成功。
        readonly_cells=[{"row": 1, "column": "审核状态"}],
    )

    full = ScenarioSpec(
        name="acceptance-full",
        title="完整验收场景：全特性矩阵",
        description=(
            "覆盖前导零、多组只读/隐藏、引号转义、多行文本、危险公式、"
            "空串与缺失的全部组合。"
        ),
        rows=[
            {
                "实验编号": "EXP-201", "样本编码": "010", "测定值": "1.20",
                "测定日期": "2026-11-08", "备注": "首行\t制表",
                "操作员": "虚拟操作员戊", "审核状态": "=HYPERLINK(\"x\")",
            },
            {
                "实验编号": "EXP-202", "样本编码": "020", "测定值": "0",
                "测定日期": "2026-11-09", "备注": "",
                "操作员": "虚拟操作员己", "审核状态": "已锁定",
            },
            {
                "实验编号": "EXP-203", "样本编码": "030", "测定值": "-0.50",
                "测定日期": "2026-11-10",
                "备注": "多行\n第二行\n第三行",
                "操作员": MISSING_TOKEN, "审核状态": "待审核",
            },
            {
                "实验编号": "EXP-204", "样本编码": "040", "测定值": "999.999",
                "测定日期": "2026-11-11",
                "备注": "引号\"内嵌\"结束",
                "操作员": "虚拟操作员庚", "审核状态": "=-2+3",
            },
            {
                "实验编号": "EXP-205", "样本编码": "050", "测定值": "0.0001",
                "测定日期": "2026-11-12", "备注": "末行空串备注",
                # 两个缺失
                "审核状态": MISSING_TOKEN,
            },
        ],
        readonly_cells=[
            {"row": 1, "column": "审核状态"},   # 已锁定格只读
            {"row": 3, "column": "测定值"},      # 数值只读
        ],
    )
    return [normal, edge, full]


def _tsv_escape(value: Optional[str]) -> str:
    """RFC 4180 风格转义：含制表符/引号/换行时用双引号包裹。"""
    if value is None:
        return ""
    needs_quote = any(ch in value for ch in ('\t', '"', '\r', '\n'))
    if not needs_quote:
        return value
    return '"' + value.replace('"', '""') + '"'


def render_matrix_tsv(spec: ScenarioSpec) -> str:
    headers = [c["key"] for c in COLUMNS]
    lines = ["\t".join(headers)]
    lines.append("\t".join(c["declared"] for c in COLUMNS))
    for row in spec.rows:
        fields = []
        for idx, key in enumerate(headers):
            val = row.get(key)
            if val is None:
                # 行尾的缺失用截断表达；行中间的缺失用显式标记
                fields.append(MISSING_TOKEN if idx < len(headers) - 1 else "")
            else:
                fields.append(_tsv_escape(val))
        # 去掉行尾连续的空字段以制造「截断即缺失」语义
        while fields and fields[-1] == "":
            fields.pop()
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def build_truth(spec: ScenarioSpec) -> Dict:
    """独立真值：测试专用，采集器不得读取。"""
    hidden = {c["key"] for c in COLUMNS if c.get("hidden")}
    readonly = {(c["row"], c["column"]) for c in spec.readonly_cells}
    expected_cells = []
    for r_idx, row in enumerate(spec.rows):
        for col in COLUMNS:
            key = col["key"]
            val = row.get(key)
            if val == MISSING_TOKEN:
                kind, raw = ValueKind.MISSING.value, None
            elif val is None:
                kind, raw = ValueKind.MISSING.value, None
            elif val == "":
                kind, raw = ValueKind.EMPTY.value, ""
            else:
                kind, raw = col["declared"], val
            writable = key not in hidden and (r_idx, key) not in readonly
            expected_cells.append({
                "row": r_idx,
                "column": key,
                "kind": kind,
                "raw_text": raw,
                "writable": writable,
                "expect_status": "success" if writable else "rejected",
            })
    return {
        "scenario": spec.name,
        "note": "独立真值，仅供测试；采集器禁止读取本文件。",
        "cells": expected_cells,
    }


def build_grid_config(spec: ScenarioSpec) -> Dict:
    frozen = sum(1 for c in COLUMNS if c.get("frozen"))
    return {
        "title": spec.title,
        "frozen_col_count": frozen,
        "hidden_columns": [c["key"] for c in COLUMNS if c.get("hidden")],
        "readonly_cells": spec.readonly_cells,
        "merged_headers": MERGED_HEADERS,
        "row_label_header": "行号",
    }


def build_schema(spec: ScenarioSpec) -> Dict:
    """列声明接口数据：只含列名/类型/数据行数，不含任何期望值（非真值）。"""
    return {
        "scenario": spec.name,
        "data_row_count": len(spec.rows),
        "columns": [
            {"key": c["key"], "kind": c["declared"],
             "hidden": bool(c.get("hidden"))}
            for c in COLUMNS
        ],
    }


def generate_all(base: Path) -> List[Path]:
    base.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for spec in _build_specs():
        d = base / spec.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "matrix.tsv").write_text(render_matrix_tsv(spec), encoding="utf-8")
        (d / "truth.json").write_text(
            json.dumps(build_truth(spec), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (d / "grid_config.json").write_text(
            json.dumps(build_grid_config(spec), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (d / "schema.json").write_text(
            json.dumps(build_schema(spec), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (d / "scenario.json").write_text(
            json.dumps(
                {"name": spec.name, "title": spec.title,
                 "description": spec.description},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        paths.append(d)
    return paths


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "data" / "samples"
    for p in generate_all(out):
        print(f"generated: {p}")
