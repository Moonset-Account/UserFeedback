# edge_partitioner — CPU / 加速器两设备推理图分图器

## 问题摘要

给定一个至多 **14 个算子**的无环计算图、每个算子在两台设备（`cpu` 与 `npu`）
上的整数耗时，以及每个张量（含模型输入）的跨设备复制耗时，求串行模型下总
耗时最低的设备分配方案。

约束与计费规则：

1. 每个算子恰好执行一次、恰好产生一个张量（单生产者）。
2. 模型输入最初只在 **CPU**；模型输出最终必须位于 **CPU**。
3. 张量的“主设备”：模型输入为 CPU；中间张量为其生产者算子所在设备。
4. 仅当另一设备上存在**任一**消费者（算子输入或模型输出读取）时才复制，
   且**每张量至多复制一次**，目标设备上的全部消费者共享该副本——分叉不按
   边重复收费；输出读取与算子读取复用同一份拷贝。
5. 两个方向（CPU→NPU、NPU→CPU）的复制都计费。复制耗时按张量给出，同一
   张量两个方向使用同一耗时值。
6. 串行总耗时 = 全部算子执行耗时 + 全部实际复制耗时。

## 目录结构

```
workspace/
├── edge_partitioner/
│   ├── __init__.py       # 公开 API 导出
│   ├── model.py          # 领域模型、输入校验、拓扑序/环检测
│   └── partitioner.py    # 枚举求解、拷贝去重、次序构造、解校验
├── tests/
│   └── test_partitioner.py
└── README.md
```

仅使用 Python 标准库（`dataclasses`、`heapq`、`unittest`、`random` 等），
无第三方依赖，不需要安装任何包。

## 公开 API

### 构造问题：`Problem.build(...)`

```python
from edge_partitioner import CPU, NPU, Problem

problem = Problem.build(
    operators=[
        {"name": "a", "inputs": ["x"], "output": "t",
         "costs": {CPU: 1, NPU: 100}},
        # costs[device] 为 None 表示该算子不能在此设备执行
    ],
    model_inputs=["x"],
    model_outputs=["w"],
    copy_time={"x": 1, "t": 6, "u": 2, "v": 2, "w": 1},
)
```

算子可传 dict，也可传 `Operator(name, inputs, output, costs)`。构造时严格
校验：算子数 1..14、名称/输出唯一、输入与输出均有来源、`copy_time` 覆盖
全部出现的张量、耗时为非负整数、图无环等，不合法时抛 `PartitionError`。

### 求解：`partition(problem) -> Solution`

枚举全部 `2**n` 种设备分配（n ≤ 14，最多 16384 种），对每种分配按规则推导
**唯一**必需的拷贝集合并计算费用，返回全局最低耗时解。并列时任选其一，
本实现确定性地取掩码最小者（CPU 优先、声明靠前的算子优先）。

`Solution` 字段：

| 字段 | 含义 |
| --- | --- |
| `assignment` | `dict[算子名, "cpu" | "npu"]`，最低耗时的设备分配 |
| `copies` | `list[Copy]`，每份**实际发生**且计费的拷贝（每张量至多一条） |
| `steps` | `list[ComputeStep | CopyStep]`，可执行的串行次序 |
| `compute_time` / `copy_time` / `total_time` | 费用分解，`total = compute + copy` |

- `Copy(tensor, source, target, cost)`
- `ComputeStep(op_name, device, cost)`
- `CopyStep(copy)`（属性 `.cost` 透传拷贝费用）

### 解校验：`validate_solution(problem, solution) -> None`

独立按 `steps` 端到端模拟：分配合法性、复制先于读取、每张量最多复制一次、
模型输出最终在 CPU、费用分解与模拟结果一致。任一不满足抛 `PartitionError`。

### 其他导出

`Operator`、`CPU`、`NPU`、`DEVICES`、`MAX_OPERATORS`、
`topological_order(operators)`（确定性拓扑序，基于最小堆按声明先后出队）、
`PartitionError`、`NoFeasiblePartitionError`。

## 使用示例（输出为真实运行结果）

```python
from edge_partitioner import CPU, NPU, Problem, partition

problem = Problem.build(
    operators=[
        {"name": "a", "inputs": ["x"], "output": "t",
         "costs": {CPU: 1, NPU: 100}},
        {"name": "b", "inputs": ["t"], "output": "u",
         "costs": {CPU: 100, NPU: 1}},
        {"name": "c", "inputs": ["t"], "output": "v",
         "costs": {CPU: 100, NPU: 1}},
        {"name": "d", "inputs": ["u", "v"], "output": "w",
         "costs": {CPU: 1, NPU: 100}},
    ],
    model_inputs=["x"],
    model_outputs=["w"],
    copy_time={"x": 1, "t": 6, "u": 2, "v": 2, "w": 1},
)
sol = partition(problem)
```

真实输出（`python3 -m <脚本名>`，Python 3.11.6）：

```text
assignment: {'a': 'cpu', 'b': 'npu', 'c': 'npu', 'd': 'cpu'}
copies: [('t', 'cpu', 'npu', 6), ('u', 'npu', 'cpu', 2), ('v', 'npu', 'cpu', 2)]
steps: [('a', 'compute', 'cpu', 1), ('t', 'copy', 'cpu', '->', 'npu'),
        ('b', 'compute', 'npu', 1), ('c', 'compute', 'npu', 1),
        ('u', 'copy', 'npu', '->', 'cpu'), ('v', 'copy', 'npu', '->', 'cpu'),
        ('d', 'compute', 'cpu', 1)]
compute/copy/total: 4 10 14
```

要点：张量 `t` 被 NPU 上的 `b`、`c` 两个算子同时消费，但只产生 **一条**
CPU→NPU 拷贝（6），不存在按边重复计费；`w` 在 CPU 本地产生，无需回传。

## 复现步骤

环境：Python **3.11.6**，仅标准库。在工作区根目录执行：

```bash
# 标准方式（测试可从根目录被自动发现）
python3 -m unittest discover -s tests -p 'test_*.py' -v

# 隔离模式复核（不读取用户站点包与环境变量）
python3 -I -m unittest discover -s tests -p 'test_*.py' -v
```

最近一次完整运行结果：**Ran 19 tests … OK**（19 个测试全部通过）。

## 验证范围

测试文件 `tests/test_partitioner.py` 共 19 个用例，分四组：

1. **正常场景（9 个）**
   - 单算子 NPU 胜出：输入 CPU→NPU 与输出 NPU→CPU 两个方向都计费；
   - 单算子 CPU 胜出时零拷贝；
   - 链式混合分图的拷贝方向与可执行次序；
   - 同设备分叉：被两个 NPU 消费者共享的张量不产生跨设备拷贝；
   - 跨设备分叉：一张量只复制一次、多消费者共享；
   - 模型输出读取与算子读取复用同一拷贝（不重复收费）；
   - 模型输入兼作模型输出的透传场景；
   - 独立分叉链的交错调度；全并列时确定性任选（CPU 优先）。
2. **边界场景（6 个）**
   - 全零耗时；14 算子链（规模上限）与暴力参照对账；
   - 算子不支持某设备（`None`）时的强制分配；
   - 拓扑序确定性；篡改 `total_time`、伪造拷贝条目均被 `validate_solution` 拒绝。
3. **输入校验（3 个，含 14 个 subTest）**
   - 空图/超 14 算子、重名、重复输出、未知输入/输出、`copy_time` 缺失或
     含未知键、负耗时、布尔值冒充整数、设备键缺失、全设备不可执行、
     输入输出同名、算子输入重复、空模型输入、图中有环。
4. **交错随机场景（1 个，60 个随机 DAG 子例）**
   - 固定随机种子生成 1..8 算子的随机 DAG（随机拓扑、随机费用、随机
     `None` 不可用设备、随机输出集合），每个解都与测试内**独立重写**的
     暴力枚举结果对账，并通过独立的步骤模拟器与 `validate_solution`
     双重校验。

说明与限制：

- 全部验证均基于上述真实单元测试；不存在预置输出替代真实功能验证的情况。
- 随机测试使用固定种子（`random.Random(20261122)`），结果可复现。
- 14 算子稠密 DAG 的全枚举实测约 0.5 秒（本机），与 `2**14` 次枚举的设计
  规模相符；该数字来自一次性规模检查脚本，脚本未保留在交付物中。
- 仅操作本地合成数据；不访问网络、不安装依赖、不读取任何凭据。
