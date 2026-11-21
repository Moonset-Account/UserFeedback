"""两设备计算图分图器。

目标
----
在至多 14 个算子的无环图上，为每个算子选择 CPU 或 NPU，最小化串行模型下
的总耗时：

    总耗时 = 全部算子在其分配设备上的执行耗时
           + 全部实际发生的跨设备张量复制耗时

计费规则（与题目一一对应）
--------------------------
1. 模型输入最初只在 CPU；仅当存在 NPU 侧消费者时才复制一次到 NPU。
2. 算子输出张量产生在算子所在设备；若另一设备上存在任一消费者，则复制一次，
   目标设备上的全部消费者共享该副本——分叉只收一次费用，而不是按边收费。
3. 模型输出最终必须在 CPU；在 NPU 产生（或仅经 NPU 消费者链路）的输出若
   CPU 上没有现成副本，则补一次 NPU -> CPU 的复制。两个方向的复制都计费。
4. 同一张量的复制动作可被消费者读取与输出读取复用，永不重复收费。

实现上枚举全部 ``2**n`` 种设备分配（n <= 14），对每种分配按上面规则计算
唯一的必需拷贝集合与其总费用，取全局最优。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    CPU,
    DEVICES,
    NPU,
    NoFeasiblePartitionError,
    Operator,
    PartitionError,
    Problem,
    topological_order,
)


@dataclass(frozen=True)
class Copy:
    """一次实际发生的跨设备张量复制。"""

    tensor: str
    source: str
    target: str
    cost: int


@dataclass(frozen=True)
class ComputeStep:
    """可执行次序中的一个算子执行动作。"""

    op_name: str
    device: str
    cost: int


@dataclass(frozen=True)
class CopyStep:
    """可执行次序中的一个张量复制动作。"""

    copy: Copy

    @property
    def cost(self) -> int:
        return self.copy.cost


@dataclass(frozen=True)
class Solution:
    """分图结果。

    - ``assignment``：算子名 -> 设备。
    - ``copies``：全部实际发生且计费的复制（每张量至多一条，已去重）。
    - ``steps``：可执行的串行次序（算子与复制交错排列）。
    - ``compute_time`` / ``copy_time`` / ``total_time``：费用分解。
    """

    assignment: dict[str, str]
    copies: list[Copy]
    steps: list[ComputeStep | CopyStep]
    compute_time: int
    copy_time: int
    total_time: int


def _other(device: str) -> str:
    return NPU if device == CPU else CPU


def _required_copies(
    problem: Problem, assignment: dict[str, str]
) -> tuple[list[Copy], int]:
    """计算给定分配下唯一必需的拷贝集合及其总费用。

    规则：张量 ``t`` 的“主设备”是其模型输入归属（CPU）或其生产者算子所在
    设备。只要另一侧设备上存在任何消费者（算子输入或模型输出读取），就恰好
    复制一次；多个消费者共享同一副本，不按边重复计费。
    """
    producer: dict[str, Operator] = {op.output: op for op in problem.operators}
    copies: list[Copy] = []

    # 以拓扑序枚举张量（模型输入按声明名补在末尾），使拷贝列表天然与
    # 可执行次序的排列方向一致，而不是按张量名字典序排列。
    ordered_tensors = [op.output for op in problem.operators]
    ordered_tensors.extend(sorted(problem.model_inputs))
    for tensor in ordered_tensors:
        if tensor in producer:
            home = assignment[producer[tensor].name]
        else:
            home = CPU  # 模型输入最初在 CPU

        consumers: list[Operator] = [
            op for op in problem.operators if tensor in op.inputs
        ]
        consumer_devices = {assignment[op.name] for op in consumers}
        if tensor in problem.model_outputs:
            # 模型输出最终必须在 CPU。
            consumer_devices.add(CPU)

        other = _other(home)
        if other in consumer_devices:
            copies.append(
                Copy(
                    tensor=tensor,
                    source=home,
                    target=other,
                    cost=problem.copy_time[tensor],
                )
            )

    copy_total = sum(c.cost for c in copies)
    return copies, copy_total


def _build_schedule(
    problem: Problem,
    assignment: dict[str, str],
    copies: list[Copy],
) -> list[ComputeStep | CopyStep]:
    """按拓扑序构造可执行的串行次序，复制动作在首个需求出现前完成。

    对每张量，其复制动作插入在“目标设备上第一个读取它的算子”之前；
    仅为满足输出回 CPU 而存在的复制在全部算子完成后补发。
    """
    order = topological_order(problem.operators)
    op_by_name = {op.name: op for op in problem.operators}
    pending = {c.tensor: c for c in copies}
    steps: list[ComputeStep | CopyStep] = []

    for name in order:
        op = op_by_name[name]
        device = assignment[name]
        needed = sorted(t for t in op.inputs if t in pending)
        for tensor in needed:
            copy = pending.pop(tensor)
            steps.append(CopyStep(copy))
        steps.append(
            ComputeStep(
                op_name=name,
                device=device,
                cost=op.costs[device],
            )
        )

    # 剩余的复制只可能是 NPU -> CPU 的模型输出回传（无 CPU 侧算子消费者）。
    for copy in copies:
        if copy.tensor in pending:
            steps.append(CopyStep(copy))
            pending.pop(copy.tensor)

    return steps


def partition(problem: Problem) -> Solution:
    """求解分图问题，返回全局串行耗时最低的 :class:`Solution`。

    并列时任选其一：本实现按分配掩码从小到大（CPU 优先、低位算子优先）
    确定地返回第一个最优解。
    """
    n = len(problem.operators)
    best: Solution | None = None

    for mask in range(1 << n):
        assignment: dict[str, str] = {}
        compute_total = 0
        feasible = True
        for i, op in enumerate(problem.operators):
            device = NPU if (mask >> i) & 1 else CPU
            cost = op.costs[device]
            if cost is None:
                feasible = False
                break
            assignment[op.name] = device
            compute_total += cost
        if not feasible:
            continue

        copies, copy_total = _required_copies(problem, assignment)
        total = compute_total + copy_total
        if best is None or total < best.total_time:
            schedule = _build_schedule(problem, assignment, copies)
            best = Solution(
                assignment=assignment,
                copies=list(copies),
                steps=schedule,
                compute_time=compute_total,
                copy_time=copy_total,
                total_time=total,
            )

    if best is None:
        raise NoFeasiblePartitionError("没有任何可行的算子设备分配")
    return best


def validate_solution(problem: Problem, solution: Solution) -> None:
    """对返回解做独立的端到端一致性校验，失败时抛出 :class:`PartitionError`。

    校验内容：
    - 分配覆盖全部算子且只使用合法、可执行的设备；
    - 每个复制步骤方向正确、张量存在且只复制一次；
    - 按 ``steps`` 模拟执行时，算子读取的输入在本地可用（复制先于读取）；
    - 模拟结束后每个模型输出都在 CPU；
    - 次序费用与 ``compute_time/copy_time/total_time`` 完全吻合。
    """
    assignment = solution.assignment
    if set(assignment) != {op.name for op in problem.operators}:
        raise PartitionError("assignment 必须为每个算子恰好指定一个设备")
    for op in problem.operators:
        device = assignment[op.name]
        if device not in DEVICES:
            raise PartitionError(f"算子 {op.name!r} 被分配到未知设备 {device!r}")
        if op.costs[device] is None:
            raise PartitionError(
                f"算子 {op.name!r} 不支持在 {device} 上执行"
            )

    # 按步骤模拟各设备上已就绪的张量集合。
    present: dict[str, set[str]] = {CPU: set(problem.model_inputs), NPU: set()}
    seen_copies: set[str] = set()
    compute_sum = 0
    copy_sum = 0
    op_by_name = {op.name: op for op in problem.operators}
    done_ops: set[str] = set()

    for step in solution.steps:
        if isinstance(step, ComputeStep):
            op = op_by_name.get(step.op_name)
            if op is None:
                raise PartitionError(f"次序中出现未知算子 {step.op_name!r}")
            if step.op_name in done_ops:
                raise PartitionError(f"算子 {step.op_name!r} 被重复执行")
            device = assignment[step.op_name]
            if step.device != device:
                raise PartitionError(
                    f"算子 {step.op_name!r} 的执行设备与 assignment 不一致"
                )
            if step.cost != op.costs[device]:
                raise PartitionError(
                    f"算子 {step.op_name!r} 的执行耗时与输入费用不一致"
                )
            missing = [t for t in op.inputs if t not in present[device]]
            if missing:
                raise PartitionError(
                    f"算子 {step.op_name!r} 在 {device} 上执行时输入不可用: {missing}"
                )
            present[device].add(op.output)
            done_ops.add(step.op_name)
            compute_sum += step.cost
        elif isinstance(step, CopyStep):
            copy = step.copy
            if copy.tensor in seen_copies:
                raise PartitionError(f"张量 {copy.tensor!r} 被重复复制")
            seen_copies.add(copy.tensor)
            if copy.source not in DEVICES or copy.target not in DEVICES:
                raise PartitionError(f"复制 {copy} 使用了未知设备")
            if copy.source == copy.target:
                raise PartitionError(f"复制 {copy.tensor!r} 的源与目标相同")
            if copy.tensor not in present[copy.source]:
                raise PartitionError(
                    f"复制 {copy.tensor!r} 时源设备上还没有该张量"
                )
            if copy.cost != problem.copy_time[copy.tensor]:
                raise PartitionError(
                    f"复制 {copy.tensor!r} 的耗时与 copy_time 不一致"
                )
            present[copy.target].add(copy.tensor)
            copy_sum += copy.cost
        else:  # pragma: no cover - 类型穷尽性保护
            raise PartitionError(f"未知步骤类型: {type(step).__name__}")

    if done_ops != {op.name for op in problem.operators}:
        raise PartitionError("可执行次序没有覆盖全部算子")
    missing_outputs = sorted(problem.model_outputs - present[CPU])
    if missing_outputs:
        raise PartitionError(f"模型输出最终不在 CPU: {missing_outputs}")

    declared = {c.tensor: c for c in solution.copies}
    if set(declared) != seen_copies:
        raise PartitionError("copies 与实际次序中的复制动作不一致")
    if compute_sum != solution.compute_time:
        raise PartitionError("compute_time 与次序模拟结果不一致")
    if copy_sum != solution.copy_time:
        raise PartitionError("copy_time 与次序模拟结果不一致")
    if compute_sum + copy_sum != solution.total_time:
        raise PartitionError("total_time 不等于执行与复制费用之和")
