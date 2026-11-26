"""改写前后的对照验证。

对共享计划中的每个请求分别执行：

1. :func:`~sharedscan.interpreter.execute_request` —— 改写前的参考语义；
2. :func:`~sharedscan.interpreter.execute_shared_plan` —— 改写后的共享计划投影。

并断言三层一致：

* 输出列名与顺序；
* 行的**多重集**（顺序无关、重复行计数，比较采用类型敏感相等：``1`` 与
  ``True``、``1`` 与 ``1.0`` 都不相等）；
* 每列实际观察到的类型族集合（含 NULL 的 ``None`` 族；AVG 的非空值恒为 float）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .interpreter import RequestResult, execute_request, execute_shared_plan
from .model import Catalog, Request
from .planner import SharedPlan


def type_strict_equal(a: object, b: object) -> bool:
    """类型敏感相等：bool 不等于 int，int 不等于 float；None 只等于 None。"""
    if a is None or b is None:
        return a is None and b is None
    if type(a) is not type(b):
        return False
    return a == b


def _row_signature(row: tuple) -> tuple:
    """把行转成可哈希、类型敏感的签名，便于多重集比较。"""
    return tuple((type(v).__name__, v) if v is not None else ("null", None) for v in row)


def _multiset(rows: list[tuple]) -> dict[tuple, int]:
    bag: dict[tuple, int] = {}
    for row in rows:
        sig = _row_signature(row)
        bag[sig] = bag.get(sig, 0) + 1
    return bag


@dataclass
class Mismatch:
    request: str
    kind: str
    detail: str


@dataclass
class VerifyReport:
    ok: bool
    compared: int
    mismatches: list[Mismatch] = field(default_factory=list)

    def raise_if_failed(self) -> None:
        if not self.ok:
            msg = "改写前后对照失败：\n" + "\n".join(
                f"  - [{m.kind}] 请求 {m.request!r}: {m.detail}" for m in self.mismatches
            )
            raise AssertionError(msg)


def _compare_result(name: str, ref: RequestResult, shared: RequestResult) -> list[Mismatch]:
    mismatches: list[Mismatch] = []

    if ref.columns != shared.columns:
        mismatches.append(
            Mismatch(name, "COLUMNS", f"列不一致: 参考={ref.columns} 共享={shared.columns}")
        )
        return mismatches  # 列不同，后续行比较没有意义

    ref_bag, shared_bag = _multiset(ref.rows), _multiset(shared.rows)
    if ref_bag != shared_bag:
        only_ref = [sig for sig, n in ref_bag.items() if shared_bag.get(sig, 0) != n]
        only_shared = [sig for sig, n in shared_bag.items() if ref_bag.get(sig, 0) != n]
        mismatches.append(
            Mismatch(
                name,
                "ROW_MULTISET",
                f"行多重集不一致: 仅参考侧={only_ref} 仅共享侧={only_shared}",
            )
        )

    if ref.column_families != shared.column_families:
        mismatches.append(
            Mismatch(
                name,
                "TYPE_FAMILIES",
                f"类型族不一致: 参考={_fmt_families(ref.column_families)} "
                f"共享={_fmt_families(shared.column_families)}",
            )
        )
    return mismatches


def _fmt_families(families: dict[str, set[Optional[str]]]) -> dict:
    return {k: sorted(v, key=lambda x: "~" if x is None else x) for k, v in families.items()}


def verify_plan(requests: list[Request], plan: SharedPlan,
                catalog: Catalog) -> VerifyReport:
    """执行改写前后两条路径并逐请求对照。"""
    shared_results = execute_shared_plan(plan, catalog)
    mismatches: list[Mismatch] = []
    compared = 0

    for request in requests:
        compared += 1
        ref = execute_request(request, catalog)
        if request.name not in shared_results:
            mismatches.append(
                Mismatch(request.name, "MISSING_PROJECTION", "共享计划缺少该请求的投影")
            )
            continue
        mismatches.extend(_compare_result(request.name, ref, shared_results[request.name]))

    extra = set(shared_results) - {r.name for r in requests}
    for name in sorted(extra):
        mismatches.append(Mismatch(name, "EXTRA_PROJECTION", "共享计划出现了未知请求的投影"))

    return VerifyReport(ok=not mismatches, compared=compared, mismatches=mismatches)
