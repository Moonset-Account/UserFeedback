# 混合执行调度器

一个仅依赖 Python 标准库的混合执行调度器原型：交互式流任务（`stream`）与批任务（`batch`）共享执行槽、内存等多维资源；批任务可以借用流任务暂时闲置的额度。流任务回收额度时，批任务只能在声明的合法阶段边界释放资源，并可在可插入保存点处暂停、之后恢复。

本实现是**本地合成数据上的防御性调度/资源规划组件**，不包含也不执行任何入侵、利用或规避载荷。

## 运行环境

- Python 3.11.6（或兼容的 3.11+）
- 无需安装第三方依赖，不需要网络访问。

## 运行测试

在当前工作区根目录执行题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

运行合成示例：

```bash
python3 examples/demo.py
```

最近一次完整执行包含 31 个 `unittest` 用例，结果为 `OK`。最终以你本地实际执行输出为准。

## 核心模型

### 1. 多维资源向量

`ResourceVector` 将执行槽、内存以及可选的 GPU、高速内存等统一为命名维度：

```python
from hybrid_scheduler import ResourceVector

need = ResourceVector.of(slots=2, memory=6)
capacity = ResourceVector.of(slots=4, memory=16)
assert need.fits_in(capacity)
assert capacity - need == ResourceVector.of(slots=2, memory=10)
```

未声明的维度按 `0` 处理；向量不可变，可安全用于计划快照和事件。

### 2. 任务、阶段依赖、保存点、资源与期限

`TaskSpec` 包含若干 `Phase`。阶段通过 `depends_on` 声明 DAG 依赖；构造时会拒绝未知依赖和依赖环。

```python
from hybrid_scheduler import Phase, TaskKind, TaskSpec, ResourceVector

batch = TaskSpec(
    id="b1",
    kind=TaskKind.BATCH,
    deadline=100.0,
    priority=10,
    phases=(
        Phase(id="load", duration=2,
              resources=ResourceVector.of(slots=1, memory=2)),
        Phase(
            id="checkpoint_boundary",
            duration=1,
            depends_on=("load",),
            resources=ResourceVector.of(slots=1, memory=2),
            legal_boundary=True,          # 可在此阶段边界释放借用额度
            savepoint_allowed=True,       # 可插入恢复保存点
            checkpoint_buffer=ResourceVector.of(memory=2),
            checkpoint_duration=0.2,
            continue_resources=ResourceVector.of(memory=1),
        ),
    ),
)
```

- `legal_boundary=True`：阶段结束后是合法释放边界。
- `savepoint_allowed=True`：该边界可以生成保存点并暂停，供后续恢复。
- `checkpoint_buffer`：写保存点/整理边界状态时临时需要的缓冲向量。
- `continue_resources`：释放借用额度后若剩余资源仍满足该向量，任务可以不暂停而继续。
- `deadline`：绝对完成期限，用于紧急度排序、恢复顺序和可选的硬期限校验。

`next_legal_boundary(spec, current_phase, savepoint_required=...)` 会在阶段 DAG 上寻找最近的合法边界，并给出到达该边界所需的后续阶段工期。

### 3. 借用账本与状态化调度器

`HybridScheduler` 维护容量、可用资源、任务运行时状态和借款账本：

```python
from hybrid_scheduler import HybridScheduler, TaskSpec, TaskKind, Phase, ResourceVector

scheduler = HybridScheduler(ResourceVector.of(slots=4, memory=8))
stream = TaskSpec(
    id="s1",
    kind=TaskKind.STREAM,
    quota=ResourceVector.of(memory=4),
    phases=(Phase(id="run", resources=ResourceVector.of(slots=1, memory=1)),),
)
scheduler.submit(stream, start=True)
# ... submit/start batches ...
scheduler.lend("s1", "b1", ResourceVector.of(memory=2))
plan = scheduler.reclaim("s1", at=5.0, borrower_order=["b2", "b1"])
```

`reclaim()` 默认：

1. 汇总该流任务未收回的借款；
2. 从每个借用者的当前阶段寻找合法释放/保存点边界；
3. 为借用者生成“释放并继续”和“保存点后暂停并释放全部持有资源”等候选动作；
4. 将其他持有资源的批任务作为**可选解阻者**纳入规划；
5. 搜索最少暂停的可行组合；
6. 在可行时应用资源、借款、任务状态和恢复候选变更。

部分回收可传 `amount=ResourceVector.of(memory=2)`；同一时刻多个流任务一起回收可使用 `reclaim_many([...])`，避免先授权一个流而误占另一条缓冲级联所需资源。

## 规划算法

对于固定的一组释放动作 `a`：

- `buffer(a)`：执行边界/保存点前必须申请到的临时缓冲；
- `release(a)`：动作完成后释放回逻辑资源池的向量。

动作可运行条件为：

```text
buffer(a) <= available
```

执行后的逻辑可用量为：

```text
available' = available + release(a)
```

因为每个完成动作只增加可用资源，不会减少已有资源，所以当前任何可运行动作都不会使另一个可运行动作变得不可行。规划器反复执行：

1. 找出所有缓冲可满足的动作；
2. 按“少暂停、少保存点、少动作、短暂停工期、截止期、优先级、事件次序、ID”选择；
3. 若没有可运行动作，则构造等待图并检测资源依赖环。

回收授权被放在协调释放链之后：一个借用者释放出的内存可以先作为下一个借用者的边界缓冲，最后再统一把回收额度授予流任务。账本仍记录每个 `ReturnGrant` 的属主和借款 ID。

### 避免过度暂停

每个 `ActionGroup` 可以提供多个 `ReleaseAction` 替代方案。`plan_reclamation()` 会在有界配置空间内搜索，按以下字典序成本选解：

```text
(暂停任务数, 保存点数量, 动作数量, 串行化暂停工期)
```

因此：

- 能 `release_and_continue` 时不会优先暂停；
- 可选的其他持有者只有在能解锁缓冲依赖时才会被选中；
- 能用一个不暂停的持有者解阻时，不会选择暂停某个借用者；
- 相同实质成本下才使用截止期、事件输入顺序和稳定 ID 做 tie-break。

### 恢复顺序

`plan_resume_order()` 根据暂停动作的 `resume_needs`、截止期和优先级安排恢复：

- 若所有候选资源总需求不超过当前可用量，按 EDF/优先级/事件顺序恢复；
- 若不能全部恢复，用有界子集搜索最大化恢复数量，并优先更早截止期的任务；
- 暂时无法恢复的任务进入 `scheduler.deferred_resume`；
- 任务完成释放资源或收到新的闲置额度事件后，`try_resume_deferred()` 会再次按截止期安排恢复。

## 无解与资源依赖环解释

当剩余动作都无法获得缓冲时，规划器构造等待边：

```text
i -> j  当且仅当 i 在维度 d 上存在缓冲缺口，而 j 的 release 在维度 d 上为正
```

如果等待图中存在环，`ReclamationPlan.cycle` / `ResourceCycle` 提供：

- `nodes`：规范化旋转后的环节点，例如 `("a", "b", "a")`；
- `edges`：每条边对应的资源维度、缺口和对方可释放量；
- `shortfalls`：环上各任务的缓冲缺口；
- `available`：失败时的可用资源；
- `message`：中文解释；
- `to_dict()`：便于输出 JSON 或接入外部诊断。

不同输入顺序会把环规范化到同一个最小节点起点，因此 `["a","b"]` 和 `["b","a"]` 会得到相同的 `("a","b","a")`。

如果缺口没有任何剩余持有者可释放，则不会误报为环；计划会说明这是外部资源不足或缺少合法边界。

## 离散事件输入

使用 `Event` 或普通字典构造事件日志，由 `run_event_log()` 执行：

```python
from hybrid_scheduler import Event, EventKind, run_event_log, ResourceVector

events = [
    Event(0, EventKind.SUBMIT, {"spec": stream}),
    Event(0, EventKind.LEND, {
        "owner_id": "s1",
        "borrower_id": "b1",
        "amount": ResourceVector.of(memory=2),
    }),
    Event(5, EventKind.RECLAIM, {
        "stream_id": "s1",
        "borrower_order": ["b2", "b1"],
    }),
]
result = run_event_log(events, capacity=ResourceVector.of(memory=8))
print(result.plans[0].release_order)
```

事件排序键为：

```text
(time, event.order, 稳定输入下标)
```

因此：

- 不同时间严格按虚拟时间排序；
- 同一时间默认保留输入排列顺序；
- 显式 `order` 可覆盖同时间顺序；
- 资源依赖强制的顺序高于输入顺序；
- 彼此独立、资源条件相同的借用者会按输入/tie-break 顺序执行；
- 无论排列如何，最终资源余额和借款账本一致。

同一时刻多个流任务一起回收时，可在一个 `RECLAIM` 事件中传：

```python
Event(5, EventKind.RECLAIM, {"stream_ids": ["s1", "s2"]})
```

或直接调用 `scheduler.reclaim_many([...])`，规划器会把多个属主的索赔合并成一个释放—授权波次。

## 公开 API 速览

| 名称 | 用途 |
| --- | --- |
| `ResourceVector` / `as_vector` | 不可变多维资源、算术、缺口和容量判断 |
| `Phase` / `TaskSpec` | 阶段依赖、合法边界、保存点、阶段资源、期限 |
| `TaskKind` / `TaskStatus` / `EventKind` | 枚举 |
| `Loan` / `ClusterState` / `TaskRuntime` | 借款账本、集群状态和运行时任务 |
| `ReleaseAction` / `ActionGroup` / `ReturnGrant` | 释放候选、替代方案组和归还账目 |
| `ResumeCandidate` | 恢复候选 |
| `PlannerConfig` | 期限硬约束、搜索上限、是否保留事件 tie 顺序 |
| `find_release_order()` | 固定动作集合的合法释放顺序/环检测 |
| `plan_reclamation()` | 带替代方案、索赔、授权和恢复的完整搜索 |
| `plan_resume_order()` | 恢复顺序和延期集合 |
| `make_resource_cycle()` / `build_wait_edges()` | 等待图和资源环诊断 |
| `HybridScheduler` | 有状态提交、启动、借出、回收、完成和恢复扫描 |
| `ReclaimRequest` | 同时回收多个流的请求对象 |
| `Event` / `run_event_log()` / `EventResult` | 离散事件输入与执行结果 |
| `next_legal_boundary()` / `legal_boundary_candidates()` | DAG 上的最近/全部合法或保存点边界 |

## 测试与验证范围

`tests/` 中包含以下合成验证：

- `test_resources.py`：资源向量运算、维度缺省、不可变性、非法负数和严格减法。
- `test_model_validation.py`：未知依赖、阶段 DAG 环、拓扑顺序、最近合法边界和缺失保存点。
- `test_planner.py`：
  - 缓冲级联必须“持有者先释放”；
  - 真资源环的规范化节点、边、缺口和中文解释；
  - 无持有者缺口不误报为环；
  - 事件顺序与稳定 ID tie-break；
  - 截止期优先和硬期限不可行；
  - 优先不暂停方案、跳过无关解阻者、用可选持有者避免暂停；
  - 索赔账目不足；
  - EDF 恢复顺序和最大恢复子集搜索。
- `test_scheduler_events.py`：
  - 高级调度器的正常释放、授权、暂停、延期恢复和槽/内存账目；
  - 部分回收；
  - 在多个可插入保存点中选择缓冲可满足的较晚边界；
  - 缺少合法边界的诊断；
  - 跨流借用者打破缓冲依赖；
  - 截止期感知的延期恢复；
  - 两个和三个同时回收借用者的不同排列；
  - 依赖顺序覆盖输入顺序；
  - 资源环在不同输入排列下输出同一规范环；
  - 同一时刻多个回收事件的稳定输入顺序；
  - 同时刻跨流合并回收波次。

## 已知边界（如实记录）

- 这是调度逻辑与账本原型，不真正 fork 进程、执行 Flink/Spark 作业或操作真实 cgroup/内存。
- 搜索针对题面规模的候选动作/恢复集合；`PlannerConfig.max_configurations` 和 `max_resume_subsets` 提供有界保护。超大实例需要更强的索引、剪枝或多项式专用算法。
- 阶段工期用于到达边界的保守估算和截止期排序；`HybridScheduler.reclaim(apply=True)` 会逻辑提交整个已验证计划。离散事件测试中的回收对象均构造为边界立即可用，或通过计划中的 `advance_duration` / `finish_time` 表达未来边界成本。
- 资源是可分命名额度；没有建模内存碎片、网络 I/O、数据本地性或检查点存储容量。
