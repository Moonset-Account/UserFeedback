# GPU 集群多机训练抢占规划器

在一个小规模 GPU 集群（最多 8 台节点、10 个既有作业）上，为带最晚启动时间的
新作业计算**可执行的抢占规划**：决定暂停哪些低优作业、在各自的哪个检查点
暂停、临时写盘如何在额度约束下排队、GPU 何时释放，以及新作业的实际启动时刻。
不存在可行计划时，报告**最早失败的约束**及结构化诊断细节。

仅使用 Python 3.11.6 标准库，无第三方依赖；所有数据均为本地合成数据，
不访问网络、真实集群或账户。

## 1. 建模语义

### 1.1 资源与作业

- `Node(node_id, num_gpus, domain)`：GPU 节点，`domain` 为高速互联域标识。
- `ExistingJob(job_id, priority, gpu_usage, checkpoints)`：
  - `gpu_usage` 为 `JobPlacement(node_id, gpus)` 元组，一个作业可横跨多节点；
  - `priority` 数值越大优先级越高，**只有严格低于新作业优先级**的作业可被暂停；
  - `checkpoints` 为该作业列出的全部“下一次可暂停”检查点。
- `CheckpointOption(checkpoint_time, write_duration, write_amount,
  recovery_time, lost_work)`：
  - `checkpoint_time`：最早可开始写检查点的时刻；
  - `write_duration` / `write_amount`：写盘持续时长与期间持续占用的临时写盘
    额度（带宽槽位）；
  - `recovery_time`：之后恢复该作业需要额外占用 GPU 的时长（用于报告恢复代价）；
  - `lost_work`：在此检查点暂停会丢失的工作量。
- `NewJob(job_id, priority, requirement, latest_start_time)`：
  - `SameMachineRequirement(gpus)`：同机若干卡；
  - `MultiNodeDomainRequirement(domain, nodes, gpus_per_node)`：同一互联域内
    多台机器、每台固定卡数。

### 1.2 关键规则

1. **暂停的原子性**：暂停以整作业为单位。写盘完成时刻该作业在所有节点上的
   GPU 同时释放；不支持只释放一个作业的部分卡。已完成暂停的资源才能参与新落位。
2. **半开区间**：写盘在 `[write_start, write_finish)` 内占用写盘额度，
   `write_finish` 时刻额度与 GPU 立即释放，因此新作业可以在释放整点启动。
3. **检查点是“最早可暂停时刻”而非保证时刻**：额度不足时写盘请求排队，
   `write_start` 可晚于 `checkpoint_time`；作业在自己的写盘完成前继续持卡。
4. **不可暂停的作业**：优先级不低于新作业，或没有不早于 `current_time` 的
   检查点的作业，其 GPU 视为锁定资源。
5. **截止时间是硬约束**：新作业启动时刻必须 `<= latest_start_time`。

### 1.3 优化目标（字典序）

1. 先保证新作业**按时落位**（不可行则进入诊断，不参与择优）；
2. 最小化被暂停方的**总丢失工作量** `sum(lost_work)`；
3. 最小化**完成暂停的最晚时刻** `max(write_finish)`；
4. 再按暂停作业 ID 的排序元组定序，保证结果完全确定。

### 1.4 求解方法

对每个可暂停作业枚举“跳过 / 选择某个检查点”，用分支限界剪枝：

- 单次写盘 `write_amount` 超过总额度的子树直接剪掉；
- 即使暂停当前已选与所有剩余作业也凑不出需求形状（同机卡数 / 互联域机器数）
  的子树剪掉；
- 已选丢失工作量严格超过当前最优解的子树剪掉。

写盘调度（`planner.quota.schedule_writes`）是事件驱动贪心：在每个“检查点
到达 / 写盘完成”事件时刻按 `(最早开始时间, 作业ID)` 顺序尽量放入写盘，
放不下的等待下一完成事件。10 作业 × 3 检查点的最坏实例（理论组合上界
4^10 ≈ 105 万）在本机实测约 0.2 秒。

### 1.5 不可行时的“最早失败约束”

按约束可被观察到的时刻（同时刻按约束类型）排序，取最早一条：

| 约束类型 | 触发条件 | 观察时刻 |
| --- | --- | --- |
| `structural_capacity` | 即使暂停所有可暂停作业也凑不出同机卡数或互联域形状（**总卡够但域不够的反例**） | 0 |
| `disk_quota_serialization` | 无额度争用时可按时落位，真实额度下被串行化拖过截止；或必需写盘本身超额 | 首次争用时刻 / 当前时刻 |
| `late_start` | 即使写盘额度无限，门控作业的检查点/写盘完成时刻仍晚于截止 | 截止时刻 |

诊断消息带 `details` 字典（涉及节点、每节点空闲卡、被推迟的写盘、门控作业、
受保护作业 ID 等），便于定位。

## 2. 公开 API

```python
from planner import (
    Node, JobPlacement, CheckpointOption, ExistingJob,
    SameMachineRequirement, MultiNodeDomainRequirement, NewJob,
    plan, schedule_writes, validate_inputs,
    PlanResult, PreemptionAction, ScheduledWrite, FailureConstraint,
)

result = plan(
    nodes,                 # tuple[Node, ...]
    existing_jobs,         # tuple[ExistingJob, ...]
    new_job,               # NewJob
    disk_write_quota,      # int，正整数
    current_time=0,        # int，规划起点
)
```

`PlanResult` 主要字段：

- `feasible: bool`；
- 可行时：`start_time`、`node_allocations`（`(节点ID, 卡数)` 元组）、
  `preemption_actions`（每个含 `checkpoint_time / write_start /
  write_finish / release_time / released_resources / lost_work /
  recovery_time / write_was_delayed`）、`total_lost_work`、
  `max_preemption_finish`、`total_recovery_gpu_time`；
- 不可行时：`failure: FailureConstraint`（`constraint / observed_at /
  message / details`），亦可通过 `earliest_failing_constraint()` 获取。

非法输入（空节点、重复 ID、占用超容量、未知域、截止早于当前时刻等）抛
`ValueError`，由 `validate_inputs` 统一校验。

模块划分：`planner/model.py`（数据模型与校验）、`planner/quota.py`
（写盘额度调度）、`planner/placement.py`（落位与形状可行性）、
`planner/diagnose.py`（失败诊断）、`planner/search.py`（分支限界与
`plan` 入口）、`planner/examples.py`（合成实例与 CLI 演示）。

## 3. 复现步骤

```bash
# 版本要求：Python 3.11.6（仅标准库）
python3 --version

# 运行全部 unittest（从工作区根目录可直接发现 tests/test_*.py）
python3 -m unittest discover -s tests -v

# 隔离模式运行（不继承用户站点包/环境变量；显式声明模块路径）
python3 -I -c "import sys; sys.path.insert(0, '$PWD'); \
import unittest; r = unittest.TextTestRunner().run( \
unittest.defaultTestLoader.discover('tests')); raise SystemExit(not r.wasSuccessful())"

# 运行三个合成实例的命令行演示（含互联域反例）
python3 -m planner.examples
```

## 4. “总卡够但凑不齐互联域”的反例

`planner.examples.fragmentation_counterexample()` 构造：8 台 8 卡节点，
`n1–n4` 属互联域 `D-A`、`n5–n8` 属 `D-B`。4 个高优作业各在 `D-A` 的一台
节点上锁 4 卡（不可暂停），`D-B` 全空。新作业要求 `D-A` 内 4 台各 8 卡
（共 32 卡）。

- 集群总空闲 48 卡 ≥ 32 卡，按总卡数判断会误以为可行；
- 但 `D-A` 每台仅剩 4 卡，**0 台**满足 8 卡要求 → `structural_capacity`；
- 把锁卡作业改为可暂停后（同一实例工厂数据手动降优先级，见测试与演示），
  规划器立即给出可行计划，证明失败确实来自互联域形状而非总容量。

同机碎片化反例（两台 8 卡机各锁 4 卡、要同机 8 卡）在
`tests/test_diagnosis.py` 中同样覆盖。

## 5. 验证范围

`tests/` 下 48 个 unittest，从根目录 `python3 -m unittest discover` 即可发现：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_model.py` | 输入校验：空节点、重复 ID、未知节点/域、超容量占用、非法检查点字段、截止早于当前时刻 |
| `test_quota.py` | 额度调度：并行/串行、半开区间整点释放、迟到请求交错、零时长写盘、超额返回 None、同刻 tie-break 确定性 |
| `test_placement.py` | 同机/多机落位、按节点聚合占用、就绪时刻、形状可行性只看可暂停作业 |
| `test_plan_normal.py` | 零抢占落位、完整暂停时间线可执行性、多检查点按丢失工作量选择、截止收紧时回退早检查点、同/高优作业保护 |
| `test_diagnosis.py` | 互联域反例、同机碎片反例、`late_start` 门控作业诊断、过期检查点忽略、额度串行化诊断及放宽额度后转可行、超额写盘诊断 |
| `test_interleaving.py` | 交错打包无空隙、三路串行可行、部分重叠后并行恢复、释放整点启动边界、`current_time` 偏移、两级 tie-break |
| `test_objective_and_release.py` | 可行性优先于丢失工作量、丢失工作量优先于完成时刻、跨节点作业原子释放与恢复代价 |
| `test_exhaustive.py` | **独立暴力枚举交叉验证**：40 个随机实例逐组合用不同实现重算，核对可行性与三级目标键（开发期另用临时脚本扩到 200 个实例复核，脚本未纳入仓库）；10 作业规模限时 <10s；确定性（重复调用结果相等） |

所有测试均在本机 Python 3.11.6 实际执行通过（48/48）。演示程序
`python3 -m planner.examples` 的输出来自真实 `plan()` 调用，不是预置文本。
