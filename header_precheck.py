"""传感器批次格式头预检。

接收端在分配任何载荷缓冲区之前调用本模块，判断一个批次文件能否
安全接收。全程只做整数运算，不会按声明长度创建大对象。

约定：
* 格式可表达的最大总长度为 32 位无符号整数上限 ``UINT32_MAX``
  （``2**32 - 1``，即 4294967295 字节）。
* 声明总长度 = 头长度 + 记录条数 * 每条记录长度。
* 记录条数为零时，约定载荷长度必须为零（即每条记录长度也必须为零）。
* 预检按固定顺序给出*首个*不满足条件的原因：
  字段越界 -> 声明总长无法表示 -> 内容截断 -> 尾部多余 -> 预算不足。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: 32 位无符号整数上限，格式头字段与声明总长度的共同上限。
UINT32_MAX = 0xFFFFFFFF

# 结果代码（稳定标识，测试与 CLI 均依赖这些字符串，不要改名）。
OK = "OK"
FIELD_OUT_OF_RANGE = "FIELD_OUT_OF_RANGE"
TOTAL_LENGTH_UNREPRESENTABLE = "TOTAL_LENGTH_UNREPRESENTABLE"
CONTENT_TRUNCATED = "CONTENT_TRUNCATED"
TRAILING_EXCESS = "TRAILING_EXCESS"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

#: 稳定的人类可读说明（中文，与结果代码一一对应）。
REASON_MESSAGES = {
    OK: "预检通过",
    FIELD_OUT_OF_RANGE: "字段越界：头字段或调用方参数超出允许范围",
    TOTAL_LENGTH_UNREPRESENTABLE: "声明总长无法表示：头长度+条数*每条长度超过32位无符号整数上限",
    CONTENT_TRUNCATED: "内容截断：实际文件字节数小于声明总长度",
    TRAILING_EXCESS: "尾部多余：实际文件字节数大于声明总长度",
    BUDGET_EXCEEDED: "预算不足：声明总长度超过调用方给定的资源预算",
}


@dataclass(frozen=True)
class PrecheckResult:
    """预检结果。

    Attributes:
        code: 稳定结果代码，取值为本模块 ``OK`` 等六个常量之一。
        ok: 是否通过（``code == OK``）。
        declared_total_length: 声明总长度；字段越界导致无法计算时为
            ``None``，否则始终给出整数结果（即使超出 uint32 上限）。
        reason: 人类可读说明。
    """

    code: str
    ok: bool
    declared_total_length: Optional[int]
    reason: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "ok": self.ok,
            "declared_total_length": self.declared_total_length,
            "reason": self.reason,
        }


def precheck_header(
    record_count: int,
    record_length: int,
    header_length: int,
    actual_file_size: int,
    resource_budget: int,
) -> PrecheckResult:
    """对传感器批次格式头做接收前预检。

    参数均为非负整数：

    Args:
        record_count: 头中声明的记录条数。
        record_length: 头中声明的每条记录长度（字节）。
        header_length: 格式头长度（字节）。
        actual_file_size: 实际文件字节数。
        resource_budget: 调用方给定的资源预算（字节），声明总长度不得
            超过该值。

    Returns:
        PrecheckResult: 首个失败原因；全部满足时代码为 ``OK``。

    检查顺序（短路返回首个失败项）：

    1. **字段越界**：任一参数不是非负整数；``record_count``、
       ``record_length``、``header_length`` 超过 ``UINT32_MAX``；
       或 ``record_count == 0`` 时 ``record_length != 0``
       （零批次约定载荷长度为零）。
    2. **声明总长无法表示**：
       ``header_length + record_count * record_length`` 超过
       ``UINT32_MAX``。
    3. **内容截断**：``actual_file_size`` 小于声明总长度。
    4. **尾部多余**：``actual_file_size`` 大于声明总长度。
    5. **预算不足**：声明总长度大于 ``resource_budget``。

    本函数只做整数运算，不按声明长度分配缓冲区，因此传入天文数字的
    声明长度也不会造成大内存分配。
    """

    # ---- 1. 字段范围检查 -------------------------------------------------
    head_fields = {
        "record_count": record_count,
        "record_length": record_length,
        "header_length": header_length,
    }
    for name, value in head_fields.items():
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            return _fail(FIELD_OUT_OF_RANGE, None, f"字段 {name} 必须是非负整数")
        if value > UINT32_MAX:
            return _fail(FIELD_OUT_OF_RANGE, None, f"字段 {name} 超过32位无符号整数上限")

    for name, value in (
        ("actual_file_size", actual_file_size),
        ("resource_budget", resource_budget),
    ):
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            return _fail(FIELD_OUT_OF_RANGE, None, f"参数 {name} 必须是非负整数")

    # 零批次约定：条数为零时载荷长度必须为零。
    if record_count == 0 and record_length != 0:
        return _fail(
            FIELD_OUT_OF_RANGE,
            None,
            "记录条数为零时每条记录长度必须为零（载荷长度为零）",
        )

    # ---- 2. 乘加总长度与可表示性 -----------------------------------------
    # Python 整数无溢出风险；绝不按该长度分配内存。
    declared_total = header_length + record_count * record_length
    if declared_total > UINT32_MAX:
        return _fail(
            TOTAL_LENGTH_UNREPRESENTABLE,
            declared_total,
            f"声明总长度 {declared_total} 超过32位无符号整数上限 {UINT32_MAX}",
        )

    # ---- 3/4. 实际文件长度：截断 / 尾部多余 ------------------------------
    if actual_file_size < declared_total:
        return _fail(
            CONTENT_TRUNCATED,
            declared_total,
            f"实际文件 {actual_file_size} 字节，少于声明总长度 {declared_total} 字节",
        )
    if actual_file_size > declared_total:
        return _fail(
            TRAILING_EXCESS,
            declared_total,
            f"实际文件 {actual_file_size} 字节，多于声明总长度 {declared_total} 字节",
        )

    # ---- 5. 资源预算 -----------------------------------------------------
    if declared_total > resource_budget:
        return _fail(
            BUDGET_EXCEEDED,
            declared_total,
            f"声明总长度 {declared_total} 字节超过资源预算 {resource_budget} 字节",
        )

    return PrecheckResult(
        code=OK,
        ok=True,
        declared_total_length=declared_total,
        reason=REASON_MESSAGES[OK],
    )


def _fail(code: str, declared_total: Optional[int], detail: str) -> PrecheckResult:
    return PrecheckResult(
        code=code,
        ok=False,
        declared_total_length=declared_total,
        reason=f"{REASON_MESSAGES[code]}：{detail}",
    )
