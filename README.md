# 成组 Pod 放置模块（group_placement）

在最多 **6 台节点**上为最多 **8 个 Pod** 的整组工作负载寻找一种完整落位方案。
模块只依赖 Python 3.11 标准库，无第三方依赖、无网络访问。

## 资源模型与生命周期

每个 Pod 由按顺序启动的初始化容器和最后统一启动的业务容器组成：

1. **普通初始化容器**：单独运行（已常驻的初始化容器除外），退出后才启动下一个；
2. **常驻初始化容器**（`resident=True`）：从启动起一直运行，与后续容器共存；
3. **业务容器**：所有普通初始化容器退出后，连同全部常驻初始化容器一起运行。

对每个生命周期阶段（每个初始化容器启动点 + 业务容器统一启动点
`app:all`）计算该时刻的**最大同时用量**。Pod 的预留峰值为：

```
某资源峰值 = 各阶段该资源最大同时用量 + Pod 该资源固定开销
```

CPU 与内存的峰值可能出现在不同阶段，因此分别返回峰值与对应阶段；同值时保留最早阶段。

放置采用**基于请求量的预留**：节点上各 Pod 的 CPU 峰值之和、内存峰值之和
分别不超过节点剩余 CPU、剩余内存时才算放得下。每个 Pod 只能落在
`eligible_nodes` 列出的节点上。

## 公开 API

全部从 `group_placement` 包导入：

| 名称 | 说明 |
| --- | --- |
| `Container(name, cpu, memory, kind="app", resident=False)` | 容器；`kind` 为 `"init"` 或 `"app"`，仅 init 可常驻 |
| `Pod(name, inits=(), apps=(), cpu_overhead=0.0, memory_overhead=0.0, eligible_nodes=())` | 不可变 Pod 描述，容器字段均为元组 |
| `Node(name, cpu_available, memory_available)` | 不可变节点及其剩余资源 |
| `compute_pod_peak(pod) -> PodPeak` | 单 Pod 双资源峰值、对应阶段与全阶段用量明细 |
| `place_group(pods, nodes) -> PlacementResult` | 成组放置主入口 |
| `Stage / StageUsage / PodPeak / NodeResidual / PlacementResult` | 结果数据类（均为 frozen dataclass） |
| `PlacementInputError` | 输入结构/数值非法时抛出 |
| `MAX_PODS=8`、`MAX_NODES=6`、`EPSILON=1e-9` | 规模上限与浮点容差 |

`PlacementResult` 字段：

- `success: bool`：整组是否全部放下；
- `peaks: dict[str, PodPeak]`：无论成败都返回每个 Pod 的两项峰值与阶段；
- `placement: dict[str, str]`：Pod 名 → 节点名，失败时为空；
- `residuals: dict[str, NodeResidual]`：各节点放置后（或失败时原样）的余量；
- `reason: str | None`：失败原因。

## 回溯策略与无解保证

`place_group` 使用深度优先回溯，并在每一步动态选择**当前可行节点最少**
的 Pod（MRV 启发式，平局时优先 CPU 峰值更大者）。因此当“前一个 Pod 有
两个去处、后一个只能用其中一个”时，受约束更强的后者先落位，前者不会
贪心抢占唯一节点；一旦某分支走不通，已做的占用会回滚后尝试下一节点。

搜索过程中**从不修改 `Node` 对象**：已占用量保存在局部字典里随回溯回滚。
整组无解时：

- `success=False`，`placement={}`；
- `residuals` 中每台节点的余量与输入完全一致；
- `peaks` 仍然填充，便于调用方排查。

## 目录结构

```
group_placement/
    __init__.py      # 公开 API 导出
    models.py        # Container / Pod / Node 不可变模型
    peaks.py         # 生命周期阶段与双资源峰值计算
    placement.py     # MRV 回溯放置引擎与结果数据类
    validation.py    # 输入校验与规模上限
tests/
    __init__.py
    test_peaks.py    # 峰值/阶段计算测试（8 个）
    test_placement.py# 放置、回溯、无解、校验测试（20 个）
README.md
```

## 复现步骤

在工作区根目录（Python 3.11.6）执行：

```bash
python3 -m unittest discover -s tests -v
```

预期输出末尾为 `Ran 28 tests ... OK`。也可运行单个模块：

```bash
python3 -m unittest tests.test_peaks -v
python3 -m unittest tests.test_placement -v
```

## 验证范围（实际执行，非预置输出）

测试由 `unittest` 从工作区根目录发现并真实执行，最后一次运行结果：
**28 个测试全部通过（OK）**。覆盖场景：

- 正常场景：多 Pod 完整落位、资格节点约束、同节点共享、余量计算；
- 峰值语义：普通 init 不重叠、常驻 init 与后续容器重叠、业务容器齐启、
  固定开销叠加、CPU/内存峰值分属不同阶段、运行中容器清单随生命周期变化；
- 边界场景：空 Pod 组、零用量 Pod、容量恰好放满（含浮点边界容差）、
  8 Pod × 6 节点上限、无资格节点、仅内存维度不足；
- 交错/回溯：两 Pod 两去处 vs 唯一去处的贪心抢占回滚、三 Pod 双资源
  交错分配、确定性（重复调用结果一致）；
- 无解保持：失败时 `residuals` 与输入逐字段相等，且 `Node` 对象本身未被修改；
- 输入校验：超规模、未知资格节点、重名、负数/NaN、非法容器类型、
  业务容器误标常驻均抛出 `PlacementInputError`。

未覆盖/未执行项：无（未使用真实集群、网络或第三方组件；全部为本地合成数据）。
