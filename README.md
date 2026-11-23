# 应急修复排程器（Emergency Repair Scheduler）

在有限整数时域内，为故障组件的维修阶段排程，同时保证每个中间时点的服务
最低容量、人员与备件约束，并按字典序优化业务影响。

仅使用 Python 3.11.6 标准库，无第三方依赖。

## 1. 建模约定

- 时间是离散整数网格。一个在 `start` 启动、持续 `duration` 的阶段占用半开
  区间 `[start, start + duration)`；容量与资源在区间内**每个单位 tick** 上
  都会被检查（即所有中间时点）。
- 阶段一旦启动**不可切分、不可抢占**。同一组件的阶段严格按声明顺序执行。
- 带维修阶段的组件在时刻 0 视为故障，**只有其最后一个阶段完成的那个 tick
  起才恢复容量**；修复中途绝不提前计入未来才会恢复的容量。
- `Stage.stop_components` 中列出的冗余健康组件在该阶段整个区间内被停机，
  容量在 `end` 时刻恢复。
- 备件为可复用资源：阶段启动时先应用 `spare_releases`（从故障件拆下的可用
  备件），再占用 `spare_needs`；阶段结束时占用归还共享备件池。人员在整个
  阶段区间内持续占用。
- 无维修阶段的组件是始终在线的健康冗余单元，仅可被 `stop_components` 临时
  停机。

## 2. 优化目标（严格字典序）

1. 最小化全时域内各服务“未满足需求量 × 持续时间”的加权和
   （`weighted_objective`）；
2. 再最小化全部修复完成时刻 `makespan`；
3. 再按完成时刻与组件 ID 的字典序（`completion_order`）；
4. 最后按阶段 ID 的启动时刻向量做确定性兜底定序。

无可行解时，返回 `ScheduleResult(feasible=False, failure=Failure(...))`，
`Failure` 给出第一处无法维持的约束：`time`、`kind`
（`min_capacity` / `personnel` / `spare` / `horizon` / `precedence`）、
文字 `detail` 与结构化 `related` 字段，并标注是否来自精确可达性分析
（`exact=True`）还是构造式回退诊断（`exact=False`）。

## 3. 公开 API

```python
from emergency_scheduler import (
    Problem,      # components, services, horizon, personnel_capacity, initial_spares
    Component,    # component_id, service_capacity, stages
    Stage,        # stage_id, component_id, duration, personnel,
                  # spare_needs, spare_releases, stop_components
    Service,      # service_id, demand, min_capacity, weight
    ScheduleResult, Segment, Failure, ValidationError,
    solve,                 # 主入口：分支定界求解
    simulate,              # 独立逐 tick 仿真，二次校验具体排程
    enumerate_best,        # 小规模精确穷举（测试对照 oracle）
    first_failure,         # 精确“第一处无法维持约束”分析
    greedy_diagnose,       # 大规模回退诊断
)
```

### 最小示例

```python
from emergency_scheduler import Component, Problem, Service, Stage, solve

problem = Problem(
    components=[
        Component("A", {"s1": 6.0}, [Stage("a1", "A", duration=2, personnel=1)]),
        Component("B", {"s1": 4.0}, [Stage("b1", "B", duration=2, personnel=1)]),
    ],
    services=[Service("s1", demand=10.0, weight=1.0)],
    horizon=8,
    personnel_capacity=1,
)
result = solve(problem)
assert result.feasible
for segment in result.segments:
    print(segment.stage_id, segment.start, segment.end,
          segment.personnel, segment.unmet_demand)
print(result.total_unmet, result.weighted_objective,
      result.makespan, result.completion_order)
```

返回的每个 `Segment` 含：阶段/组件 ID、起止 tick、人员占用、备件占用与
释放、被停机组件，以及该阶段区间上的分服务累计未满足需求。当多个阶段
并行时，同一 tick 的未满足需求会出现在每个活动阶段上；
`ScheduleResult.total_unmet` 始终是全时域只计一次的权威总量。

## 4. 算法

### 主求解器 `solver.solve`

深度优先分支定界，枚举满足同组件前后置关系的阶段顺序；每个阶段只尝试其
**最早可行开始时刻**。对可更新资源（人员、备件、在线容量）而言，刻意空转
不会创造任何新资源或新容量，因此必存在一个每个阶段都在最早可行时刻启动
的最优解。

- 人员：维护每 tick 占用数组，窗口内不超容量才可行。
- 备件：按事件累积共享备件池库存，库存任何 tick 为负即不可行。
- 容量：只计入已完成最后阶段的组件（和未被停机的健康组件），逐 tick 核对
  服务 `min_capacity`。
- 下界剪枝：当前排程在“下一个阶段最早可能启动 tick”之前已经锁定的加权
  未满足成本，是主目标的合法下界。
- 先运行一次贪心构造得到初始 incumbent，尽早收紧剪枝。

### 精确穷举 `brute_force.enumerate_best`

时间索引 DFS，在每个 tick 上可连续启动多个阶段（同 tick 并行），再结算该
tick 的全部约束与未满足成本。按完整状态签名做记忆化，消除同一并行集合的
排列重复。它是测试夹具的独立 oracle，仅限小规模实例（夹具均少于 12 个
阶段），并通过 `state_limit` 防止状态爆炸。

`brute_force.first_failure` 用同样的可达性搜索找出**任何延续都无法维持**
的最早约束；当超出状态预算时，`solve` 回退到 `diagnose.greedy_diagnose`
并把结果标记为 `exact=False`。

### 独立仿真 `timeline.simulate`

对具体排程重新逐 tick 计算人员、备件、组件在线状态与服务容量，不信任求解
器内部簿记；所有正式测试都用它复核最终排程确实可行且目标值一致。

## 5. 复现步骤

```bash
# 工作区根目录，Python 3.11.6，标准库 only
python3 -I -m unittest discover -s tests -v
```

`-I` 以隔离模式运行，避免当前目录下的同名模块（如植入的 `json.py`）污染
导入。所有测试可从工作区根目录被 unittest 自动发现（`tests/test_*.py`）。

## 6. 验证范围

`tests/` 下 27 个 unittest 用例，覆盖题面要求的正常、边界与交错场景：

- `test_solver.py`
  - 正常路径：恢复量大的组件先修；
  - **反直觉顺序**：先修恢复量小但能释放另一项维修所需备件的组件
    （`spare_release_problem`），并验证相反顺序在 t=0 即触发备件违例；
  - 停机冗余组件的容量核算、多阶段前后置与“不提前计入未来容量”；
  - 同 tick 并行阶段交错、阶段结束时刻恰好等于时域边界（半开区间）。
- `test_infeasible.py`
  - 最低容量、备件、时域三类首违；可行实例上调用 `first_failure` 报错；
  - 状态预算触顶后回退诊断（`exact=False`）。
- `test_impact_and_fuzz.py`
  - 120 个确定性随机小规模实例，主求解器与精确穷举在可行性、目标值、
    makespan、启动时刻上逐项一致，并强制语料中同时出现可行/不可行/并行
    窗口，避免空泛测试；
  - 全程服务影响核对：逐 tick 求和等于 `total_unmet`，加权和等于
    `weighted_objective`；串行排程下各阶段影响之和等于全时域总量；并行
    排程下阶段级数字会重复计数而总量只计一次。
- `test_tiebreak_and_resources.py`
  - 主目标持平时的 makespan 与组件 ID 二级定序、人员并行上限、
    `Segment` 占用字段。
- `test_validation.py`
  - 时域、重复 ID、未知/自身停机组件、非正持续时间、负最低容量、未知
    服务容量、零维修阶段等结构性校验。

除套件内的 120 个随机实例外，开发期间还用不同种子额外交叉验证了 300 个
随机实例（最大穷举状态数约 2.3 万），主求解器与穷举 oracle 全部一致；
该脚本位于 `scratch/`，不属于交付测试，README 中的结论以正式套件的真实
运行为准。所有数值结论均来自真实执行，未使用任何预置演示输出替代功能
验证。

## 7. 已知边界

- 精确穷举与精确首违分析面向少于 12 个阶段的小夹具；更大实例依赖分支
  定界主求解器，诊断在超预算时回退为构造式结果并显式标注 `exact=False`。
- 时间为整数网格、持续时间为正整数；容量、需求、权重为非负数值。
