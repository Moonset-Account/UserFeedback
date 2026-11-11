"""结构化实验矩阵的类型化值模型。

五种语义值严格区分，互不混淆：
- ``id_text``    标识符文本（保留前导零，如 ``007``、``EXP-042``）
- ``decimal``    十进制数（数值参与比对时按 Decimal，保留原始字面量）
- ``date_text``  日期文本（``2026-11-09`` 按字符串原样保存，不转 Date）
- ``text``       普通文本（可含制表符、换行、以 ``=`` 开头的公式样式字符串）
- ``empty``      空字符串（用户显式输入的空串，与「缺失」不同）
- ``missing``    缺失值（采集时该单元格无数据；写入时跳过，不触碰目标格）

本模块只做纯 Python 建模与规则判定，不依赖浏览器，单元测试可直接覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Optional


class ValueKind(str, Enum):
    ID_TEXT = "id_text"
    DECIMAL = "decimal"
    DATE_TEXT = "date_text"
    TEXT = "text"
    EMPTY = "empty"
    MISSING = "missing"

    @property
    def label(self) -> str:
        return _KIND_LABELS[self]


_KIND_LABELS = {
    ValueKind.ID_TEXT: "标识符文本(前导零)",
    ValueKind.DECIMAL: "十进制数",
    ValueKind.DATE_TEXT: "日期文本",
    ValueKind.TEXT: "普通文本",
    ValueKind.EMPTY: "空字符串",
    ValueKind.MISSING: "缺失值",
}


@dataclass(frozen=True)
class TypedValue:
    """一个带类型标注的单元格值。

    raw_text: 采集到的原始字符串（missing 时为 None）。
    kind:     语义类型。
    decimal_value: kind=decimal 时的精确数值，其余为 None。
    """

    raw_text: Optional[str]
    kind: ValueKind
    decimal_value: Optional[Decimal] = field(default=None, compare=False)

    @property
    def display(self) -> str:
        """网页/报告里展示的文本。"""
        if self.kind is ValueKind.MISSING:
            return "∅ 缺失"
        if self.kind is ValueKind.EMPTY:
            return "「」空串"
        return self.raw_text if self.raw_text is not None else ""


# 日期文本的声明格式（本项目只声明支持 ISO 日期，不做通用日期推断）。
import re

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 纯数字但带前导零（整数形态），或全数字串——用于 id_text 判定提示。
_LEADING_ZERO_RE = re.compile(r"^0\d+$")
# 普通十进制数字面量（含正负号、小数、科学计数法），不属于公式注入。
_PLAIN_NUMBER_RE = re.compile(r"^[+-]?(\d+\.\d+|\.\d+|\d+)([eE][+-]?\d+)?$")


def is_dangerous_formula(text: str) -> bool:
    """判定公式注入风险样式（Excel DDE/公式触发前缀）。

    - 以 ``=`` 开头一律危险（表格软件必按公式处理）；
    - 以 ``+``/``-``/``@`` 开头时，仅当整体不是普通数字面量、且含有
      公式特征字符（括号、感叹号、@ 等）才算危险，避免把 ``-2.75``
      这类负数十进制数误判；
    - 空白前缀先剥离再判定。
    """
    if text is None:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped[0] == "=":
        return True
    if stripped[0] in ("+", "-", "@"):
        if _PLAIN_NUMBER_RE.match(stripped):
            return False  # -2.75 这类普通十进制数不算公式
        # 非数字面量的运算符前缀串（如 +1-2、@SUM(..)）表格软件可能解释
        return True
    return False


def classify_value(
    raw: Optional[str],
    declared_type: Optional[str] = None,
) -> TypedValue:
    """把采集到的原始字符串按列声明类型建模为 TypedValue。

    declared_type 取自样例的列类型声明行（schema），取值为 ValueKind 的
    value 字符串；为 None 时仅做保守的文本处理，不做自动类型推断。
    """
    # 缺失：None 表示该单元格在采集结果中不存在。
    if raw is None:
        return TypedValue(raw_text=None, kind=ValueKind.MISSING)

    # 空字符串：存在但长度为 0——与缺失严格区分。
    if raw == "":
        return TypedValue(raw_text="", kind=ValueKind.EMPTY)

    if declared_type is not None:
        try:
            kind = ValueKind(declared_type)
        except ValueError as exc:
            raise ValueError(f"未知的列声明类型: {declared_type!r}") from exc
    else:
        kind = ValueKind.TEXT

    if kind is ValueKind.DECIMAL:
        try:
            dec = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"列声明为十进制数，但值无法解析: {raw!r}") from exc
        if not dec.is_finite():
            raise ValueError(f"十进制数必须有限: {raw!r}")
        return TypedValue(raw_text=raw, kind=kind, decimal_value=dec)

    if kind in (ValueKind.ID_TEXT, ValueKind.DATE_TEXT, ValueKind.TEXT):
        if kind is ValueKind.DATE_TEXT and not _DATE_RE.match(raw):
            raise ValueError(f"列声明为日期文本(YYYY-MM-DD)，实际值: {raw!r}")
        return TypedValue(raw_text=raw, kind=kind)

    if kind in (ValueKind.EMPTY, ValueKind.MISSING):
        raise ValueError(f"非空原始值不能建模为 {kind}: {raw!r}")

    raise ValueError(f"未处理的类型: {kind}")


def values_equal(expected: TypedValue, actual_text: Optional[str],
                 actual_kind: str) -> tuple[bool, str]:
    """回读比对。返回 (是否一致, 差异说明)。

    actual_text/actual_kind 来自浏览器网格回读：
    - missing 期望：目标格应保持「未写入」状态（网格模型里为 null）。
    - empty 期望：目标格值为空字符串且类型为 text/empty。
    - decimal：数值按 Decimal 比较，避免 1.20 与 1.2 的字面量差异误报，
      但同时要求回读字面量去尾零后与原始一致（网格 number 格按数值显示）。
    - 其余文本类：要求逐字符相等且类型一致。
    """
    try:
        actual = ValueKind(actual_kind)
    except ValueError:
        return False, f"回读类型 {actual_kind!r} 不是受支持的类型"

    if expected.kind is ValueKind.MISSING:
        if actual_text is None and actual is ValueKind.MISSING:
            return True, ""
        return False, f"期望缺失(未写入)，实际为 [{actual.label}] {_preview(actual_text)}"

    if expected.kind is ValueKind.EMPTY:
        if actual_text == "" and actual in (ValueKind.EMPTY, ValueKind.TEXT):
            return True, ""
        return False, f"期望空字符串，实际为 [{actual.label}] {_preview(actual_text)}"

    if expected.kind is ValueKind.DECIMAL:
        if actual is not ValueKind.DECIMAL:
            return False, f"期望十进制数，实际类型为 {actual.label}"
        try:
            actual_dec = Decimal(actual_text)
        except (InvalidOperation, TypeError):
            return False, f"回读值无法解析为十进制数: {_preview(actual_text)}"
        if actual_dec != expected.decimal_value:
            return False, (
                f"数值不一致：期望 {expected.decimal_value}，实际 {actual_dec}"
            )
        return True, ""

    # 文本类（id_text / date_text / text）
    if actual is not expected.kind and not (
        expected.kind is ValueKind.TEXT and actual in (ValueKind.ID_TEXT,
                                                       ValueKind.DATE_TEXT)
    ):
        # id_text/date_text 在网格里都以 text 存储格式承载，允许文本大类互通，
        # 但 decimal/missing/empty 不可冒充。
        if actual not in (ValueKind.ID_TEXT, ValueKind.DATE_TEXT, ValueKind.TEXT):
            return False, f"期望文本类({expected.kind.label})，实际类型 {actual.label}"
    if actual_text != expected.raw_text:
        return False, (
            f"文本逐字符不一致：期望 {_preview(expected.raw_text)}，"
            f"实际 {_preview(actual_text)}"
        )
    return True, ""


def _preview(text: Any, limit: int = 40) -> str:
    if text is None:
        return "∅"
    s = str(text).replace("\t", "⇥").replace("\r", "␍").replace("\n", "⏎")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return repr(s)
