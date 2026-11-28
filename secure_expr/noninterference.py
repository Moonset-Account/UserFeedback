"""非干涉性（Noninterference）验证辅助。

对同一个程序（AST）分别构造两个仅秘密输入取值不同的运行时，沿同一组观察
路径、在同一许可下逐一比对公开观察应答。若全部一致，则秘密输入的变化没有
泄露到该许可可见的任何字段（值与错误均不可区分）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .ast import Expr
from .labels import Level
from .runtime import Observation, Runtime


@dataclass(frozen=True)
class NoninterferenceResult:
    """一次非干涉性检查的结果。"""

    clearance: Level
    paths: tuple[tuple[int, ...], ...]
    observations_a: tuple[Observation, ...]
    observations_b: tuple[Observation, ...]
    consistent: bool
    first_divergence: tuple[int, ...] | None

    def failures(self) -> list[tuple[int, ...]]:
        if self.consistent:
            return []
        return [self.first_divergence] if self.first_divergence is not None else []


def _run_sweep(root: Expr,
               inputs: Mapping[str, tuple[object, Level]],
               paths: Sequence[Sequence[int]],
               clearance: Level) -> tuple[Runtime, tuple[Observation, ...]]:
    runtime = Runtime(root, inputs)
    observations = tuple(runtime.observe(tuple(path), clearance) for path in paths)
    return runtime, observations


def noninterference_check(
    root: Expr,
    inputs_a: Mapping[str, tuple[object, Level]],
    inputs_b: Mapping[str, tuple[object, Level]],
    paths: Sequence[Sequence[int]] = ((),),
    clearance: Level = Level.PUBLIC,
) -> NoninterferenceResult:
    """比较两组输入下的公开观察是否逐路径一致。

    参数
    ----
    root:
        两个样本共用的程序 AST。
    inputs_a / inputs_b:
        两份输入登记表，约定只在 SECRET 输入的取值上不同。
    paths:
        要检查的分字段观察路径集合，默认只观察根。
    clearance:
        观察者许可，默认 PUBLIC（最严格的公开视角）。
    """
    norm_paths = tuple(tuple(p) for p in paths)
    _, obs_a = _run_sweep(root, inputs_a, norm_paths, clearance)
    _, obs_b = _run_sweep(root, inputs_b, norm_paths, clearance)

    divergence: tuple[int, ...] | None = None
    for path, left, right in zip(norm_paths, obs_a, obs_b):
        if left != right:
            divergence = path
            break

    return NoninterferenceResult(
        clearance=clearance,
        paths=norm_paths,
        observations_a=obs_a,
        observations_b=obs_b,
        consistent=divergence is None,
        first_divergence=divergence,
    )
