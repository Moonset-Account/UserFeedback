# 束管理器（Beam Manager + 分页 KV 搬移调度）

纯 Python 3.11 标准库实现，无第三方依赖。面向推理后端逐时间步返回的候选，
完成两件事：

1. **束选择**：按累计分数与稳定编号选出固定宽度的存活束，结束假设单独维护；
2. **分页 KV 调度**：在固定物理槽（家槽 + 堆槽 + 唯一临时槽）上，生成可逐步
   执行的搬页 / 复制 / 追加计划，处理写时复制、重排环，并在容量不足时于任何
   覆写发生之前拒绝。

## 目录结构

```
beam_manager/
  __init__.py     # 公开 API 汇总导出
  planner.py      # 物理页、页操作、PhysicalMemory 执行器、build_step_plan 规划器
  manager.py      # Candidate / Beam / StepPlan / BeamManager
tests/
  test_planner.py            # 规划器单元测试（环、链、容量、源槽保护、随机化）
  test_manager.py            # 选择语义、稳定编号、计划/执行分离、翻页、容量
  test_three_step_scenario.py# 三个连续时间步端到端核对（重排环→同父分叉→早结束）
```

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

预期：`Ran 22 tests ... OK`。

## 公开 API

### `beam_manager.manager`

| 名称 | 说明 |
| --- | --- |
| `Candidate(parent_beam_id, token, score, ended=False)` | 后端单步候选；`parent_beam_id` 是父束**稳定编号**，与父束在后端数组中的位置无关 |
| `Beam` | 不可变束假设：`beam_id`、`parent_beam_id`、`token`、`score`、`ended`、`page_ids`（根→末页逻辑链）、`home_slot`；`trace_tokens(registry)` 沿稳定父链回溯完整 token |
| `StepPlan` | 单步可执行计划：有序 `ops`、`active_beams`、`finished_beams`、`ranked_candidates`、`admitted_indices`、`slots_before/after` |
| `BeamManager(width, heap_slots, block_size=16)` | 束管理器 |

`BeamManager` 关键方法：

- `plan_step(candidates) -> StepPlan`：**只规划**，物理内存与束状态均不变；
- `execute_plan(plan)`：逐操作执行计划并提交束状态；
- `step(candidates) -> StepPlan`：规划 + 执行合一；
- `trace(beam_id)`：某束从根到当前的完整 token 序列；
- `page_tokens(pid)` / `slot_of(pid)`：物理页内容与所在槽位；
- 属性 `active_beams`、字段 `finished`（按 `(-score, beam_id)` 排序）。

### `beam_manager.planner`

| 名称 | 说明 |
| --- | --- |
| `PageOp`（基类）/ `DropPage` / `MovePage` / `CopyPage` / `AppendPage` | 不可变页操作；后端一次只能移动一页，故全部排成线性序列 |
| `PhysicalMemory(width, heap_slots)` | 物理内存与逐条执行器，对每个操作做物理断言（空槽、占用者、源页存在性） |
| `ChildSpec(new_pid, source_pid, home, token)` | 规划器输入：一个新末页需求 |
| `build_step_plan(...)` | 纯函数调度规划器，返回有序 `PageOp` 列表 |
| `CapacityError` | 容量不足 / 临时槽被占用；抛出前不产生任何覆写 |

槽位编号：`0 .. width-1` 为家槽，`width .. width+heap_slots-1` 为堆槽，
`width+heap_slots` 为唯一临时槽。

## 关键语义

### 束选择（与后端返回顺序解耦）

排序键依次为：累计分数降序 → 父束稳定编号升序 → token 升序 → 后端列表下标升序。
单遍扫描：

- 非结束候选依次录取为存活束，直到达到固定 `width`；
- 结束候选只有落在前 `width` 个排名位（本可占据固定名额）才进入结束假设，
  不占存活槽位，其名额由后续非结束候选按排名**递补**；前 `width` 名之外的
  结束候选直接丢弃；
- 候选整体不足时存活束可以少于宽度（极端情况下可为 0）。

束编号与页编号均单调递增、永不复用。

### KV 页生命周期

- **同父多孩 → 写时复制（COW）**：共享只读历史页（含父末页）保持不动，
  每个孩子的新末页先 `CopyPage` 到独立可写位置再 `AppendPage`；
- **独占孩子**：直接继承父末页，仅在需要换槽时 `MovePage`；
- **父末页写满**（达到 `block_size`）：孩子分配全新页（`AppendPage(fresh=True)`）；
- **结束假设**：末页落堆槽，其全部历史页永久保留（`retained_pids`），
  参与后续容量核算，绝不被误回收。

### 调度顺序与安全保证

每步操作严格按以下阶段线性排列，可逐条 `PhysicalMemory.apply` 执行：

1. `DropPage`：引用归零的死页最先淘汰，释放槽位；
2. `MovePage`：旧页迁移。重排链从自由端推进；重排环用唯一临时槽打破
   （环首入临时槽 → 沿空槽缺口正向顺移 → 环首接回目的槽），调度结束
   临时槽必然恢复为空；
3. `CopyPage`：所有迁移完成后才复制，保证 COPY 目的槽不可能是任何未读源槽；
4. `AppendPage`：最后在独立可写末页上写入新 token。

`PhysicalMemory` 在执行每条操作时物理断言“目的槽为空 / 源槽占用者正确”，
因此“目标槽恰是另一个孩子还未读的源槽”这类交错在执行层会被直接拦截；
规划层则通过上述阶段顺序从根本上避免该情形。

容量核算在任何操作生成前完成：所需常规槽（临时槽除外）不足、结束假设无堆槽、
或步骤开始时临时槽被占用，均抛 `CapacityError`，调用方物理内存保持原样。

## 验证范围（本地合成数据）

全部为离线合成数据，无网络、无真实目标、无第三方依赖。

- `test_three_step_scenario.py`：题面要求的三个连续时间步
  - 步 0 根束三分叉；
  - 步 1 **重排环**：独占继承的末页家槽旋转，断言临时槽恰好使用一次、
    环首选择、物理槽布局与每页 token；
  - 步 2 **同父分叉 + 早结束**：断言 COW 独立末页、共享源内容冻结、
    结束假设落堆槽且不占家槽、淘汰候选不落页、存活名额递补；
  - 步 3 分叉后再次形成重排环，并核对结束假设内容在后续步中持续冻结；
  - 每个候选均核对稳定父链 token 回溯与物理页内容。
- `test_planner.py`（9 项）：三环、真链（自由端）、DROP 优先、临时槽占用
  拒绝、源槽写前保护（含目的槽即源槽的交错）、容量拒绝时内存不变、
  40 组随机化多轮规划（执行器物理断言兜底）。
- `test_manager.py`（10 项）：分数/稳定编号并列排序、乱序输入结果完全一致、
  结束假设分离与递补、全结束边界、计划不修改内存、逐步可执行、写满翻页、
  容量拒绝后状态可恢复、非法父束校验、源槽即目的槽的管理器级交错。

## 设计边界与如实说明

- 页内容以 token id 序列充当 KV 载体（真实 KV 张量可平替为页内数据结构，
  调度逻辑不变）。
- 随机化测试固定种子，可复现；它验证“规划序列总能通过物理执行断言”，
  不替代端到端数值正确性。
- 未实现真实 GPU 搬运与并发；所有“搬页”为内存内线性操作序列，逐步执行
  语义与后端“一次只能移动一页、临时页只有一个”的约束一一对应。
