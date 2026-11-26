# 模型版本感知的缓冲区调度模块

这是一个纯 Python 3.11 标准库实现的**模拟 GPU 缓冲池调度器**，用于语音转写/二阶段模型场景。它不连接真实 GPU 或网络，而是用带偏移和对齐的区间分配器验证：

- 调用方提交张量形状、可用格式、依赖阶段和完成回执/回调；
- 同一输出的多个只读分支共享同一个实际区域，不发生模拟拷贝；
- 覆盖性写入等待所有相关读者结束，并避免写者饥饿；
- 批次在创建时固定模型内存布局，模型热更只影响后续新批次；
- 可取消单个分支或其依赖链；
- 内存压力下先回收空闲且 `evictable=True` 的缓冲，再安全重定位无租约的可移动缓冲；
- 每个调度步骤都可追溯实际区域、是否授权、租约/区域释放原因。

## 运行测试

题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

无需安装依赖，不需要联网或真实 GPU。当前工作区已用 Python 3.11.6 实测：

```text
Ran 22 tests in ...

OK
```

## 公开 API

主要导入：

```python
from buffer_scheduler import (
    AccessMode,
    BufferScheduler,                 # 别名：ModelVersionBufferScheduler
                                     #       ModelVersionAwareBufferScheduler
    ModelLayout,
    Shape,
    StageRequest,
    RequestState,
    ReleaseReason,
    SchedulerResult,
    StepReport,
    RegionInfo,
    CompletionReceipt,
)
```

### `BufferScheduler(...)`

```python
BufferScheduler(
    memory_limit=64 * 1024 * 1024,
    *,
    model=ModelLayout("default", alignment=256),
    models=None,
    active_version=None,
    format_policy="ordered",       # 或 "smallest"
    fair_writer_queue=True,
    raise_on_pressure=False,
)
```

- `memory_limit`：模拟缓冲池总字节数。
- `model` / `models`：初始模型布局。
- `format_policy`：`ordered` 按调用方格式优先级选择；`smallest` 选择占用最小的可用格式。
- `fair_writer_queue`：有覆盖写等待时，阻止更晚创建的新读者插队。
- `raise_on_pressure`：为 `True` 时，无法满足的分配抛出 `MemoryPressureError`；默认返回 `state=pending`、`blocked_reason="memory_pressure"` 的结果，便于之后取消/回收再重试。

### 提交与生命周期

- `submit_stage(request, /, *, strict=None, **kwargs) -> SchedulerResult`  
  别名：`submit`。提交一个生产者、只读分支或覆盖写阶段。
- `StageRequest(...)` 字段：
  - `request_id: str`
  - `shape: Shape | Sequence[int]`
  - `available_formats: Sequence[str]`，支持 `fp32/float32`、`fp16/float16`、`bf16`、`int8/uint8/int32/int64` 等写法
  - `batch_id="default"`
  - `depends_on=()`：上游请求/阶段 ID
  - `access=AccessMode.WRITE` 或 `AccessMode.READ`
  - `source_id`：零拷贝共享或覆盖写的源缓冲/生产者请求 ID
  - `buffer_id`：自定义稳定缓冲 ID，默认等于生产者 `request_id`
  - `stage_name`、`model_version`、`on_complete`、`receipt_id`
  - `evictable=False`、`movable=True`
- `StageRequest.branch(branch_id, source_id, shape, available_formats, ...)`：快速创建只读分支，并自动把 `source_id` 加入依赖。
- `submit_output(request_id, shape, available_formats, dependent_stages, ...)`：一次提交 GPU 输出和多个下游分支；`dependent_stages` 可传阶段名字符串或映射。
- `submit_batch(batch_id, stages, *, model_version=None, auto_release=True)`：创建固定模型版本的批次、加入 DAG 并封板。
- `seal_batch(batch_id)`：动态逐阶段提交后封板；封板时检查未知依赖和环。
- `complete(request_id, payload=_UNSET)`：完成运行中的阶段，释放其读/写租约，推进依赖者和等待写者，并发送回执。
- `cancel_branch(branch_id, reason=None, *, cancel_dependents=True)`  
  别名：`cancel_request(...)`。取消一个分支/请求；默认同时取消其传递依赖链，但不会影响共享源和兄弟分支。被取消生产者的无租约物理区域会立即释放以供再分配。

### 模型热更

- `hot_swap_model(ModelLayout(version="new", alignment=256, ...))`
- `register_model(layout, activate=None)`
- `activate_model(version)`
- `get_model(version=None)`

批次在首次创建时持有自己的 `ModelLayout`。之后热更活动模型不会修改旧批次；即使向旧 `batch_id` 继续添加阶段，也仍使用旧布局。

### 内存维护与查询

- `release_buffer(buffer_id, *, reason=None)`：调用方显式释放空闲缓冲；仍有租约时抛 `BufferBusyError`。
- `reclaim_memory(needed_bytes=0)`：回收空闲且 `evictable=True` 的缓冲。
- `write_payload(identifier, payload)` / `read_payload(identifier)`：在当前区域保存/读取合成对象；可传稳定 `buffer_id` 或请求 ID。只读分支读到的是同一对象引用，用于测试零拷贝共享。
- `get_request(request_id) -> RequestInfo`
- `current_region(buffer_id) -> RegionInfo | None`
- `get_region(region_id) -> RegionInfo`
- `active_readers(buffer_id)` / `waiting_writers(buffer_id)`
- `steps_for(request_id)`
- `memory_snapshot()`：容量、空闲、最大空闲块、区间段和缓冲租约状态。
- `batch_snapshot(batch_id)`
- `audit()`：完整步骤轨迹的 JSON-like 字典列表。
- `scheduler.trace: list[StepReport]`：全局有序审计轨迹。
- `scheduler.receipts: dict[str, CompletionReceipt]`

## 返回报告与释放原因

每个写操作返回 `SchedulerResult`，其中：

- `steps: list[StepReport]`：仅本次调用新产生的步骤；
- `request: RequestInfo | None`：最新请求状态；
- `receipt` / `receipts`：完成或取消回执；
- `region`：最后一个带实际区域的步骤所使用的 `RegionInfo`；
- `regions`：本次所有步骤涉及的区域快照；
- `releases`：所有 `ReleaseRecord`；
- `release_reasons`：便捷的原因字符串列表；
- `ok` / `state` / `error`：阻塞或失败信息。

`StepReport` 关键字段：

```python
step.sequence
step.action          # submitted, region_allocated, region_shared, write_waiting,
                     # write_granted, lease_released, request_completed,
                     # request_cancelled, memory_pressure, region_reallocated, ...
step.region          # 本步骤实际分配/共享/授权/重定位/释放的 RegionInfo
step.granted         # 锁或分配是否被授权
step.release         # ReleaseRecord | None
step.release_reason  # step.release.reason 的快捷方式
```

`ReleaseRecord.release_kind` 区分：

- `"lease"`：读/写租约结束，物理区域可能仍保留给其他分支；
- `"region"`：模拟内存区间被释放、驱逐或因重定位替换。

内置释放原因（`ReleaseReason`）：

| 原因 | 含义 |
| --- | --- |
| `branch_completed` / `reader_completed` | 只读分支/读者结束 |
| `writer_completed` | 覆盖写或生产者结束写租约 |
| `request_completed` | 普通请求完成 |
| `branch_cancelled` / `request_cancelled` | 分支或请求被取消 |
| `dependency_cancelled` | 上游被取消，依赖链连带取消 |
| `batch_completed` | 封板批次全部终态后释放区域 |
| `caller_released` | 调用方显式释放 |
| `memory_pressure_eviction` | 内存压力下驱逐空闲可回收缓冲 |
| `memory_pressure_relocation` | 内存压力下把空闲可移动缓冲重定位以合并空间 |

## 最小示例

```python
from buffer_scheduler import BufferScheduler, ModelLayout

scheduler = BufferScheduler(
    memory_limit=4096,
    model=ModelLayout("asr-v1", alignment=128),
)

scheduler.submit_output(
    "asr-output",
    shape=(1, 32, 16),
    available_formats=["fp16", "fp32"],
    dependent_stages=["ctc-decode", "phase-2"],
)

scheduler.complete("asr-output", payload={"synthetic_mel": [[0.0]]})

ctc = "asr-output:ctc-decode"
phase2 = "asr-output:phase-2"
assert scheduler.get_request(ctc).region.region_id == \
       scheduler.get_request(phase2).region.region_id

done = scheduler.complete(ctc)
for step in done.steps:
    print(
        step.sequence,
        step.action,
        step.region.region_id if step.region else None,
        step.release_reason,
    )

# 热更后，旧 output-asr 批次仍固定在 asr-v1；新输出使用 asr-v2。
scheduler.hot_swap_model(ModelLayout("asr-v2", alignment=256))
```

覆盖写会在仍有 `phase-2` 读者时保持 pending；完成或取消最后一个相关读者后，写者才会得到 `write_granted` 步骤和同一实际区域。

## 验证范围

测试文件：

- `tests/test_layouts_allocation.py`：形状/步长/格式选择、对齐分配、内存压力驱逐、碎片场景重定位、不可满足分配的边界。
- `tests/test_reader_writer.py`：多分支零拷贝共享、负载对象身份、覆盖写等待全部读者、写者公平队列、取消单分支、依赖链取消、回执回调、重复完成、格式冲突、未知依赖/依赖环、轨迹序号连续性、区域/释放报告完整性。
- `tests/test_model_versioning.py`：热更后旧批次和旧布局区域不变、新批次使用新对齐/布局、显式版本固定、禁止同版本冲突重定义。
- `tests/test_cancellation_pressure.py`：取消生产者后等待中的分配立即复用区域、活跃租约阻止驱逐/重定位、读者结束后压力分配自动重试成功。
- `tests/test_speech_pipeline.py`：本地合成 log-mel/输出对象的语音 ASR → CTC/二阶段模型分支正常流程，并交错热更。
- `tests/test_thread_safety.py`：16 个读者线程并发完成，验证写者只在全部读者结束后授权以及内存区间记账一致。

这些测试全部使用本地合成数据和进程内模拟区间池；没有读取凭据、联网、访问真实目标、上传材料或安装依赖。该模块只实现防御性的调度、租约、版本固定和内存安全判断，不包含任何利用、入侵、攻击或规避载荷。

未覆盖真实 GPU 驱动、真实张量拷贝耗时、跨进程共享和性能基准；`payload` 身份检查用于在模拟层证明“共享而非复制”。
