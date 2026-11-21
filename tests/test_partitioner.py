"""edge_partitioner 的 unittest 测试。

运行方式（在工作区根目录）::

    python3 -m unittest discover -s tests -p 'test_*.py' -v

覆盖范围：
- 正常场景：单机最优、混合分图、分叉张量共享拷贝、输出读取复用、次序可执行性；
- 边界场景：零耗时、14 算子上限、设备不支持（None）、并列任选、输入校验；
- 交错场景：随机无环图上与独立暴力参照逐例对账，并对每个解做端到端模拟校验。
"""

import os
import random
import sys
import unittest

# 保证从工作区根目录直接 discover 时也能导入工作区内的包。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from edge_partitioner import (  # noqa: E402
    CPU,
    NPU,
    Copy,
    CopyStep,
    ComputeStep,
    PartitionError,
    Problem,
    Solution,
    partition,
    topological_order,
    validate_solution,
)


def op(name, inputs, output, cpu, npu):
    return {
        "name": name,
        "inputs": list(inputs),
        "output": output,
        "costs": {CPU: cpu, NPU: npu},
    }


def build_problem(operators, model_inputs, model_outputs, copy_time):
    return Problem.build(
        operators=operators,
        model_inputs=list(model_inputs),
        model_outputs=list(model_outputs),
        copy_time=dict(copy_time),
    )


def simulate_and_check(testcase, problem, solution):
    """独立模拟 solution.steps，并校验费用分解与输出位置。"""
    validate_solution(problem, solution)  # 包内自带的端到端校验先跑一遍。

    present = {CPU: set(problem.model_inputs), NPU: set()}
    compute_sum = 0
    copy_sum = 0
    op_by_name = {o.name: o for o in problem.operators}
    finished = []
    seen_copy_tensors = set()

    for step in solution.steps:
        if isinstance(step, ComputeStep):
            o = op_by_name[step.op_name]
            testcase.assertTrue(
                all(t in present[step.device] for t in o.inputs),
                f"算子 {step.op_name} 执行时输入未在本地就绪",
            )
            present[step.device].add(o.output)
            compute_sum += step.cost
            finished.append(step.op_name)
        else:
            assert isinstance(step, CopyStep)
            c = step.copy
            testcase.assertNotIn(c.tensor, seen_copy_tensors)
            seen_copy_tensors.add(c.tensor)
            testcase.assertIn(c.tensor, present[c.source])
            present[c.target].add(c.tensor)
            copy_sum += c.cost

    # 拓扑合法性：算子必须在其全部生产者之后执行。
    producer = {o.output: o.name for o in problem.operators}
    position = {name: i for i, name in enumerate(finished)}
    for o in problem.operators:
        for t in o.inputs:
            parent = producer.get(t)
            if parent is not None:
                testcase.assertLess(position[parent], position[o.name])

    testcase.assertEqual(compute_sum, solution.compute_time)
    testcase.assertEqual(copy_sum, solution.copy_time)
    testcase.assertEqual(
        compute_sum + copy_sum,
        solution.total_time,
    )
    for t in problem.model_outputs:
        testcase.assertIn(t, present[CPU])

    # 每张量至多复制一次。
    testcase.assertEqual(
        len(seen_copy_tensors),
        len(solution.copies),
    )


def brute_force_total(problem):
    """测试内独立实现的暴力参照：枚举掩码并直接按语义重算费用。"""
    n = len(problem.operators)
    producer = {o.output: o for o in problem.operators}
    best = None
    for mask in range(1 << n):
        assignment = {}
        compute_total = 0
        for i, o in enumerate(problem.operators):
            device = NPU if (mask >> i) & 1 else CPU
            cost = o.costs[device]
            if cost is None:
                break
            assignment[o.name] = device
            compute_total += cost
        else:
            copy_total = 0
            for tensor in set(problem.model_inputs) | set(producer):
                home = (
                    assignment[producer[tensor].name]
                    if tensor in producer
                    else CPU
                )
                readers = {
                    assignment[o.name]
                    for o in problem.operators
                    if tensor in o.inputs
                }
                if tensor in problem.model_outputs:
                    readers.add(CPU)
                other = NPU if home == CPU else CPU
                if other in readers:
                    copy_total += problem.copy_time[tensor]
            total = compute_total + copy_total
            best = total if best is None else min(best, total)
    return best


class NormalScenariosTest(unittest.TestCase):
    def test_single_op_npu_wins_with_two_directional_copies(self):
        # NPU 执行 1 + 输入 CPU->NPU 3 + 输出 NPU->CPU 5 = 9 < CPU 10。
        problem = build_problem(
            [op("a", ["x"], "y", cpu=10, npu=1)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 3, "y": 5},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment["a"], NPU)
        self.assertEqual(sol.compute_time, 1)
        self.assertEqual(sol.copy_time, 8)
        self.assertEqual(sol.total_time, 9)
        self.assertEqual(
            {(c.tensor, c.source, c.target) for c in sol.copies},
            {("x", CPU, NPU), ("y", NPU, CPU)},
        )
        kinds = [type(s).__name__ for s in sol.steps]
        self.assertEqual(kinds, ["CopyStep", "ComputeStep", "CopyStep"])
        simulate_and_check(self, problem, sol)

    def test_single_op_cpu_wins_no_copy_at_all(self):
        problem = build_problem(
            [op("a", ["x"], "y", cpu=10, npu=1)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 10, "y": 10},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment["a"], CPU)
        self.assertEqual(sol.copies, [])
        self.assertEqual(sol.total_time, 10)
        self.assertEqual([type(s).__name__ for s in sol.steps], ["ComputeStep"])
        simulate_and_check(self, problem, sol)

    def test_chain_mixed_partition_input_copy_once(self):
        # a 在 NPU，b 回到 CPU：x 复制到 NPU 一次，a 的输出 t 复制回 CPU 一次。
        problem = build_problem(
            [
                op("a", ["x"], "t", cpu=100, npu=1),
                op("b", ["t"], "y", cpu=1, npu=100),
            ],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 2, "t": 3, "y": 4},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment, {"a": NPU, "b": CPU})
        self.assertEqual(sol.compute_time, 2)
        # x: CPU->NPU 2；t: NPU->CPU 3；y 产生即在 CPU，无需拷贝。
        self.assertEqual(sol.copy_time, 5)
        self.assertEqual(sol.total_time, 7)
        # copies 按拓扑输出序排列；两条拷贝的方向与费用逐一核对。
        self.assertEqual(
            {(c.tensor, c.source, c.target) for c in sol.copies},
            {("x", CPU, NPU), ("t", NPU, CPU)},
        )
        # 可执行次序中必须严格满足“先拷贝输入、再算子、再拷贝下一跳输入”。
        self.assertEqual(
            [
                (s.copy.tensor if isinstance(s, CopyStep) else s.op_name)
                for s in sol.steps
            ],
            ["x", "a", "t", "b"],
        )
        simulate_and_check(self, problem, sol)

    def test_fan_out_tensor_copied_once_not_per_edge(self):
        # t 同时被 b、c 两个 NPU 算子消费：同设备共享，t 根本无需跨设备复制；
        # 即便需要跨设备，也只能出现一条 Copy，而不是按两条边收两次费。
        problem = build_problem(
            [
                op("a", ["x"], "t", cpu=100, npu=1),
                op("b", ["t"], "u", cpu=100, npu=1),
                op("c", ["t"], "v", cpu=100, npu=1),
                op("d", ["u", "v"], "w", cpu=100, npu=1),
            ],
            model_inputs=["x"],
            model_outputs=["w"],
            copy_time={"x": 2, "t": 7, "u": 7, "v": 7, "w": 3},
        )
        sol = partition(problem)
        self.assertEqual(set(sol.assignment.values()), {NPU})
        copied = [c.tensor for c in sol.copies]
        self.assertEqual(sorted(copied), ["w", "x"])  # t/u/v 均无跨设备拷贝
        self.assertEqual(len(copied), len(set(copied)))
        self.assertEqual(sol.total_time, 4 + 2 + 3)
        simulate_and_check(self, problem, sol)

    def test_cross_device_fan_out_charges_single_shared_copy(self):
        # a 留在 CPU 产生 t，b、c 在 NPU 消费 t：t 只复制一次 CPU->NPU。
        problem = build_problem(
            [
                op("a", ["x"], "t", cpu=1, npu=100),
                op("b", ["t"], "u", cpu=100, npu=1),
                op("c", ["t"], "v", cpu=100, npu=1),
                op("d", ["u", "v"], "w", cpu=1, npu=100),
            ],
            model_inputs=["x"],
            model_outputs=["w"],
            copy_time={"x": 1, "t": 6, "u": 2, "v": 2, "w": 1},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment["a"], CPU)
        self.assertEqual(sol.assignment["b"], NPU)
        self.assertEqual(sol.assignment["c"], NPU)
        self.assertEqual(sol.assignment["d"], CPU)
        t_copies = [c for c in sol.copies if c.tensor == "t"]
        self.assertEqual(len(t_copies), 1)
        self.assertEqual((t_copies[0].source, t_copies[0].target), (CPU, NPU))
        # u、v 各自 NPU->CPU 一次，w 本地产生，x 本地消费，无多余拷贝。
        self.assertEqual(
            sorted((c.tensor, c.source, c.target) for c in sol.copies),
            [("t", CPU, NPU), ("u", NPU, CPU), ("v", NPU, CPU)],
        )
        simulate_and_check(self, problem, sol)

    def test_output_read_reuses_the_same_copy(self):
        # t 在 NPU 产生，被本地算子 b 消费且本身是模型输出：
        # 输出读取与（假设的）跨设备读取复用同一份拷贝，t 只回传一次。
        problem = build_problem(
            [
                op("a", ["x"], "t", cpu=100, npu=1),
                op("b", ["t"], "u", cpu=100, npu=1),
            ],
            model_inputs=["x"],
            model_outputs=["t", "u"],
            copy_time={"x": 2, "t": 4, "u": 5},
        )
        sol = partition(problem)
        t_copies = [c for c in sol.copies if c.tensor == "t"]
        self.assertEqual(len(t_copies), 1)
        self.assertEqual((t_copies[0].source, t_copies[0].target), (NPU, CPU))
        self.assertEqual(sol.total_time, 2 + (2 + 4 + 5))
        simulate_and_check(self, problem, sol)

    def test_model_input_that_is_also_output_passthrough(self):
        # 模型输入 x 同时是模型输出：纯 CPU 方案下 x 已在 CPU，零拷贝。
        problem = build_problem(
            [op("a", ["x"], "y", cpu=3, npu=100)],
            model_inputs=["x"],
            model_outputs=["x", "y"],
            copy_time={"x": 9, "y": 9},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment["a"], CPU)
        self.assertEqual(sol.copies, [])
        self.assertEqual(sol.total_time, 3)
        simulate_and_check(self, problem, sol)

    def test_independent_forks_interleaved_schedule(self):
        # 两条独立链交错放置，校验次序中拷贝总是先于对应读取。
        problem = build_problem(
            [
                op("a1", ["x"], "t1", cpu=100, npu=2),
                op("a2", ["t1"], "y1", cpu=2, npu=100),
                op("b1", ["x"], "t2", cpu=100, npu=2),
                op("b2", ["t2"], "y2", cpu=2, npu=100),
            ],
            model_inputs=["x"],
            model_outputs=["y1", "y2"],
            copy_time={"x": 1, "t1": 1, "t2": 1, "y1": 1, "y2": 1},
        )
        sol = partition(problem)
        self.assertEqual(
            sol.assignment,
            {"a1": NPU, "a2": CPU, "b1": NPU, "b2": CPU},
        )
        # x 只复制一次，两条链共享该 NPU 副本。
        self.assertEqual(len([c for c in sol.copies if c.tensor == "x"]), 1)
        simulate_and_check(self, problem, sol)

    def test_tied_optimum_returns_deterministic_cpu_preferred_choice(self):
        # 所有分配并列：拷贝耗时为 0，两端计算耗时相同。约定掩码最小者胜（CPU 优先）。
        problem = build_problem(
            [
                op("a", ["x"], "y", cpu=10, npu=10),
                op("b", ["x"], "z", cpu=10, npu=10),
            ],
            model_inputs=["x"],
            model_outputs=["y", "z"],
            copy_time={"x": 0, "y": 0, "z": 0},
        )
        sol = partition(problem)
        self.assertEqual(sol.total_time, 20)
        self.assertEqual(sol.assignment, {"a": CPU, "b": CPU})
        simulate_and_check(self, problem, sol)


class BoundaryScenariosTest(unittest.TestCase):
    def test_zero_costs_are_allowed(self):
        problem = build_problem(
            [op("a", ["x"], "y", cpu=0, npu=0)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 0, "y": 0},
        )
        sol = partition(problem)
        self.assertEqual(sol.total_time, 0)
        simulate_and_check(self, problem, sol)

    def test_fourteen_operator_chain_at_limit(self):
        operators = [op("o0", ["x"], "t0", cpu=5, npu=2)]
        for i in range(1, 14):
            operators.append(
                op(f"o{i}", [f"t{i - 1}"], f"t{i}", cpu=5, npu=2)
            )
        tensors = {"x"} | {f"t{i}" for i in range(14)}
        problem = build_problem(
            operators,
            model_inputs=["x"],
            model_outputs=["t13"],
            copy_time={t: 1 for t in tensors},
        )
        self.assertEqual(len(problem.operators), 14)
        sol = partition(problem)
        self.assertEqual(sol.total_time, brute_force_total(problem))
        simulate_and_check(self, problem, sol)

    def test_unsupported_device_forces_assignment(self):
        problem = build_problem(
            [op("a", ["x"], "y", cpu=100, npu=None)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 0, "y": 0},
        )
        sol = partition(problem)
        self.assertEqual(sol.assignment["a"], CPU)
        self.assertEqual(sol.total_time, 100)
        simulate_and_check(self, problem, sol)

    def test_topological_order_deterministic(self):
        problem = build_problem(
            [
                op("a", ["x"], "u", cpu=1, npu=1),
                op("b", ["x"], "v", cpu=1, npu=1),
                op("c", ["u", "v"], "y", cpu=1, npu=1),
            ],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 1, "u": 1, "v": 1, "y": 1},
        )
        self.assertEqual(topological_order(problem.operators), ["a", "b", "c"])

    def test_validate_detects_tampered_total(self):
        problem = build_problem(
            [op("a", ["x"], "y", cpu=3, npu=100)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 1, "y": 1},
        )
        sol = partition(problem)
        tampered = Solution(
            assignment=dict(sol.assignment),
            copies=list(sol.copies),
            steps=list(sol.steps),
            compute_time=sol.compute_time,
            copy_time=sol.copy_time,
            total_time=sol.total_time + 1,
        )
        with self.assertRaises(PartitionError):
            validate_solution(problem, tampered)

    def test_validate_detects_copy_list_mismatch(self):
        problem = build_problem(
            [op("a", ["x"], "y", cpu=3, npu=100)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 1, "y": 1},
        )
        sol = partition(problem)
        ghost = Copy(tensor="x", source=CPU, target=NPU, cost=1)
        tampered = Solution(
            assignment=dict(sol.assignment),
            copies=list(sol.copies) + [ghost],
            steps=list(sol.steps),
            compute_time=sol.compute_time,
            copy_time=sol.copy_time,
            total_time=sol.total_time,
        )
        with self.assertRaises(PartitionError):
            validate_solution(problem, tampered)


class InputValidationTest(unittest.TestCase):
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            operators=[op("a", ["x"], "y", cpu=1, npu=1)],
            model_inputs=["x"],
            model_outputs=["y"],
            copy_time={"x": 1, "y": 1},
        )
        kwargs.update(overrides)
        return kwargs

    def test_reject_invalid_shapes_and_values(self):
        cases = {
            "empty_graph": self._base_kwargs(operators=[]),
            "duplicate_op_name": self._base_kwargs(
                operators=[
                    op("a", ["x"], "y", 1, 1),
                    op("a", ["y"], "z", 1, 1),
                ],
                model_outputs=["z"],
                copy_time={"x": 1, "y": 1, "z": 1},
            ),
            "duplicate_output": self._base_kwargs(
                operators=[
                    op("a", ["x"], "y", 1, 1),
                    op("b", ["x"], "y", 1, 1),
                ]
            ),
            "unknown_input": self._base_kwargs(
                operators=[op("a", ["ghost"], "y", 1, 1)]
            ),
            "output_without_source": self._base_kwargs(model_outputs=["ghost"]),
            "missing_copy_time": self._base_kwargs(copy_time={"x": 1}),
            "unknown_copy_time_key": self._base_kwargs(
                copy_time={"x": 1, "y": 1, "ghost": 1}
            ),
            "negative_compute_cost": self._base_kwargs(
                operators=[op("a", ["x"], "y", cpu=-1, npu=1)]
            ),
            "negative_copy_cost": self._base_kwargs(
                copy_time={"x": -1, "y": 1}
            ),
            "bool_cost_rejected": self._base_kwargs(
                operators=[op("a", ["x"], "y", cpu=True, npu=1)]
            ),
            "missing_device_in_costs": self._base_kwargs(
                operators=[
                    {
                        "name": "a",
                        "inputs": ["x"],
                        "output": "y",
                        "costs": {CPU: 1},
                    }
                ]
            ),
            "unsupported_everywhere": self._base_kwargs(
                operators=[op("a", ["x"], "y", cpu=None, npu=None)]
            ),
            "input_output_name_collision": self._base_kwargs(
                operators=[op("a", ["x"], "x", 1, 1)]
            ),
            "duplicate_inputs_in_op": self._base_kwargs(
                operators=[op("a", ["x", "x"], "y", 1, 1)]
            ),
            "empty_model_inputs": self._base_kwargs(model_inputs=[]),
        }
        for label, kwargs in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(PartitionError):
                    Problem.build(**kwargs)

    def test_reject_cycle(self):
        with self.assertRaises(PartitionError):
            build_problem(
                [
                    op("a", ["z"], "y", 1, 1),
                    op("b", ["y"], "z", 1, 1),
                ],
                model_inputs=[],
                model_outputs=["z"],
                copy_time={"y": 1, "z": 1},
            )

    def test_reject_more_than_fourteen_operators(self):
        operators = [op("o0", ["x"], "t0", 1, 1)]
        for i in range(1, 15):
            operators.append(op(f"o{i}", [f"t{i - 1}"], f"t{i}", 1, 1))
        with self.assertRaises(PartitionError):
            build_problem(
                operators,
                model_inputs=["x"],
                model_outputs=["t14"],
                copy_time={"x": 0, **{f"t{i}": 0 for i in range(15)}},
            )


class RandomizedOracleTest(unittest.TestCase):
    def test_random_dags_match_independent_brute_force(self):
        rng = random.Random(20261122)
        for case_id in range(60):
            n = rng.randint(1, 8)
            model_inputs = [f"x{j}" for j in range(rng.randint(1, 3))]
            available = list(model_inputs)
            operators = []
            produced = []
            for i in range(n):
                degree = rng.randint(1, min(3, len(available)))
                inputs = rng.sample(available, degree)
                output = f"t{i}"
                cpu_cost, npu_cost = self._cost_pair(rng)
                operators.append(op(f"o{i}", inputs, output, cpu_cost, npu_cost))
                available.append(output)
                produced.append(output)
            model_outputs = rng.sample(
                produced, k=rng.randint(1, len(produced))
            )
            all_tensors = set(model_inputs) | set(produced)
            copy_time = {t: rng.randint(0, 8) for t in all_tensors}

            problem = build_problem(
                operators, model_inputs, model_outputs, copy_time
            )
            with self.subTest(case_id=case_id, n=n):
                sol = partition(problem)
                self.assertEqual(
                    sol.total_time,
                    brute_force_total(problem),
                )
                # 分解费用之和必须等于总费用，且步骤可端到端模拟。
                simulate_and_check(self, problem, sol)

    @staticmethod
    def _cost_pair(rng):
        cpu_cost = None if rng.random() < 0.1 else rng.randint(0, 20)
        npu_cost = None if rng.random() < 0.1 else rng.randint(0, 20)
        if cpu_cost is None and npu_cost is None:
            cpu_cost = rng.randint(0, 20)
        return cpu_cost, npu_cost


if __name__ == "__main__":
    unittest.main(verbosity=2)
