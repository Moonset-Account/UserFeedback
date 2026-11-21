# 变更窗口排程模块 (change_scheduler)

为生产变更系统提供排程：给定按固定顺序执行的若干步骤，每步有不可中断的
执行时长、执行窗口，以及对应回退动作的时长与值守窗口，求 **最晚可开工时刻**、
一份 **正向安排**，以及 **每一步失败时的逆序连续回退安排**。

- Python 3.11.6，仅用标准库，无第三方依赖。
- 所有时间为一整天内的整数分钟；所有区间为 **左闭右开** `[start, end)`。

## 问题约定

1. 步骤顺序固定；每步正向执行 `duration` 分钟，且整个过程必须完整落在其
   执行窗口 `window` 内。
2. 步骤之间 **允许等待**。
3. 所有正向步骤最迟在 `deadline` 时刻完成，即最后一步满足 `end <= deadline`
   （截止时刻本身允许）。
4. 每步 **结束** 时（含最后一步）都可能宣布失败。从该时刻起，必须 **立刻**
   按 **逆序**、**连续**（无空隙）回退所有已执行步骤。
5. 每个回退动作 `rollback_duration` 必须完整落在自己的值守窗口 `duty_window` 内。
6. 只有正向窗口合适、但第一步做完就赶不上回退值守的情况，同样判为
   **不可放行**（`feasible=False`）。

## 公开 API

```python
from change_scheduler import Interval, Step, plan_change, verify_plan

plan = plan_change(steps, deadline)
```

### 数据类型（`change_scheduler.model`）

| 名称 | 说明 |
| --- | --- |
| `Interval(start, end)` | 左闭右开整数分钟区间；构造时校验 `end > start >= 0`，拒绝 `bool`/`float`。 |
| `Step(duration, window, rollback_duration, duty_window, name="")` | 一步的输入。 |
| `ForwardStep(step_index, name, start, end)` | 正向安排中的一步（`step_index` 从 1 起）。 |
| `RollbackAction(step_index, name, start, end)` | 回退动作的具体落位。 |
| `FailurePlan(failed_step, failure_time, rollbacks)` | 某步失败时的完整逆序回退计划；`rollbacks` 按逆序排列。 |
| `ChangePlan(feasible, start_time, deadline, forward, failures, reason)` | 排程结果。 |

### `plan_change(steps, deadline) -> ChangePlan`

- 可行：`feasible=True`，`start_time` 为 **最晚** 可开工时刻，`forward` 为正向
  安排，`failures` 为每个失败点（1..n）的回退计划。
- 不可行：`feasible=False`，`start_time=None`，`forward`/`failures` 为空，
  `reason` 说明阻塞原因。
- 空变更视为可行，`start_time = deadline`。
- **输入畸形**（类型错误、区间非法等）才抛 `TypeError`/`ValueError`；
  真正的排程不可行通过返回值表达，不抛异常。

### `verify_plan(steps, deadline, plan) -> list[str]`

独立复核一份计划是否满足全部约束（窗口、截止、顺序、逆序连续回退、值守窗口）。
返回问题列表，空列表表示通过。`plan_change` 返回前会调用它做自检。

## 算法（为何精确）

设第 `i` 步计划结束于 `e_i`、时长为 `d_i`。在第 `i` 步结束处失败时，逆序连续
回退从 `e_i` 开始。记后缀和 `R(j)=Σ_{m=j..i} r_m`，则第 `j` 步回退占
`[e_i+R(j+1), e_i+R(j))`。要求其落在 `duty_j` 内得到 `e_i` 的闭区间
`[L_i, U_i]`（对 `j=1..i` 取交集）。

正向约束为 `e_i <= window_i.end`、`e_i - d_i >= window_i.start`、
`e_i <= e_{i+1} - d_{i+1}`、`e_n <= deadline`——除每步下界外均为 `e_i` 的
上界。要最大化开工时刻 `e_1 - d_1`，即最大化每个 `e_i`，用一次 **从后向前**
的扫描 `e_i = min(window_i.end, e_{i+1}-d_{i+1}, U_i)` 即得。若某 `e_i` 低于
`max(L_i, window_i.start + d_i)`，则整体无解（不可放行）。复杂度 O(n²)。

## 复现步骤

在工作区根目录执行：

```bash
# 运行全部单元测试（含 800 组随机用例与穷举法交叉验证）
python3 -m unittest discover -s tests -v

# 命令行：读取 JSON 问题，输出真实排程结果（不伪造）
python3 -m change_scheduler examples/feasible.json
python3 -m change_scheduler examples/not_releasable.json
```

JSON 输入格式：

```json
{
  "deadline": 100,
  "steps": [
    {"name": "a", "duration": 10, "window": [0, 100],
     "rollback_duration": 5, "duty_window": [0, 200]}
  ]
}
```

## 验证范围（真实测试结果）

- 工作区根目录执行 `python3 -m unittest discover -s tests -v`，**28 项测试全部通过**，
  其中包括 **800 组随机用例与独立穷举搜索交叉验证**（含可行偏向生成器，>200 组可行用例）。
  覆盖：

- **正常场景**：单步/多步、截止与窗口分别成为约束、步骤间等待、空变更。
- **回退约束**：最后一步失败逆序连续回退、每个失败点均生成计划、值守窗口
  收紧导致正向前移/后移、回退时长超过值守窗口。
- **边界**：左闭右开上界独占、恰好填满、`deadline` 含等于、零时长步骤、
  非法区间构造。
- **题目显式要求**：正向窗口合适但第一步做不完就赶不上回退值守 → 判为不可放行。
- **输入校验**：负时长、`bool`/`float` 冒充整数、非 `Step` 对象、非法 deadline。
- **交叉验证**：800 组随机小规模用例（混合 + 可行偏向两套生成器），与独立穷举搜索
  的“最晚开工时刻”逐一比对；另在开发过程中跑过 7000 组更大规模抽查，零不符。
- **CLI**：读文件、可行与不可行两类输出。

未执行项：本例未涉及分布式/持久化或真实环境联调，故无此类测试。

## 文件结构

```
change_scheduler/
  __init__.py     # 公开 API 导出
  model.py        # 数据模型与输入校验
  scheduler.py    # 排程算法 + verify_plan 独立校验
  __main__.py     # 命令行入口
tests/
  test_scheduler.py
examples/
  feasible.json
  not_releasable.json
```
