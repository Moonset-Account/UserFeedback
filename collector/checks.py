"""语义契约检查：标签、单位、限定说明（统计口径）、数据类型。

检查输入只来自浏览器实际采集到的页面文本与元素，选择器命中不等于语义通过。
违规分两级：
  - column 级（列头语义问题）：影响该列全部记录，整列记录进入隔离；
  - record 级（单元格数据类型问题）：仅隔离对应记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contracts import (
    STAT_MEAN,
    STAT_MEDIAN,
    STAT_UNKNOWN,
    TYPE_NUMBER,
    FieldContract,
)
from .units import UnsupportedUnit, Unit, normalize, scale_ratio

# 违规码 ---------------------------------------------------------------
LABEL_MISMATCH = "LABEL_MISMATCH"          # 标签含义改变（如干重→湿重）
UNIT_MISSING = "UNIT_MISSING"              # 契约要求单位，列头未识别到单位
UNIT_UNSUPPORTED = "UNIT_UNSUPPORTED"      # 单位不在声明支持的协议子集内
UNIT_INCOMPATIBLE = "UNIT_INCOMPATIBLE"    # 同列单位量纲不兼容
UNIT_SCALE = "UNIT_SCALE"                  # 同量纲但倍数改变（g→mg 等）
QUALIFIER_MISSING = "QUALIFIER_MISSING"    # 缺少必要限定说明（如“平均”）
STAT_MISMATCH = "STAT_MISMATCH"            # 统计口径改变（平均→中位数）
TYPE_MISMATCH = "TYPE_MISMATCH"            # 数据类型组成不符（数字→文本）
HEADER_MISSING = "HEADER_MISSING"          # 选择器未命中列头，无法建立观察值

_LEVEL_COLUMN = "column"
_LEVEL_RECORD = "record"

_BRACKETS = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
_NUMBER_RE = re.compile(r"[-+]?\d+(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_MEAN_WORDS = ("平均", "均值", "算术平均")
_MEDIAN_WORDS = ("中位数", "中位值", "中位")


@dataclass
class Violation:
    code: str
    field_key: str
    level: str
    message: str
    evidence_selector: str
    evidence_text: str
    expected: dict[str, Any] | None = None
    observed: dict[str, Any] | None = None
    row_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "field_key": self.field_key,
            "level": self.level,
            "message": self.message,
            "expected": self.expected,
            "observed": self.observed,
            "evidence_selector": self.evidence_selector,
            "evidence_text": self.evidence_text,
            "row_index": self.row_index,
        }


@dataclass
class ObservedField:
    """浏览器在列头上实际观察到的语义要素。"""

    key: str
    header_text: str
    label_text: str
    unit_raw: str | None
    unit: Unit | None
    stat: str
    qualifiers_found: tuple[str, ...]
    header_selector: str
    cell_selector: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "header_text": self.header_text,
            "label_text": self.label_text,
            "unit_raw": self.unit_raw,
            "unit": self.unit.canonical if self.unit else None,
            "unit_dimension": self.unit.dimension if self.unit else None,
            "stat": self.stat,
            "qualifiers_found": list(self.qualifiers_found),
            "header_selector": self.header_selector,
            "cell_selector": self.cell_selector,
        }


# --------------------------------------------------------------------------
# 列头解析
# --------------------------------------------------------------------------
def _split_brackets(text: str) -> tuple[str, str | None]:
    """返回（括号外文本, 第一个括号内文本）。"""
    for open_, close in _BRACKETS:
        start = text.find(open_)
        if start != -1:
            end = text.find(close, start + 1)
            if end != -1:
                return text[:start].strip(), text[start + 1 : end].strip()
    return text.strip(), None


def _detect_stat(text: str) -> str:
    if any(w in text for w in _MEDIAN_WORDS):
        return STAT_MEDIAN
    if any(w in text for w in _MEAN_WORDS):
        return STAT_MEAN
    return STAT_UNKNOWN


def _detect_qualifiers(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for word in (*_MEAN_WORDS, *_MEDIAN_WORDS):
        if word in text and word not in found:
            found.append(word)
    return tuple(found)


def _parse_unit(inner: str | None) -> tuple[str | None, Unit | None]:
    if inner is None:
        return None, None
    for token in re.split(r"[，,、\s]+", inner):
        token = token.strip()
        if not token:
            continue
        # 只有含拉丁字母 / % / 斜杠的令牌才视为单位写法；
        # 纯中文令牌（如“烘箱法测定”）属于方法说明，不算单位。
        if not re.search(r"[A-Za-z%/]", token):
            continue
        try:
            return token, normalize(token)
        except UnsupportedUnit:
            return token, None
    # 括号存在但没有任何单位写法
    return None, None


def build_observed(contract_field: FieldContract, header_text: str | None) -> ObservedField | None:
    if header_text is None:
        return None
    label_part, inner = _split_brackets(header_text)
    unit_raw, unit = _parse_unit(inner)
    return ObservedField(
        key=contract_field.key,
        header_text=header_text,
        label_text=label_part,
        unit_raw=unit_raw,
        unit=unit,
        stat=_detect_stat(header_text),
        qualifiers_found=_detect_qualifiers(header_text),
        header_selector=contract_field.header_selector,
        cell_selector=contract_field.cell_selector,
    )


# --------------------------------------------------------------------------
# 列头语义检查（column 级）
# --------------------------------------------------------------------------
def check_header(contract_field: FieldContract, observed: ObservedField | None) -> list[Violation]:
    selector = contract_field.header_selector
    if observed is None:
        return [
            Violation(
                code=HEADER_MISSING,
                field_key=contract_field.key,
                level=_LEVEL_COLUMN,
                message=f"选择器 {selector} 未命中列头，无法取得语义证据",
                evidence_selector=selector,
                evidence_text="",
                expected={"allowed_labels": list(contract_field.allowed_labels)},
                observed=None,
            )
        ]

    violations: list[Violation] = []
    violations.extend(_check_label(contract_field, observed))
    violations.extend(_check_unit(contract_field, observed))
    violations.extend(_check_qualifier_and_stat(contract_field, observed))
    return violations


def _check_label(cf: FieldContract, obs: ObservedField) -> list[Violation]:
    if any(label in obs.label_text for label in cf.allowed_labels):
        return []
    return [
        Violation(
            code=LABEL_MISMATCH,
            field_key=cf.key,
            level=_LEVEL_COLUMN,
            message=f"字段标签含义改变：列头“{obs.label_text}”不匹配契约标签 {list(cf.allowed_labels)}",
            evidence_selector=obs.header_selector,
            evidence_text=obs.header_text,
            expected={"allowed_labels": list(cf.allowed_labels)},
            observed={"label_text": obs.label_text, "header_text": obs.header_text},
        )
    ]


def _check_unit(cf: FieldContract, obs: ObservedField) -> list[Violation]:
    if cf.expected_unit is None:
        return []
    expected = normalize(cf.expected_unit)
    if obs.unit is None:
        code = UNIT_MISSING if obs.unit_raw is None else UNIT_UNSUPPORTED
        reason = "列头未声明单位" if code == UNIT_MISSING else f"单位 {obs.unit_raw!r} 不在支持子集内"
        return [
            Violation(
                code=code,
                field_key=cf.key,
                level=_LEVEL_COLUMN,
                message=f"{cf.name}：{reason}",
                evidence_selector=obs.header_selector,
                evidence_text=obs.header_text,
                expected={"unit": expected.canonical, "dimension": expected.dimension},
                observed={"unit_raw": obs.unit_raw},
            )
        ]
    if obs.unit.dimension != expected.dimension:
        return [
            Violation(
                code=UNIT_INCOMPATIBLE,
                field_key=cf.key,
                level=_LEVEL_COLUMN,
                message=(
                    f"{cf.name}：单位量纲不兼容，页面单位 {obs.unit.canonical}"
                    f"（{obs.unit.dimension}）无法换算为契约单位 {expected.canonical}"
                    f"（{expected.dimension}）"
                ),
                evidence_selector=obs.header_selector,
                evidence_text=obs.header_text,
                expected={"unit": expected.canonical, "dimension": expected.dimension},
                observed={"unit": obs.unit.canonical, "dimension": obs.unit.dimension},
            )
        ]
    ratio = scale_ratio(obs.unit, expected)
    if abs(ratio - 1.0) > 1e-12:
        return [
            Violation(
                code=UNIT_SCALE,
                field_key=cf.key,
                level=_LEVEL_COLUMN,
                message=(
                    f"{cf.name}：单位倍数改变，页面单位 {obs.unit.canonical} 相对契约单位 "
                    f"{expected.canonical} 的换算倍数为 {ratio:g}（页面数值需 ×{ratio:g}）"
                ),
                evidence_selector=obs.header_selector,
                evidence_text=obs.header_text,
                expected={"unit": expected.canonical},
                observed={"unit": obs.unit.canonical, "scale_ratio_to_expected": ratio},
            )
        ]
    return []


def _check_qualifier_and_stat(cf: FieldContract, obs: ObservedField) -> list[Violation]:
    if not cf.required_qualifiers and cf.required_stat == STAT_UNKNOWN:
        return []

    if cf.required_stat == STAT_MEAN:
        if obs.stat == STAT_MEDIAN:
            return [
                Violation(
                    code=STAT_MISMATCH,
                    field_key=cf.key,
                    level=_LEVEL_COLUMN,
                    message=f"{cf.name}：统计口径由“平均”变为“中位数”，语义不可自动等同",
                    evidence_selector=obs.header_selector,
                    evidence_text=obs.header_text,
                    expected={"stat": STAT_MEAN, "required_qualifiers": list(cf.required_qualifiers)},
                    observed={"stat": STAT_MEDIAN, "qualifiers_found": list(obs.qualifiers_found)},
                )
            ]
        if obs.stat != STAT_MEAN:
            return [
                Violation(
                    code=QUALIFIER_MISSING,
                    field_key=cf.key,
                    level=_LEVEL_COLUMN,
                    message=f"{cf.name}：缺少必要限定说明 {list(cf.required_qualifiers)}（统计口径不明）",
                    evidence_selector=obs.header_selector,
                    evidence_text=obs.header_text,
                    expected={"stat": STAT_MEAN, "required_qualifiers": list(cf.required_qualifiers)},
                    observed={"stat": STAT_UNKNOWN, "qualifiers_found": list(obs.qualifiers_found)},
                )
            ]
    return []


# --------------------------------------------------------------------------
# 单元格数据类型检查（record 级）
# --------------------------------------------------------------------------
def parse_number(text: str) -> float | None:
    if text is None:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def check_cell(cf: FieldContract, row_index: int, cell_text: str | None) -> list[Violation]:
    if cf.value_type != TYPE_NUMBER:
        if cell_text is None or str(cell_text).strip() == "":
            return [
                Violation(
                    code=TYPE_MISMATCH,
                    field_key=cf.key,
                    level=_LEVEL_RECORD,
                    message=f"{cf.name}：第 {row_index + 1} 行文本字段为空",
                    evidence_selector=cf.cell_selector,
                    evidence_text=cell_text or "",
                    expected={"value_type": cf.value_type},
                    observed={"value": cell_text},
                    row_index=row_index,
                )
            ]
        return []

    value = parse_number(cell_text or "")
    if value is None:
        return [
            Violation(
                code=TYPE_MISMATCH,
                field_key=cf.key,
                level=_LEVEL_RECORD,
                message=f"{cf.name}：第 {row_index + 1} 行无法解析为数值，数据类型组成不符",
                evidence_selector=cf.cell_selector,
                evidence_text=cell_text or "",
                expected={"value_type": TYPE_NUMBER},
                observed={"value": cell_text},
                row_index=row_index,
            )
        ]
    return []


def violation_severity(code: str) -> str:
    """供界面排序使用。"""
    order = {
        HEADER_MISSING: 0,
        LABEL_MISMATCH: 1,
        UNIT_INCOMPATIBLE: 2,
        UNIT_SCALE: 3,
        UNIT_MISSING: 4,
        UNIT_UNSUPPORTED: 4,
        STAT_MISMATCH: 5,
        QUALIFIER_MISSING: 6,
        TYPE_MISMATCH: 7,
    }
    return str(order.get(code, 9))
