# 进程组驻留内存回放核算

一个仅使用 Python 标准库的内存观测核算组件。它回放采集回执中的进程、映射区间、驻留页帧号（PFN）和帧复用编号，输出：

- 机器口径的唯一物理驻留帧数量与字节数；
- 每进程独占帧/字节；
- 每进程比例份额（PSS 口径，使用精确 `Fraction`）；
- 观测位置的连续核算水位、最新完整位置和缺口；
- 帧复用编号隔离后的迟到撤销审计。

运行环境已实测为 Python `3.11.6`，不需要安装第三方依赖。

## 核算规则

1. 物理帧身份是复合键 `(pfn, reuse_id)`。裸 PFN 在帧释放并复用后不再唯一。
2. 总物理驻留量按当前活动的不同 `(pfn, reuse_id)` 计数：
   \[
   physical\_bytes = active\_frame\_count \times page\_size
   \]
3. 同一进程即使有两个映射或两个虚拟地址指向同一帧，也只形成一个持有者进程；进程内的多个映射出现次数仍保留在 `mapping_occurrence_*` 字段中，用来解释原始 RSS 相加为何偏大。
4. 一个帧有 \(n\) 个不同持有者进程时，每个持有者分摊 \(1/n\) 页。所有已观测进程的比例份额之和精确等于物理驻留总量。
5. 只有一个持有者进程的帧计入该进程的独占量。
6. COW 事件把指定的一个映射/虚拟页从旧帧移动到新帧；如果该进程还有另一个映射指向旧帧，它仍保留旧帧持有者身份。
7. 迟到的撤销必须携带旧帧的 `(pfn, reuse_id)`。组件只删除该精确帧代，绝不会删除同 PFN 的新复用代。目标不存在时记录 stale audit，而不是按 PFN 误删。

报告还提供 `process_rss_sum_bytes`、`shared_frame_overcount_bytes`、`mapping_occurrence_sum_bytes` 和 `duplicate_mapping_overcount_bytes`，把“进程 RSS 相加偏大”拆成共享帧重复计数与同进程重复映射计数两部分。

## 运行测试

题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

最近一次本地实测结果：`Ran 30 tests ... OK`。

测试全部使用合成数据，覆盖：

- 正常共享/独占/PSS 对账；
- 同进程双映射同一页；
- 删除其中一个映射后仍为持有者，删除最后一个后释放；
- COW 后旧帧共享者和新帧独占者的所有权；
- COW 只移动重复映射中的一个地址；
- PFN 复用、乱序到达、旧帧撤销晚到、重复撤销，以及三份交错回执的全部 6 种到达排列；
- 观测位置缺失、指定来源缺失、显式 seal/confirm；
- 乱序回执按逻辑位置重放；
- 重复 `receipt_id` 幂等；
- 快照对账删除旧页；
- 映射区间和越界地址校验；
- CLI 与 `examples/` 下两份本地合成数据。

## 命令行

```bash
# 完整 JSON 结果
python3 -m memory_accounting examples/normal_group.json

# 文本摘要
python3 -m memory_accounting examples/normal_group.json --summary
python3 -m memory_accounting examples/interleaved_reuse_and_gap.json --summary
```

正常合成数据的摘要为：

```text
latest_accountable_position=1
latest_complete_position=1
gaps=[]
physical_frames=3
physical_resident_bytes=12288
rss_reconciliation=process_rss_sum=16384,shared_overcount=4096,duplicate_mapping_overcount=4096
pid=A exclusive=4096 proportional=6144 share=1/2
pid=B exclusive=4096 proportional=6144 share=1/2
```

交错数据中，位置 1/2/3 已齐，位置 4 缺失；旧帧 `(5,1)` 的撤销不会删除存活的 `(5,2)`：

```text
latest_accountable_position=3
latest_complete_position=3
gaps=[{'position': 4, 'state': 'missing_position', ...}]
physical_frames=1
physical_resident_bytes=4096
rss_reconciliation=process_rss_sum=4096,shared_overcount=0,duplicate_mapping_overcount=0
pid=B exclusive=4096 proportional=4096 share=1
```

## 公开 API

### 帧与底层核算

```python
from memory_accounting import FrameRef, MemoryAccount

account = MemoryAccount(page_size=4096, strict_mappings=True)
account.declare_mapping("A", "m1", start=0x1000, end=0x3000)

account.add_resident(
    "A",
    pfn=100,
    reuse_id=7,
    mapping_id="m1",
    vaddr=0x1000,
)

account.copy_on_write(
    pid="A",
    mapping_id="m1",
    vaddr=0x1000,
    old_pfn=100,
    old_reuse_id=7,
    new_pfn=120,
    new_reuse_id=1,
)

account.revoke_frame(pfn=100, reuse_id=7)  # 只撤销旧的第 7 代
report = account.report()

report.physical_resident_bytes
report.processes["A"].exclusive_bytes
report.processes["A"].proportional_bytes  # fractions.Fraction
report.to_dict()                          # Fraction 转精确分数与 float
```

常用方法：

- `declare_mapping(...)` / `remove_mapping(pid, mapping_id)`
- `add_resident(...)` / `remove_resident(...)`
- `revoke_frame(frame=..., pfn=..., reuse_id=...)`
- `copy_on_write(...)`
- `snapshot_process(...)`
- `report()`

也可以直接从 JSON-like 进程列表计算：

```python
from memory_accounting import account_observations

report = account_observations({"page_size": 4096, "processes": [...]})
```

### 回执回放与缺口

```python
from memory_accounting import ReplaySession, replay_receipts

result = replay_receipts(
    {
        "page_size": 4096,
        "first_position": 1,
        "expected_sources": ["collector-a", "collector-b"],
        "receipts": [...],
    }
)

result.latest_accountable_position  # 从 first_position 起连续完整的最高位置
result.watermark                    # 别名
result.latest_complete_position     # 即使前面有缺口，最高的单独完整位置
result.gaps                         # GapInfo(position, state, missing_sources, ...)
result.report                       # watermark 处的累计回放报告
result.reports                      # 连续前缀上每个位置的累计报告
result.isolated_reports             # 每个完整位置仅用该位置回执生成的快照报告
result.latest_isolated_report       # latest_complete_position 对应的本地快照报告
result.to_dict()
```

两种“可核算位置”的口径同时保留：

- 对事件/增量回放，缺口后的状态缺少前置事件，因此 `latest_accountable_position == watermark`，即连续完整前缀的末端；`report`/`reports` 是可靠累计结果。
- 若每个观测位置的回执都是完整进程快照，缺口后的位置仍可独立核算；查看 `latest_complete_position`、`isolated_reports[position]` 或 `latest_isolated_report`。

增量式用法：

```python
session = ReplaySession(
    page_size=4096,
    expected_sources={"1": ["a", "b"], 2: ["a", "b"]},
    expected_positions=[1, 2, 3],
    require_seal=False,
)
session.ingest(receipt_dict)
session.confirm_position(1)  # 缺少 expected source 时抛 GapError
result = session.result()
```

完整性字段的含义：

- `watermark` / `latest_accountable_position`：从 `first_position`（默认 1）开始连续完整、可安全按事件顺序回放的最高位置。
- `latest_complete_position`：所有期望来源已到齐（或被权威 seal）的最高位置；它可能位于缺口之后。
- `gaps[].state`：
  - `missing_position`：该位置没有任何回执；
  - `incomplete`：有回执，但缺少 `expected_sources` 中的来源。
- `require_seal=True` 时，还必须显式 `seal_position()`，或在回执中给出 `"confirmed": true` / `"sealed": true` / `"complete": true`。

## 支持的回执形状

### 进程快照回执

```json
{
  "position": 12,
  "source": "collector-0",
  "receipt_id": "receipt-12-0",
  "processes": [
    {
      "pid": 1001,
      "mappings": [
        {"mapping_id": "libc", "start": 4096, "end": 20480}
      ],
      "pages": [
        {
          "mapping_id": "libc",
          "vaddr": 4096,
          "pfn": 55301,
          "reuse_id": 9
        }
      ]
    }
  ]
}
```

`frame` 也可以写成对象：

```json
{"frame": {"pfn": 55301, "reuse_id": 9}, "mapping_id": "libc", "vaddr": 4096}
```

快照会对账该 PID 的完整 mappings/pages：新快照中消失的旧页会被移除，但不会影响其他进程对同一帧的持有。

### 事件记录

回执可包含 `events`、`records` 或 `changes`：

```json
{"kind": "resident", "pid": "A", "pfn": 5, "reuse_id": 2,
 "mapping_id": "m", "vaddr": 4096}

{"kind": "unmap", "pid": "A", "pfn": 5, "reuse_id": 1,
 "mapping_id": "m", "vaddr": 4096}

{"kind": "revoke", "pfn": 5, "reuse_id": 1}

{"kind": "cow", "pid": "A", "mapping_id": "m", "vaddr": 4096,
 "old_pfn": 5, "old_reuse_id": 1,
 "new_pfn": 9, "new_reuse_id": 1}
```

支持的 kind 别名包括 `add/map/resident_page`、`remove/unmap/remove_page`、`free/free_frame/tombstone`、`copy_on_write/write_fault`、`upsert_mapping/declare_mapping` 和 `remove_mapping/delete_mapping`。

## JSON 中的分数

比例页和字节使用 `fractions.Fraction` 计算以避免浮点误差。`to_dict()` 同时给出：

```json
{
  "numerator": 6144,
  "denominator": 1,
  "exact": "6144/1",
  "float": 6144.0
}
```

例如半页会序列化为 `numerator=1, denominator=2`。由于 `page_size=4096` 时半页字节可整除，`proportional_bytes` 会约分为 `2048/1`。

## 验证范围与限制

已实际执行并通过的验证仅限本仓库中的本地合成数据和 CLI 样例；没有读取真实 `/proc`、真实采集目标、凭据，也没有联网或安装依赖。

当前实现假设同一核算实例使用统一 `page_size`。普通页核算可直接使用；若未来需要透明大页/异构页大小，需要为每个 `FrameRef` 增加帧大小。映射区间用于包含关系校验和虚拟页槽位归一化，不会把整个 VMA 区间都计为驻留内存；只有显式驻留帧参与物理量计算。
