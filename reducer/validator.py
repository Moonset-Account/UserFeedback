"""格式校验器。

调用方可以提供任意 ``validate(sample) -> bool`` 形式的校验器；
引擎同时内置 :class:`StructuralValidator`，负责核对本库声明的
结构约束（唯一 id、引用完整性、必填字段、字符串最小长度）。
"""

from __future__ import annotations

from typing import Callable, Protocol

from .model import Sample


class FormatValidator(Protocol):
    def validate(self, sample: Sample) -> bool: ...


class StructuralValidator:
    """声明式结构校验器，返回违例列表而不是抛异常。"""

    def check(self, sample: Sample) -> list[str]:
        violations: list[str] = []
        seen: set[str] = set()
        known_ids: set[str] = set()

        for record in sample.records:
            if record.rid in seen:
                violations.append(f"duplicate_id:{record.rid}")
            seen.add(record.rid)
            known_ids.add(record.rid)

        for record in sample.records:
            for spec in record.specs.values():
                # 必填字段存在性
                if spec.name not in record.fields:
                    if not spec.optional:
                        violations.append(
                            f"missing_required_field:{record.rid}.{spec.name}"
                        )
                    continue
                value = record.fields[spec.name]
                if spec.kind == "string":
                    if not isinstance(value, str):
                        violations.append(
                            f"type_error:{record.rid}.{spec.name}:not_string"
                        )
                    elif len(value) < spec.min_length:
                        violations.append(
                            f"min_length:{record.rid}.{spec.name}:"
                            f"{len(value)}<{spec.min_length}"
                        )
                elif spec.kind == "reference":
                    if not isinstance(value, str):
                        violations.append(
                            f"type_error:{record.rid}.{spec.name}:not_reference"
                        )
                    elif value not in known_ids:
                        violations.append(
                            f"dangling_reference:{record.rid}.{spec.name}->{value}"
                        )
                elif spec.kind == "references":
                    if not isinstance(value, list) or not all(
                        isinstance(v, str) for v in value
                    ):
                        violations.append(
                            f"type_error:{record.rid}.{spec.name}:not_reference_list"
                        )
                    else:
                        for target in value:
                            if target not in known_ids:
                                violations.append(
                                    f"dangling_reference:{record.rid}.{spec.name}->"
                                    f"{target}"
                                )
        return violations

    def validate(self, sample: Sample) -> bool:
        return not self.check(sample)


class CallableValidator:
    """把调用方自定义校验函数适配成 :class:`FormatValidator`。

    调用方函数返回 ``False`` 或抛异常均视为格式无效；
    结构约束始终先跑一遍，避免候选破坏声明的引用/长度约束。
    """

    def __init__(
        self,
        fn: Callable[[Sample], bool],
        structural: StructuralValidator | None = None,
    ) -> None:
        self._fn = fn
        self._structural = structural or StructuralValidator()
        self.last_violations: list[str] = []

    def validate(self, sample: Sample) -> bool:
        self.last_violations = self._structural.check(sample)
        if self.last_violations:
            return False
        try:
            return bool(self._fn(sample))
        except Exception:
            return False
