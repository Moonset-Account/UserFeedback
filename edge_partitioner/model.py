"""分图问题的领域模型与输入校验。

设备约定
--------
- ``CPU``：主机侧设备，模型输入最初只在 CPU，模型输出最终必须回到 CPU。
- ``NPU``：加速器侧设备。

每个算子执行一次并恰好产生一个张量；张量在设备间至多复制一次，复制后的
张量可被目标设备上的全部消费者共享（见 :mod:`edge_partitioner.partitioner`）。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

CPU = "cpu"
NPU = "npu"
DEVICES = (CPU, NPU)

# 题目规定的算子数量上限。
MAX_OPERATORS = 14


class PartitionError(ValueError):
    """输入问题本身不合法（结构、取值或环）时抛出。"""


class NoFeasiblePartitionError(PartitionError):
    """不存在任何可行设备分配时抛出（例如所有设备都不支持某算子）。"""


@dataclass(frozen=True)
class Operator:
    """单个算子。

    ``costs`` 为 ``{CPU: 整数耗时 | None, NPU: ...}``；``None`` 表示该算子
    不能在对应设备上执行。
    """

    name: str
    inputs: tuple[str, ...]
    output: str
    costs: Mapping[str, int | None]


@dataclass(frozen=True)
class Problem:
    """一次分图求解的完整输入。"""

    operators: tuple[Operator, ...]
    model_inputs: frozenset[str]
    model_outputs: frozenset[str]
    copy_time: Mapping[str, int]

    @staticmethod
    def build(
        operators: Iterable[Operator | Mapping[str, Any]],
        model_inputs: Sequence[str],
        model_outputs: Sequence[str],
        copy_time: Mapping[str, int],
    ) -> "Problem":
        """从算子对象（或等价 dict）构造并严格校验一个问题实例。"""
        norm_ops: list[Operator] = []
        names: set[str] = set()
        outputs: set[str] = set()

        for raw in operators:
            op = _normalize_operator(raw)
            if op.name in names:
                raise PartitionError(f"算子名重复: {op.name!r}")
            if op.output in outputs:
                raise PartitionError(
                    f"张量 {op.output!r} 被多个算子产生，违反单生产者约束"
                )
            names.add(op.name)
            outputs.add(op.output)
            norm_ops.append(op)

        if not 1 <= len(norm_ops) <= MAX_OPERATORS:
            raise PartitionError(
                f"算子数量必须在 1..{MAX_OPERATORS} 之间，实际为 {len(norm_ops)}"
            )

        inputs = _normalize_name_set(model_inputs, "model_inputs")
        final_outputs = _normalize_name_set(model_outputs, "model_outputs")
        if not inputs:
            raise PartitionError("model_inputs 至少包含一个模型输入张量")
        if not final_outputs:
            raise PartitionError("model_outputs 至少包含一个模型输出张量")
        if inputs & outputs:
            overlap = sorted(inputs & outputs)
            raise PartitionError(f"以下名称同时是模型输入和算子输出: {overlap}")

        produced_or_input = inputs | outputs
        for op in norm_ops:
            unknown = [t for t in op.inputs if t not in produced_or_input]
            if unknown:
                raise PartitionError(
                    f"算子 {op.name!r} 依赖未知张量 {unknown}，"
                    "它既不是模型输入也没有任何算子产生"
                )
        unknown_outputs = sorted(final_outputs - produced_or_input)
        if unknown_outputs:
            raise PartitionError(
                f"模型输出张量没有来源（既非模型输入也非算子输出）: {unknown_outputs}"
            )

        all_tensors = set(outputs) | inputs
        for op in norm_ops:
            all_tensors.update(op.inputs)
        all_tensors.update(final_outputs)

        if not isinstance(copy_time, Mapping):
            raise PartitionError("copy_time 必须是张量名到整数耗时的映射")
        unknown_keys = sorted(set(copy_time) - all_tensors)
        if unknown_keys:
            raise PartitionError(f"copy_time 中出现未知张量: {unknown_keys}")
        norm_copy: dict[str, int] = {}
        for tensor in all_tensors:
            if tensor not in copy_time:
                raise PartitionError(f"张量 {tensor!r} 缺少 copy_time 条目")
            value = copy_time[tensor]
            _check_non_negative_int(value, f"copy_time[{tensor!r}]")
            norm_copy[tensor] = int(value)

        # 环检测：构造失败会抛 PartitionError。
        topological_order(norm_ops)

        return Problem(
            operators=tuple(norm_ops),
            model_inputs=frozenset(inputs),
            model_outputs=frozenset(final_outputs),
            copy_time=norm_copy,
        )


def _normalize_operator(raw: Operator | Mapping[str, Any]) -> Operator:
    if isinstance(raw, Operator):
        op = raw
    elif isinstance(raw, Mapping):
        missing = [k for k in ("name", "inputs", "output", "costs") if k not in raw]
        if missing:
            raise PartitionError(f"算子定义缺少字段: {missing}")
        try:
            op = Operator(
                name=str(raw["name"]),
                inputs=tuple(raw["inputs"]),
                output=str(raw["output"]),
                costs=dict(raw["costs"]),
            )
        except TypeError as exc:
            raise PartitionError(f"算子定义类型不正确: {exc}") from exc
    else:
        raise PartitionError(f"不支持的算子定义类型: {type(raw).__name__}")

    if not op.name:
        raise PartitionError("算子名不能为空")
    if not op.output:
        raise PartitionError(f"算子 {op.name!r} 的输出张量名不能为空")
    if not isinstance(op.inputs, tuple) or not all(
        isinstance(t, str) and t for t in op.inputs
    ):
        raise PartitionError(
            f"算子 {op.name!r} 的 inputs 必须是非空字符串组成的序列"
        )
    if len(set(op.inputs)) != len(op.inputs):
        raise PartitionError(f"算子 {op.name!r} 的输入张量存在重复")
    if not isinstance(op.costs, Mapping):
        raise PartitionError(f"算子 {op.name!r} 的 costs 必须是映射")
    if set(op.costs) != set(DEVICES):
        raise PartitionError(
            f"算子 {op.name!r} 的 costs 必须且只能包含设备 {DEVICES}"
        )
    for device, value in op.costs.items():
        if value is not None:
            _check_non_negative_int(value, f"算子 {op.name!r} 在 {device} 上的耗时")
    if all(op.costs[d] is None for d in DEVICES):
        raise PartitionError(f"算子 {op.name!r} 在所有设备上均不可执行")
    return op


def _normalize_name_set(values: Sequence[str], field_name: str) -> set[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise PartitionError(f"{field_name} 必须是字符串序列")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise PartitionError(f"{field_name} 中的张量名必须是非空字符串")
        result.add(value)
    return result


def _check_non_negative_int(value: Any, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PartitionError(f"{what} 必须是非负整数，实际为 {value!r}")


def topological_order(operators: Sequence[Operator]) -> list[str]:
    """返回确定性的拓扑序（同名就绪算子按声明先后出队）。

    若图中存在环则抛出 :class:`PartitionError`。
    """
    index = {op.name: i for i, op in enumerate(operators)}
    producer = {op.output: op.name for op in operators}
    indegree = {op.name: 0 for op in operators}
    children: dict[str, list[str]] = {op.name: [] for op in operators}

    for op in operators:
        for tensor in op.inputs:
            parent = producer.get(tensor)
            if parent is not None:
                indegree[op.name] += 1
                children[parent].append(op.name)

    heap = [index[name] for name, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        i = heapq.heappop(heap)
        name = operators[i].name
        order.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, index[child])

    if len(order) != len(operators):
        raise PartitionError("计算图存在环，无法得到拓扑次序")
    return order
