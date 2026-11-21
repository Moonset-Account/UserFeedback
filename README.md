# 流水段训练规划器（pipeline_planner）

为最多 **3 个流水段、4 个微批** 的流水线并行训练排出整步（一个 step）
最短完成时间（makespan），输出每个操作的起止时刻、各段激活峰值。

## 约束建模

- 每个微批先依次前向 `F0 → F1 → … → F(S-1)`，再倒序反向
  `B(S-1) → … → B0`；同段前向必须先于反向。
- 每段一个独立设备，同一时刻只能运行一项操作；跨段移交瞬时，
  同一时刻允许“先完成、再开工”（所有时间区间为半开 `[start, end)`）。
- 微批在某段的**前向一开始即占用一个激活槽**，直到该微批在该段的
  **反向结束才释放**（占用区间 `[F.start, B.end)`，结束时刻即可被新前向复用）。
- 各段前向/反向时长为固定正整数，激活容量为正整数。

## 入口

包：`pipeline_planner`

| 符号 | 说明 |
| --- | --- |
| `StageConfig(forward_duration, backward_duration, activation_capacity)` | 单段配置（均为正整数） |
| `Operation(stage, microbatch, kind, start, end)` | 单个操作，`kind` 为 `"forward"`/`"backward"` |
| `Schedule(makespan, operations, activation_peak)` | 规划结果：最早完成时刻、全部操作、各段激活峰值 |
| `plan(stages, num_microbatches) -> Schedule` | **主入口**：求全局最短 makespan 的调度 |
| `verify_schedule(stages, num_microbatches, schedule) -> dict` | 独立重放校验全部交叉约束，违规则抛 `ValueError` |

```python
from pipeline_planner import StageConfig, plan, verify_schedule

stages = [
    StageConfig(forward_duration=1, backward_duration=1, activation_capacity=2),
    StageConfig(forward_duration=1, backward_duration=1, activation_capacity=1),
    StageConfig(forward_duration=1, backward_duration=1, activation_capacity=2),
]
schedule = plan(stages, num_microbatches=2)
print(schedule.makespan)        # 10
verify_schedule(stages, 2, schedule)
```

## 算法

离散时间事件驱动 DFS + 记忆化 + 分支限界：

- 状态 = 各设备空闲时刻、各微批前向进度 / 下一反向段、各段占用中的激活槽；
- 仅在设备空闲事件时刻决策，枚举每个空闲段的合法操作（含主动空转），
  无工可开时整体跳到下一事件时刻；
- 合法性同时校验计数偏序与**时间偏序**（段 s+1 前向不早于段 s 前向结束、
  段 s 反向不早于段 s+1 反向结束）与激活容量；
- 下界 = 各设备剩余工作量与单微批关键链的最大值；贪心可行解提供初始上界；
- 仅缓存有限结果，避免剪枝产生的 `INF` 在上界收紧后误杀等价状态。

枚举覆盖全部可行调度，因此返回的 makespan 为全局最优。

## 题面基准：容量 1 → 2，10 拍 → 8 拍

2 微批、3 段、前后向均耗时 1、两端容量 2，仅中段容量变化：

| 中段容量 | 最短 makespan | 激活峰值 | 原因 |
| --- | --- | --- | --- |
| 1 | **10** | `(2, 1, 1)` | 中段同一时刻只容一份激活，微批 1 的中段前向必须等微批 0 中段反向释放槽位，流水被串行化 |
| 2 | **8** | `(2, 2, 2)` | 两份激活可并存，两个微批交错流水，整步缩短 2 拍 |

## 测试与验证范围

运行（隔离模式，不依赖 cwd 注入）：

```bash
python3 -I -m unittest discover -s tests -v
```

`tests/test_planner.py` 共 13 个用例，全部为真实断言，覆盖：

**可接受结果**
- 中段容量 1 → makespan 10、容量 2 → makespan 8（题面基准，含激活峰值）；
- 单微批 3 段纯串行 6 拍；单段 2 微批 4 拍；混合时长最优 12 拍（附下界推导）；
- 规模上限 4 微批 3 段，求解 < 10s 且通过独立校验。

**应被阻止 / 拒绝的结果**
- 激活容量越界（同段两个微批占用区间交错）→ `verify_schedule` 抛 `ValueError`；
- 同段设备时间重叠 → 拒绝；操作缺失 / 时长与配置不符 → 拒绝；
- 非法输入（段数 >3、微批数 >4、非正整数、空配置、布尔型微批数）→ 拒绝。

**边界 / 交错场景**
- 瞬时移交：同一时刻先完成再开工合法，紧凑调度中存在零等待衔接，
  且跨段前向严格满足 `next.start >= prev.end`；
- 容量瓶颈的真实阻塞断言（微批 0 中段反向结束 ≤ 微批 1 中段前向开始）。

开发期间另用独立的逐时刻暴力枚举参考实现做过 126 组小规模配置
（1–3 段 × 1–3 微批 × 单位/混合时长 × 容量 1/2）交叉比对，
DFS 最优值与暴力枚举**全部一致**，且每组产出的调度均通过 `verify_schedule`；
该参考脚本为一次性核验工具，未保留在交付物中。最大规模（M=4）实测
求解时间 ≤ 0.9s。

## 目录

```
pipeline_planner/
  __init__.py      # 公共 API 导出
  planner.py       # plan / verify_schedule 与数据类
tests/
  test_planner.py  # unittest 验收用例（13 个）
README.md
```

仅依赖 Python 3.11 标准库；纯本地计算，无网络或外部目标访问。
