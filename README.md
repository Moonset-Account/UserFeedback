# 参数组调度组件（param_scheduler）

训练运行器使用的参数组学习率调度器。它不执行训练，只负责**计划 / 确认 / 状态管理**：

- 训练可以暂停（freeze）一部分参数，之后按**参数身份**把它们加入指定学习率组；
- 每个组给出按**参数自身有效更新次数**取值的学习率安排，暂停中的参数不计步；
- 以**步编号**确认完成：确认时更新参数计数并同时产出下一个步的计划；
- **重复确认同一步沿用首次结果**（幂等），不重复计数；
- 断点恰好发生在“参数已调组、该步尚未确认”时，恢复后从参数**已有的计数**在新组取学习率；
- 第一次解冻且从未更新的参数，从**第一步**（`schedule(0)`）开始算。

仅使用 Python 3.11 标准库，无第三方依赖。

## 目录结构

```
workspace/
├── param_scheduler/
│   ├── __init__.py        # 公开 API 导出
│   └── scheduler.py       # Scheduler / StepPlan / StepResult / SchedulerError
├── tests/
│   └── test_scheduler.py  # 23 个 unittest 用例
├── examples/
│   └── smoke_demo.py      # 端到端合成数据冒烟示例（可 -I 隔离运行）
└── README.md
```

## 核心语义

### 有效更新次数与学习率取值

- 参数注册后计数从 0 开始；只有在某一步被 `confirm_step` 确认、且当时未暂停，计数才加 1。
- 第 `n` 次有效更新的计划学习率为 `schedule(n)`：
  - **序列 schedule**：按顺序取值，如 `[1.0, 0.5, 0.25]`；计数超出长度后停在最后一个值；
  - **可调用 schedule**：`update_count -> lr`，如 `lambda n: 0.9 ** n`。
- 暂停中的参数不出现在计划的 `learning_rates` 中，而是出现在 `frozen` 列表里，计数保留不变。

### 步的生命周期

1. `plan_step()` 取得下一步的只读计划（当前未暂停参数的学习率 + 暂停参数列表）；
2. 训练执行该步；
3. `confirm_step(step)` 确认完成：未暂停参数计数 +1，缓存结果，并在结果中给出 `next_plan`；
4. 步编号必须从 1 开始连续；重复确认已完成的步直接返回缓存的 `StepResult`。

### 调组与断点恢复

- `move_param(pid, group)` 按参数身份迁移，**计数与暂停状态跟随参数保留**。
- 若调组发生在某步确认之前而进程中断，保存的状态里参数已属于新组；恢复后
  `plan_step()` 自然按它在新组中已有的计数取学习率。
- `state_dict()` 的输出可直接 `json.dumps` 落盘；可调用 schedule 的代码无法序列化，
  恢复时通过 `Scheduler.from_state(state, schedules={...})` 重新提供。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `Scheduler(groups: Mapping[str, Schedule])` | 构造调度器，组名 -> 学习率安排 |
| `add_group(name, schedule)` | 动态新增学习率组 |
| `add_param(param_id, group, *, frozen=False, update_count=0)` | 按身份注册参数到组 |
| `freeze_param(param_id)` / `unfreeze_param(param_id)` | 暂停 / 解冻（均幂等） |
| `move_param(param_id, group)` | 按参数身份调组，计数与暂停状态保留 |
| `plan_step(step=None) -> StepPlan` | 生成下一步（或指定待确认步）的计划快照 |
| `confirm_step(step) -> StepResult` | 确认步完成，推进计数并产出下一步计划；重复确认返回缓存 |
| `get_result(step) -> StepResult` | 读取已确认步的结果 |
| `is_confirmed(step)` / `confirmed_steps()` | 完成情况查询 |
| `get_count(pid)` / `get_group(pid)` / `is_frozen(pid)` / `current_lr(pid)` | 参数状态查询 |
| `next_step` / `last_completed_step` | 步游标属性 |
| `state_dict()` / `load_state_dict(state)` / `Scheduler.from_state(state, schedules=None)` | 状态保存与恢复 |
| `StepPlan` | 不可变快照：`step`、`learning_rates`、`frozen` |
| `StepResult` | 不可变快照：`step`、`updated`、`learning_rates`、`counts_after`、`frozen`、`next_plan` |
| `SchedulerError` | 所有可预期错误（非法输入、不连续步、状态不一致等） |

`param_id` 可以是任意可哈希对象；需要 JSON 落盘时推荐使用字符串身份（如参数名）。

## 运行方法

```bash
# 单元测试（题面指定方式）
python3 -m unittest discover -s tests -v

# 端到端冒烟示例（隔离模式，不读取当前目录的第三方模块）
python3 -I examples/smoke_demo.py
```

## 验证范围

全部依据本地合成数据的实际执行与断言（23 个用例，见 `tests/test_scheduler.py`）：

- **schedule 语义**：序列与可调用两种安排均按参数自身计数取值；序列耗尽停在末值；
  计划/结果为不可变快照，后续变更不污染历史结果。
- **暂停/解冻**：暂停参数不计步且计数保留；解冻后按保留计数续算；注册即暂停、
  从未更新的参数首次解冻从 `schedule(0)` 起步；中途新注册的零计数参数同样从第一步起步；
  暂停/解冻幂等。
- **调组**：计数跟随参数身份进入新组并按新组 schedule 取值；暂停中调组保留暂停状态；
  同组迁移为空操作。
- **步确认**：确认同时完成计数更新与下一步计划；重复确认返回同一结果对象且不重复计数；
  跳步、回退、非整数/非正步编号均被拒绝；无参数时步游标仍正常推进。
- **断点恢复**：覆盖“调组后、确认前崩溃”“暂停中调组后崩溃”两类断点；
  `state_dict` 经 `json.dumps/loads` 往返后用 `from_state` 重建，历史步结果、
  计数、暂停状态与后续训练行为完全可复现；`load_state_dict` 路径核对组结构、
  类型与序列取值，损坏状态（版本号、组不匹配、可调用组缺失、非映射输入等）被拒绝。
- **交错场景**：两个参数交替暂停/解冻/调组，穿插重复确认与检查点重建，
  断言每一步的学习率、计数与越界钳制行为。

## 边界与约定

- 步编号从 1 开始且必须连续，不支持乱序确认；历史步不可重新出计划。
- 序列 schedule 不允许为空；组名必须为非空字符串；`param_id` 必须可哈希、组内/调度器内唯一。
- `load_state_dict` 要求恢复前已注册同名同类型的组；可调用组的代码逻辑由调用方保证与保存前一致。
- 本组件仅实现题面规定的调度与防御性状态校验，不含任何训练执行、网络访问或利用性代码。
