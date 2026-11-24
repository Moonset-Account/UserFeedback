# transit_audit —— 跨区专线传输合规核查库

跨区专线在每一跳给出由接收端确认的交接回执。本库输入数据批次的预期分段、
每段传输尝试的编号与最后收货结果，以及同一尝试沿途的中继与区域回执，
按最终收货的字节范围核对每段能否在同次传输的回执中接成连续的获准路线；
原先失败的尝试单独留档。越界或缺一跳时，报告给出具体子区间和节点。

仅使用 Python 3.11 标准库（`dataclasses`、`enum`），无第三方依赖。

## 1. 核查规则

1. **字节区间语义**：所有区间为左闭右开的半开区间 `[start, end)`。
   因此 `[0,10)` 与 `[10,20)` 首尾相接，视为连续，中间不存在缺口。
2. **尝试选择**：每个分段只核查最后收货成功（`DeliveryResult.DELIVERED`）
   的那次尝试；存在多次成功尝试时取尝试编号最大者。
   - 收货失败（`FAILED`）的尝试不参与路线核查，单独留档；
   - 被更大编号成功尝试取代的较早成功尝试同样留档，并注明取代关系；
   - 没有任何成功尝试的分段结论为 `undelivered`。
3. **逐跳路线核查**（对选中的成功尝试，沿预期路线按跳号顺序）：
   - 中继可把一段拆成多次传送：一份回执可携带多个实际覆盖的字节区间，
     区间允许乱序、重叠或首尾相接，核查前先合并；
   - 合并后回执未覆盖分段预期范围的部分 → `missing_coverage`（缺口，给出缺口子区间与节点）；
   - 回执越出分段预期范围的部分 → `out_of_bounds`（越界，给出越界子区间与节点）；
   - 预期路线上的节点没有任何回执 → `missing_hop`（缺跳，按分段完整区间给出节点）；
   - 出现预期路线之外的节点回执 → `unknown_node`（给出该节点与其回执区间）。
4. 分段无任何违规即 `compliant`，否则 `non_compliant`；批次内任一分段不合规
   （含未送达）时，批次报告的 `compliant` 为 `False`。

## 2. 公开 API

所有公开名称均可从顶层包 `transit_audit` 导入。

### 输入模型（`transit_audit.model`）

| 名称 | 说明 |
| --- | --- |
| `ByteRange(start, end)` | 半开字节区间，要求非负整数且 `start < end` |
| `NodeKind` | 节点角色枚举：`RELAY`（中继）/ `REGION`（区域） |
| `RouteNode(node, sequence, kind)` | 预期路线上的一跳，`sequence` 为从 1 开始的连续跳号 |
| `HopReceipt(node, kind, ranges)` | 一个节点确认的交接回执，`ranges` 为非空 `ByteRange` 序列 |
| `DeliveryResult` | 收货结果枚举：`DELIVERED` / `FAILED` |
| `Attempt(attempt_no, route, receipts, result)` | 一次传输尝试；跳号必须恰好覆盖 1..N，同节点至多一份回执 |
| `Segment(segment_id, expected_range)` | 预期数据分段 |
| `Batch(segments, attempts)` | 完整批次输入；`attempts` 为 `segment_id -> Attempt 序列` 的字典 |

模型均为不可变值对象，构造时完成字段校验，非法输入抛 `ValueError`。

### 核查入口与报告（`transit_audit.audit`）

| 名称 | 说明 |
| --- | --- |
| `verify_batch(batch) -> BatchReport` | 核查整批，按分段顺序产出报告 |
| `verify_segment(segment, attempts) -> SegmentReport` | 核查单个分段 |
| `BatchReport` | `.compliant`（全段合规）、`.violations`（跨段违规明细）、`.segment_reports`、`.to_dict()` |
| `SegmentReport` | `.status`、`.selected_attempt_no`、`.violations`、`.archived_attempts`、`.is_compliant`、`.to_dict()` |
| `Violation` | `.kind` / `.node` / `.node_kind` / `.ranges`（具体子区间）/ `.message`（中文定位说明） |
| `ViolationKind` | `OUT_OF_BOUNDS` / `MISSING_COVERAGE` / `MISSING_HOP` / `UNKNOWN_NODE` |
| `SegmentStatus` | `COMPLIANT` / `NON_COMPLIANT` / `UNDELIVERED` |
| `ArchivedAttempt` | `.attempt_no` / `.result` / `.reason`（留档原因） |

所有报告对象提供 `to_dict()`，输出仅含原生 JSON 类型，可直接 `json.dumps`。

### 区间工具（`transit_audit.intervals`）

| 函数 | 说明 |
| --- | --- |
| `merge_ranges(ranges)` | 合并重叠/首尾相接的区间，返回有序不相交区间 |
| `intersect_ranges(ranges, bound)` | 与边界取交集并合并，越界部分被截断 |
| `uncovered_ranges(covered, target)` | 求 `target` 内未被覆盖的缺口子区间 |
| `excess_ranges(ranges, bound)` | 求落在 `bound` 之外的越界子区间 |

## 3. 快速示例

```python
from transit_audit import (
    Attempt, Batch, ByteRange, DeliveryResult, HopReceipt,
    NodeKind, RouteNode, Segment, verify_batch,
)

segment = Segment("seg-1", ByteRange(0, 100))
route = (
    RouteNode("relay-a", 1, NodeKind.RELAY),
    RouteNode("region-b", 2, NodeKind.REGION),
)
# 中继把一段拆成两次传送，回执乱序但合并后连续覆盖 [0,100)
receipts = (
    HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(50, 100), ByteRange(0, 50))),
    HopReceipt("region-b", NodeKind.REGION, (ByteRange(0, 100),)),
)
attempt = Attempt(1, route, receipts, DeliveryResult.DELIVERED)
report = verify_batch(Batch((segment,), {"seg-1": (attempt,)}))
print(report.compliant)  # True
```

含越界、缺口、缺跳与失败留档的完整示例见 `scripts/smoke_demo.py`，
运行：`python3 scripts/smoke_demo.py`。

## 4. 运行测试

```bash
python3 -m unittest discover -s tests -v
```

当前共 55 个测试用例，全部通过（Python 3.11.6 实测）。测试文件：

- `tests/test_intervals.py` —— 区间合并、交集、缺口、越界运算，含半开区间
  首尾相接、贴边不越界、完全分离整体越界等边界；
- `tests/test_models.py` —— 模型字段校验：负数/零长区间、布尔值伪装整数、
  跳号不连续、重复节点回执、未知分段键、重复尝试编号等；
- `tests/test_audit.py` —— 核查主流程（见下节）。

## 5. 验证范围

正常与边界场景：

- 单段三跳完整覆盖合规；单字节分段 `[50,51)`；
- 回执乱序、重叠、拆分多次传送后仍能接成连续路线；
- 首尾相接的区间（`[0,50)` + `[50,100)`）判定连续无缺口；
- 相邻分段（`[0,100)` 与 `[100,200)`）同批独立核查互不影响；
- 报告 `to_dict()` 可被 `json.dumps` 序列化。

违规定位场景：

- 单侧/双侧越界，精确报告越界子区间与节点（含节点角色与跳号）；
- 中间缺口、两端缺口，精确报告缺口子区间；
- 整跳缺失按分段完整区间报告 `missing_hop`；
- 路线外节点回执报告 `unknown_node`；
- 同次尝试多个违规按跳号顺序聚合输出。

交错与留档场景：

- 多个分段的尝试编号交错（失败后成功、多次成功取最大编号）；
- 失败尝试单独留档、较早成功尝试标注“取代”后留档；
- 全部失败/无尝试的分段判 `undelivered`，批次 `compliant` 为 `False`；
- 批次内一个分段违规不影响其他分段的独立结论。

## 6. 目录结构

```
transit_audit/
    __init__.py     # 公开 API 导出
    model.py        # 输入数据模型与字段校验
    intervals.py    # 半开区间纯函数工具（含 doctest）
    audit.py        # 尝试选择、逐跳核查、报告模型
tests/
    test_intervals.py
    test_models.py
    test_audit.py
scripts/
    smoke_demo.py   # 端到端合成数据冒烟脚本（含断言）
```

## 7. 设计说明与限制

- 本库只实现题面规定的防御性合规核查：对输入做结构与字段校验、对回执做
  连续性/边界/路线核对，不包含任何网络访问、凭据读取或攻击载荷逻辑。
- 越界属于**合规违规**而非输入错误：构造合法但超出分段范围的回执会进入
  报告的 `out_of_bounds` 违规，而不是抛异常；只有结构性非法输入（负数、
  零长区间、跳号不连续等）才在模型构造时抛 `ValueError`。
- 每跳都要求完整覆盖分段预期范围；逐跳均完整即保证全程路线连续，
  不要求各跳的回执切分方式彼此对齐。
- 隔离模式 `python3 -I` 不会把当前目录加入 `sys.path`（CPython 3.11 行为），
  如需在 `-I` 下运行，请通过 `PYTHONPATH` 显式指定仓库根目录。
