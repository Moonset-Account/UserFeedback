# 容量受限的降级决策模块

机房剩余容量不足时，推荐、搜索等入口需要各自降低服务档位。多个入口的某些档位会依赖同一个昂贵组件（例如向量检索服务）；该组件只需启用一次即可供所有入口共用，只有它的**最后一个使用者**退出时，其 CPU/内存开销才会释放。

本仓库实现该决策问题的**精确求解器**（穷举 + 剪枝判定，无启发式近似）。

## 公开 API

模块：`degradation.py`（仅使用 Python 3.11 标准库）。

| 名称 | 说明 |
| --- | --- |
| `Component(name, cpu, memory, requires=())` | 共享组件。`requires` 为直接依赖的组件名，启用时递归启用。 |
| `Tier(benefit, cpu, memory, components=())` | 入口的一个档位。`benefit`/`cpu`/`memory` 为整数；`components` 为该档必需的共享组件名。 |
| `Entry(name, tiers, floor=None)` | 一个入口。`tiers` 从最优（索引 0）到最差排序，最多 3 个。`floor` 是允许降到的最低档位**索引上限**（不可再降的底线）；`None` 表示允许使用最后一个档位，`0` 表示不可降级。 |
| `solve(entries, components, *, cpu_capacity, memory_capacity) -> Plan` | 求解入口。返回最优可行方案。 |
| `Plan` | `selections`、`enabled_components`、`total_benefit`、`total_cpu`、`total_memory`、`private_cpu`、`private_memory`、`component_cpu`、`component_memory`；便捷属性 `choices`（入口名 → 档位索引）、`tier_index(name)`。 |
| `InputError` | 输入非法（含未知组件引用、依赖成环、超限、负数、非整数、重名等）。`InputError` 继承自 `PlannerError`，后者继承 `ValueError`。 |
| `InfeasiblePlanError` | 在底线与两项容量约束下无可行解。 |
| `MAX_ENTRIES = 10`、`MAX_TIERS = 3` | 题目规模上限。 |

`entries` / `components` 同时接受上述 dataclass 与普通映射（见下例）。入口的输入顺序参与确定性并列裁决。

### 目标与约束

1. 每个入口选一个不超过其底线（`tier_index <= floor`）的档位。
2. 私有开销 = 所选各档的 `cpu` / `memory` 之和。
3. 启用组件 = 所有所选档位 `components` 的传递闭包；**每个组件只计一次**开销，即使多个入口、多个档位或依赖闭包重复引用它。
4. 可行性：`私有 + 启用组件` 的 CPU ≤ `cpu_capacity` 且 内存 ≤ `memory_capacity`。
5. 优化顺序：**总收益最大** → **总 CPU（含组件）最小** → 仍并列时按输入顺序取字典序最小的档位索引向量（确定性"任选"）。

> 关于"最后使用者"：组件的开销是否计入完全由最终方案里是否还有任何入口（含传递依赖）需要它决定。某入口多降一级但仍有其他入口使用该组件时，组件开销不会消失。

### 输入格式

直接构造对象：

```python
from degradation import Component, Entry, Tier, solve

entries = [
    Entry("rec",    (Tier(100, 4, 4, ("vec",)), Tier(60, 1, 1), Tier(10, 0, 0))),
    Entry("search", (Tier(90, 3, 3, ("vec",)), Tier(50, 1, 1), Tier(5, 0, 0))),
]
components = [Component("vec", 6, 6)]

plan = solve(entries, components, cpu_capacity=100, memory_capacity=100)
print(plan.choices)               # {'rec': 0, 'search': 0}
print(plan.total_benefit)         # 190
print(plan.total_cpu)             # 13  (私有 7 + vec 6，只计一次)
print(sorted(plan.enabled_components))  # ['vec']
```

也可用纯字典描述（`tests/test_degradation.py::TestMappingCoercion` 有完整用例）：

```python
entries = {
    "rec": {"floor": 2, "tiers": [
        {"benefit": 100, "cpu": 4, "memory": 4, "components": ["vec"]},
        {"benefit": 60,  "cpu": 1, "memory": 1},
        {"benefit": 10,  "cpu": 0, "memory": 0},
    ]},
}
components = {"vec": {"cpu": 6, "memory": 6, "requires": []}}
```

## 复现步骤

环境：Python 3.11.6（`python3 --version` 应为 3.11.6）。无需安装任何依赖。

```bash
cd <workspace>                 # 仓库根目录
python3 --version              # 预期：Python 3.11.6
python3 -m unittest discover -v
```

测试位于 `tests/test_*.py`，从仓库根目录即可被 `unittest discover` 发现（`tests/` 含 `__init__.py`）。若只想运行本模块测试：

```bash
python3 -m unittest tests.test_degradation -v
```

## 验证范围

`tests/test_degradation.py` 共 57 个测试，覆盖：

- **正常场景**：容量充足取最优档；共享组件只计一次；传递依赖（含菱形依赖）不重复计费；未使用的组件不启用不计费；某项多降一级但仍有别的使用者时组件开销不消失；收益相同取总 CPU 更少者；非单调收益按字面值处理；内存成为瓶颈；单档位入口不可降；两个取值相同的档位仍能报出正确的位置索引。
- **边界场景**：容量为 0；`floor=0` 禁止降级；`floor` 取中间档；恰好 10 个入口、恰好 3 个档位；11 个入口与 4 个档位被拒绝；空入口集合；负收益；组件声明顺序相反；深依赖链；3^10 最坏规模在限时内完成（约 0.13 s）。
- **交错场景**：把底线、共享、传递依赖、CPU 与内存双约束混在一起，并与独立枚举参考实现比对具体档位与资源合计。
- **输入校验**：未知档位组件、未知依赖、自环/二元/三元依赖环、重名组件/入口、负数开销、非整数、布尔值、NegativeCapacity、越界 floor、空名字、映射键与 `name` 不一致。
- **随机交叉验证**：250 组随机问题（组件的随机 DAG、随机档位/收益/开销/底线、随机容量）与测试内独立参考枚举逐项比对收益、总 CPU、总内存与并列裁决结果；另有 80 组打乱入口顺序后验证目标值与原顺序一致。

参考实现（`_reference_solve`）与主实现相互独立：它用递归闭包和 `itertools.product` 重新枚举，不做任何共享代码复用。

**未执行/未覆盖**：未连接任何真实机房、监控或调度系统；未做大规模实证数据集测试；未安装或使用第三方依赖；未运行网络访问。全部验证均基于本题工作区内的合成数据。

## 实现说明

- `solve` 的搜索空间上界为 `3^10 = 59049` 个档位组合，直接精确枚举；每个组合计算私有开销、组件闭包并集与两类总资源，取最优键 `(收益, -总CPU)`，严格优于才替换，故并列时保留最先遇到（字典序最小）的组合。
- 依赖成环、未知依赖、未知组件引用在求解前统一校验。
- 所有开销与容量必须为非负整数；`bool` 被显式拒绝。
