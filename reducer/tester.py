"""试验器协议、单次试验结果与异常分类。

试验器由调用方提供，必须暴露：

* ``version``：试验器版本字符串（缓存绑定三要素之一）；
* ``run(sample)``：在样本上执行一次试验，正常返回失败指纹字符串；
  超时时抛出 :class:`TimeoutError`，其他失败抛出任意其他异常。

引擎通过 :func:`safe_run` 把每次试验统一归类为五种结果之一，
并严格区分“原失败指纹复现”与超时/其他异常——后两者不计成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .model import Sample

# 五种单次试验状态
STATUS_MATCH = "match"          # 出现目标指纹
STATUS_OTHER_FP = "other_fp"    # 试验完成，但指纹不同
STATUS_TIMEOUT = "timeout"      # 超时
STATUS_ERROR = "error"          # 另一种异常
STATUS_FP_ERROR = "fp_error"    # 试验器本身故障（run 未返回字符串等）


@runtime_checkable
class Tester(Protocol):
    version: str

    def run(self, sample: Sample) -> str: ...


@dataclass(frozen=True)
class TrialResult:
    status: str
    fingerprint: str | None
    detail: str

    @property
    def accepted_match(self) -> bool:
        return self.status == STATUS_MATCH


def safe_run(tester: Tester, sample: Sample, target_fingerprint: str) -> TrialResult:
    """执行一次试验并归类，不抛异常。

    判定规则：

    * 抛 :class:`TimeoutError` -> ``timeout``；
    * 抛其他异常 -> ``error``；
    * 正常返回且等于目标指纹 -> ``match``；
    * 正常返回但不同 -> ``other_fp``；
    * 返回值不是字符串 -> ``fp_error``。
    """
    try:
        fingerprint = tester.run(sample)
    except TimeoutError as exc:
        return TrialResult(STATUS_TIMEOUT, None, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - 试验器异常一律归类，不外泄
        return TrialResult(STATUS_ERROR, None, f"{type(exc).__name__}: {exc}")
    if not isinstance(fingerprint, str):
        return TrialResult(
            STATUS_FP_ERROR,
            None,
            f"non_string_fingerprint:{type(fingerprint).__name__}",
        )
    if fingerprint == target_fingerprint:
        return TrialResult(STATUS_MATCH, fingerprint, "target_fingerprint")
    return TrialResult(STATUS_OTHER_FP, fingerprint, "different_fingerprint")


def all_matches(results: list[TrialResult], required_repeats: int) -> bool:
    """候选接受条件：恰好 ``required_repeats`` 次且全部复现目标指纹。"""
    if len(results) != required_repeats or required_repeats <= 0:
        return False
    return all(r.status == STATUS_MATCH for r in results)
