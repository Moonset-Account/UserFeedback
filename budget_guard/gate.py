"""质量门禁：把分析报告与内存/记录数预算比较，给出放行/拒绝结论。"""

from __future__ import annotations

from typing import Optional

from .model import (
    B,
    AnalysisReport,
    BudgetViolation,
    GateDecision,
)


def _check_limit_value(value: Optional[int], name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数或 None，得到 {value!r}")
    return value


def evaluate(
    report: AnalysisReport,
    *,
    max_bytes: Optional[int] = None,
    max_records: Optional[int] = None,
    strict: bool = False,
) -> GateDecision:
    """根据预算评估报告。

    放行条件：

    1. 不存在活动的 blocking Issue（缺少证据 / 可能无界 / 未知目标 / 调用环）；
    2. 若设置了 ``max_bytes``，内存上界必须有限且 <= 预算；上界为 None
       时产生 ``UNPROVABLE_BOUND``；
    3. ``max_records`` 同理；
    4. ``strict=True`` 时，任何活动 warning（匿名引用、未证明共享等）
       也会拒绝，供导入端采用更保守策略。

    导入端若只想剔除个别分支，可先从 ``report.rejectable_branches`` 取
    ``branch_id``，调用 ``report.reject_branches(...)`` 得到新报告后再评估。
    """
    max_bytes = _check_limit_value(max_bytes, "max_bytes")
    max_records = _check_limit_value(max_records, "max_records")

    violations: list[BudgetViolation] = []
    total = report.total

    if max_bytes is not None:
        if total.bytes is None:
            violations.append(
                BudgetViolation(
                    dimension="bytes",
                    code=B.UNPROVABLE_BOUND,
                    message=(
                        "内存上界无法证明为有限值（缺少证据或存在无界分支），"
                        f"因而不能保证满足 max_bytes={max_bytes}"
                    ),
                    bound=None,
                    limit=max_bytes,
                )
            )
        elif total.bytes > max_bytes:
            violations.append(
                BudgetViolation(
                    dimension="bytes",
                    code=B.BYTES_BUDGET_EXCEEDED,
                    message=(
                        f"可证明内存上界 {total.bytes} 字节超过预算 "
                        f"max_bytes={max_bytes}"
                    ),
                    bound=total.bytes,
                    limit=max_bytes,
                )
            )

    if max_records is not None:
        if total.records is None:
            violations.append(
                BudgetViolation(
                    dimension="records",
                    code=B.UNPROVABLE_BOUND,
                    message=(
                        "展开记录数上界无法证明为有限值（缺少证据或存在无界分支），"
                        f"因而不能保证满足 max_records={max_records}"
                    ),
                    bound=None,
                    limit=max_records,
                )
            )
        elif total.records > max_records:
            violations.append(
                BudgetViolation(
                    dimension="records",
                    code=B.RECORDS_BUDGET_EXCEEDED,
                    message=(
                        f"可证明展开记录数上界 {total.records} 超过预算 "
                        f"max_records={max_records}"
                    ),
                    bound=total.records,
                    limit=max_records,
                )
            )

    blocking = list(report.blocking_issues)
    warnings = list(report.warnings)
    allowed = not violations and not blocking
    if strict and warnings:
        allowed = False

    return GateDecision(
        allowed=allowed,
        total=total,
        max_bytes=max_bytes,
        max_records=max_records,
        violations=violations,
        blocking_issues=blocking,
        warnings=warnings,
        rejectable_branches=list(report.rejectable_branches),
        report=report,
    )
