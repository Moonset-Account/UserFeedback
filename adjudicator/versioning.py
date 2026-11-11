"""三段式数字版本与预发布版本的解析及排序。

版本语法（严格解析，解析失败即视为“无法比较的版本”）：

    <core>[-<prerelease>][+<build>]
    core       := 三段点分非负整数，即 MAJOR.MINOR.PATCH，不允许前导零
                  （数字 0 本身除外，例如 1.0.0 合法、1.01.0 非法）
    prerelease := 点分标识符序列，标识符只能由 [0-9A-Za-z-] 组成且不能为空；
                  纯数字标识符不允许前导零（0 本身除外）
    build      := 点分标识符序列，仅用于保留原始信息，不参与排序

排序规则（参照 SemVer 2.0.0 第 11 条的优先级规则）：
1. 核心三段按 MAJOR、MINOR、PATCH 依次做数值比较；
2. 同核心下，没有预发布段的正式版高于带预发布段的版本
   （1.0.0 > 1.0.0-rc.1）；
3. 两个预发布版本按标识符从左到右比较：
   - 纯数字标识符按数值比较；
   - 字母数字标识符按 ASCII 字典序比较；
   - 数字标识符的优先级低于字母数字标识符（1 < rc）；
   - 标识符序列短的排在前面（1.0.0-alpha < 1.0.0-alpha.1）；
4. build 元数据不参与任何比较。

不可解析的版本（例如 1.2、20261109-r1、含通配符的版本）不参与排序，
调用方必须通过 parse_version() 的返回值 None 或 IncomparableVersionError
识别这一情况，并把裁决降级为“待核实”，而不是猜测顺序。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

# 核心三段：每段为 0 或非零开头的数字（禁止前导零）
_CORE_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
# 预发布 / build 标识符：字母数字与连字符，非空
_IDENT_RE = re.compile(r"^[0-9A-Za-z-]+$")
_NUMERIC_RE = re.compile(r"^[0-9]+$")


class IncomparableVersionError(TypeError):
    """版本无法解析或两个版本不具备可比较性时抛出。"""


@dataclass(frozen=True)
class Version:
    """可比较的三段式版本对象。

    Attributes:
        core: (MAJOR, MINOR, PATCH) 非负整数三元组。
        prerelease: 预发布标识符元组；元素为 int（纯数字标识符）或 str。
            空元组表示正式发布版。
        raw: 原始版本字符串，用于报告与证据展示。
    """

    core: Tuple[int, int, int]
    prerelease: Tuple[Union[int, str], ...]
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        return self.raw


def _parse_identifiers(text: str, field: str) -> Tuple[Union[int, str], ...]:
    identifiers: List[Union[int, str]] = []
    for ident in text.split("."):
        if not ident or not _IDENT_RE.match(ident):
            raise IncomparableVersionError(f"非法{field}标识符: {ident!r}")
        if _NUMERIC_RE.match(ident):
            # 纯数字标识符禁止前导零（0 本身合法）
            if len(ident) > 1 and ident[0] == "0":
                raise IncomparableVersionError(f"数字标识符存在前导零: {ident!r}")
            identifiers.append(int(ident))
        else:
            identifiers.append(ident)
    return tuple(identifiers)


def parse_version(text: object) -> Optional[Version]:
    """尽量解析版本字符串；无法解析时返回 None（不抛异常）。

    裁决流程统一使用本函数，把 None 映射为“待核实 / 版本无法比较”，
    从而保证脏数据不会被静默按字符串排序处理。
    """
    if not isinstance(text, str) or not text:
        return None
    raw = text
    # build 元数据：剥离但保留在 raw 中，不参与排序
    main = text
    if "+" in main:
        main, build = main.split("+", 1)
        if not build or any(not _IDENT_RE.match(p) for p in build.split(".")):
            return None
    prerelease: Tuple[Union[int, str], ...] = ()
    if "-" in main:
        # 预发布段只允许出现一次核心分隔之后；split 一次即可
        core_text, pre_text = main.split("-", 1)
        try:
            prerelease = _parse_identifiers(pre_text, "预发布")
        except IncomparableVersionError:
            return None
    else:
        core_text = main
    core_match = _CORE_RE.match(core_text)
    if core_match is None:
        return None
    core = (int(core_match.group(1)), int(core_match.group(2)), int(core_match.group(3)))
    return Version(core=core, prerelease=prerelease, raw=raw)


def _compare_identifier(a: Union[int, str], b: Union[int, str]) -> int:
    a_num = isinstance(a, int)
    b_num = isinstance(b, int)
    if a_num and b_num:
        return (a > b) - (a < b)
    if a_num != b_num:
        # 数字标识符优先级低于字母数字标识符
        return -1 if a_num else 1
    a_s, b_s = str(a), str(b)
    return (a_s > b_s) - (a_s < b_s)


def _compare_prerelease(
    a: Tuple[Union[int, str], ...], b: Tuple[Union[int, str], ...]
) -> int:
    # 正式版（空元组）高于任何预发布版
    if not a and not b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    for x, y in zip(a, b):
        result = _compare_identifier(x, y)
        if result != 0:
            return result
    # 前缀相同则长者为大
    return (len(a) > len(b)) - (len(a) < len(b))


def compare(a: Version, b: Version) -> int:
    """返回 -1 / 0 / 1。任一参数不是 Version 即视为不可比较。"""
    if not isinstance(a, Version) or not isinstance(b, Version):
        raise IncomparableVersionError("比较双方都必须是已解析的 Version 对象")
    if a.core != b.core:
        return -1 if a.core < b.core else 1
    return _compare_prerelease(a.prerelease, b.prerelease)


# 富比较方法：仅在 Version 之间工作，其他类型直接抛 TypeError，
# 避免与不可解析版本混用时产生隐式排序。
def _make(op: str):
    def method(self: Version, other: object) -> bool:
        if not isinstance(other, Version):
            raise IncomparableVersionError(f"Version 不可与 {type(other).__name__} 比较")
        result = compare(self, other)
        return {
            "__lt__": result < 0,
            "__le__": result <= 0,
            "__eq__": result == 0,
            "__gt__": result > 0,
            "__ge__": result >= 0,
        }[op]

    return method


Version.__lt__ = _make("__lt__")  # type: ignore[attr-defined]
Version.__le__ = _make("__le__")  # type: ignore[attr-defined]
Version.__gt__ = _make("__gt__")  # type: ignore[attr-defined]
Version.__ge__ = _make("__ge__")  # type: ignore[attr-defined]
Version.__eq__ = _make("__eq__")  # type: ignore[attr-defined]
Version.__hash__ = lambda self: hash((self.core, self.prerelease))  # type: ignore[assignment]
