"""变换原语与组合工具。

一条流水线把每行输入对状态的变换按原输入顺序叠加。连续若干行可以先
归约为一个 *复合变换*（本模块中统一表示为 ``Callable[[T], T]``）。

组合严格按从前往后的顺序：``compose([f, g])`` 表示先应用 ``f``、再
应用 ``g``，即 ``g(f(state))``。顺序颠倒会改变最终状态，合并模块必须
按原区间从左到右应用，不能按到达顺序应用。
"""

from typing import Callable, Generic, TypeVar

T = TypeVar("T")

Transform = Callable[[T], T]


def identity() -> Transform:
    """恒等变换（空区间或无报告时的中性元素）。"""

    def _identity(state: T) -> T:
        return state

    return _identity


def compose(transforms: list[Transform]) -> Transform:
    """按从左到右的顺序组合变换。

    compose([f, g])(x) == g(f(x))。返回的复合变换不修改输入列表，
    内部按快照顺序闭包，避免调用方后续修改列表造成隐性行为变化。
    """
    snapshot = list(transforms)

    def _composed(state: T) -> T:
        for transform in snapshot:
            state = transform(state)
        return state

    return _composed


class LabeledTransform(Generic[T]):
    """带标签的变换构造器（测试与可观测性辅助）。

    每个实例向状态末尾追加一个标签，便于在测试中断言实际生效的片段
    序列与应用顺序。例如 LabeledTransform("a") 把 state 变成
    state + ["a"]。
    """

    def __init__(self, label: str) -> None:
        self.label = label

    def __call__(self, state: list) -> list:
        result = list(state)
        result.append(self.label)
        return result

    def __repr__(self) -> str:
        return f"LabeledTransform({self.label!r})"
