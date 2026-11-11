"""SVG transform 属性解析，支持协议子集：
matrix / translate / scale / rotate（含 rotate(a cx cy)）。
skewX、skewY 不在支持子集内，遇到将抛出 UnsupportedFeature。
"""

from __future__ import annotations

import re

from . import geometry

_FUNC_RE = re.compile(
    r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)"
)
_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class UnsupportedFeature(ValueError):
    """遇到协议子集之外的特性。"""


def parse_transform(attr: str | None):
    """把 transform 字符串解析为一组矩阵（按书写顺序返回，调用方按序左乘）。"""
    if not attr:
        return []
    matrices = []
    pos = 0
    stripped = attr.strip()
    for match in _FUNC_RE.finditer(attr):
        # 函数之间只允许空白
        between = attr[pos:match.start()].strip()
        if between:
            raise UnsupportedFeature(f"无法解析的 transform 片段: {between!r}")
        pos = match.end()
        name = match.group(1)
        nums = [float(n) for n in _NUM_RE.findall(match.group(2))]
        if name == "matrix":
            if len(nums) != 6:
                raise UnsupportedFeature(f"matrix 需要 6 个参数，得到 {len(nums)}")
            a, b, c, d, e, f = nums
            matrices.append((a, b, c, d, e, f))
        elif name == "translate":
            if len(nums) == 1:
                nums.append(0.0)
            if len(nums) != 2:
                raise UnsupportedFeature("translate 需要 1~2 个参数")
            matrices.append(geometry.translation(nums[0], nums[1]))
        elif name == "scale":
            if len(nums) == 1:
                nums.append(nums[0])
            if len(nums) != 2:
                raise UnsupportedFeature("scale 需要 1~2 个参数")
            matrices.append(geometry.scaling(nums[0], nums[1]))
        elif name == "rotate":
            if len(nums) == 1:
                matrices.append(geometry.rotation(nums[0]))
            elif len(nums) == 3:
                matrices.append(
                    geometry.rotation(nums[0], nums[1], nums[2])
                )
            else:
                raise UnsupportedFeature("rotate 参数数量非法")
        else:  # skewX / skewY
            raise UnsupportedFeature(f"暂不支持 {name} 斜切变换")
    tail = attr[pos:].strip()
    if tail:
        raise UnsupportedFeature(f"无法解析的 transform 片段: {tail!r}")
    if not matrices and stripped:
        raise UnsupportedFeature(f"无法解析 transform 属性: {attr!r}")
    return matrices


def combine(matrices) -> tuple:
    """按书写顺序合并：m = m_n * ... * m_2 * m_1。"""
    result = geometry.IDENTITY
    for m in matrices:
        result = geometry.multiply(result, m)
    return result
