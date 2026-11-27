# 文书签章修订检查器（sigcheck）

审核岗视角的签章增量保存越界检查：给定一份按**增量修订**演化的文书模型
（字节范围、版本化对象、页面/表单引用、已验真签章摘要），定位每份签章在
哪一次增量保存后首先失去认可，并给出一条从页面根到越界对象的引用链。

纯 Python 3.11 标准库实现，无第三方依赖；不做任何密码学验真，签章摘要的
真伪由调用方负责，本工具只消费“已验真”的结果并做**结构性**越界判定。

## 判定模型

四类输入与题面一一对应：

| 输入 | 模型字段 | 含义 |
| --- | --- | --- |
| 按字节范围标记的修订 | `revisions` | 每次增量保存的半开字节区间 `[start, end)`，按起点严格递增、互不重叠 |
| 每版新增/替换的对象 | `objects` | 同一 `object_id` 在不同修订再次出现即“替换”；`digest` 相同表示内容未实际变化 |
| 页面到内容/表单值的引用 | `references` | 带生命周期的有向边：`body`（普通页面正文）或 `field`（表单字段到值对象），可用 `removed_in` 标记移除 |
| 已验真签章摘要 | `signatures` | `certifying`（认证，可声明 `allowed_fields`）或 `approval`（认可，无豁免） |

核心规则：

1. **版本化可达性**：每次修订结束时按当时存活的引用边重建引用图，从
   `page_root_ids` 做 BFS。`body` 模式只沿正文边；`full` 模式同时沿
   正文边与字段边。悬空边（目标对象该版不存在）不入链。
2. **签章覆盖范围**：签章创建修订 `s` 当时，沿 `body` 边可达的对象
   （页面根 + 至少经过一跳正文边的对象；仅经字段边可达的表单字典/值对象
   不在内），并排除所有签章自身对象。覆盖范围在签章创建时固定。
3. **允许后填字段**：认证签章允许的字段，其值对象在“修订当时”仍只经
   `field` 边可达时，替换记 `allowed_field_fill`（info，不越界）。
   一旦该对象同时被 `body` 边可达（同一对象被普通页面正文引用），
   字段豁免失效，替换即 `object_modified` 越界——包括“同一修订里先填值、
   后接正文边”的情况。
4. **认可签章**没有任何字段豁免，覆盖对象的真实替换一律越界。
5. **字节范围侵入**：后续修订区间与签章 `byte_range`（全局字节坐标）
   重叠时记 `byte_range_tamper` 越界。
6. **正文结构移除**：签章当时已存在、源对象在覆盖范围内的引用边被移除，
   记 `removed_reference` 越界，链路由签章当时的引用图给出。
7. **历史不可消除**：所有发现按修订顺序累积。后来把文字改回原样，只产生
   新的替换事件，早先的越界仍保留，`earliest_violation` 不变。
8. **新签章独立计算**：后来新增的签章从自己所在修订的实际可达关系重新
   计算覆盖范围，旧签章的越界不会“传染”给它，反之亦然。
9. 覆盖范围外的全新对象、纯新增引用边属于增量追加，不构成越界。

## 公开 API

```python
from sigcheck import (
    load_document,      # dict / JSON 字符串 -> Document（含结构校验）
    inspect_document,   # Document -> InspectionReport
    DocumentModelError, # 输入模型不合法时抛出
    # 数据类
    Revision, ObjectVersion, ReferenceEdge, Signature, Document,
    InspectionReport, SignatureAssessment, Finding, Hop,
)
```

常用入口：

- `load_document(payload) -> Document`
- `inspect_document(document) -> InspectionReport`
- `InspectionReport.violated` / `.earliest_violation` /
  `.assessment_for(signature_id)` / `.to_dict()` / `.to_json()`
- `Finding.chain`：`Hop` 元组，即一条从页面根到越界对象的引用链；
  `Finding.rule_id` 取值 `byte_range_tamper` / `object_modified` /
  `removed_reference`（越界）或 `benign_rewrite` / `allowed_field_fill` /
  `ineffective_allowance`（信息）。

模型层另提供 `Document.assemble_unchecked(...)`，**仅供检查器层测试**
构造模型层本应拒绝的畸形字节区间，生产代码不要使用。

### 输入 JSON 形态

见 `tests/fixtures/sample_document.json`。顶层字段：

```json
{
  "revisions":   [{"revision_id": "r0", "start": 0, "end": 100}],
  "objects":     [{"object_id": "P", "revision_id": "r0", "digest": "d-P"}],
  "references":  [{"source_id": "P", "target_id": "C", "kind": "body",
                   "added_in": "r0", "removed_in": null, "field_name": null}],
  "signatures":  [{"signature_id": "S1", "revision_id": "r1",
                   "byte_range": [100, 180], "signature_object_id": "SIG1",
                   "sig_type": "certifying", "allowed_fields": ["f1"]}],
  "page_root_ids": ["P"]
}
```

## 运行方法

运行全部单元测试（50 个用例，纯标准库）：

```bash
python3 -m unittest discover -s tests -v
```

CLI（读 JSON 模型，输出 JSON 报告；退出码 `0` 无越界 / `1` 发现越界 /
`2` 输入模型不合法）：

```bash
python3 -m sigcheck.cli tests/fixtures/sample_document.json
python3 -m sigcheck.cli tests/fixtures/sample_document.json -o report.json
```

综合端到端场景（一份连续 6 版增量保存的文书，内含字段填值、字段对象升格
为正文、正文篡改、改回原样、后加认可签章等交错情节，带 9 条断言）：

```bash
python3 tests/scenario_end_to_end.py
```

## 验证范围

所有验证均基于本地合成数据，无网络访问、无外部依赖、无真实文书。

- `tests/test_model.py`：模型校验（重复/重叠/乱序修订、未知引用、
  field 边缺字段名、认可签章误声明豁免、byte_range 越界等）与版本化
  状态重建（对象版本追踪、边的 add/remove 生命周期）。
- `tests/test_reachability.py`：BFS 可达性、body/full 两种模式、
  菱形图最短链、根空链、排除集、悬空边、多页面根、边移除后消失。
- `tests/test_inspector.py`：
  - 正常流程：无变化干净、追加新对象、允许字段填值（含签章后才接上的
    空字段）；
  - 边界：字段对象同时被正文引用、正文对象替换的最早修订与引用链、
    同摘要良性重写、改回原样后早先越界仍可查、认可签章拒绝填值、
    签章对象自身不在正文覆盖内；
  - 交错：先接正文边再改值、同版填值+接正文边、多次越界取最早、
    后加签章独立计算、新签章所在修订冒犯旧签章、字节区间重叠、
    正文引用边移除；
  - 报告：JSON 可序列化、发现按修订顺序排列。
- `tests/test_cli.py`：CLI 退出码、stdout/文件输出、干净文书与坏模型。
- `tests/scenario_end_to_end.py`：题面三个关键要求的连续情节验证
  （改回原样仍可查、后加签章独立、字段/正文同对象的豁免失效）。

## 边界与取舍（如实记录）

- 本工具只做**结构性**判定：对象 digest 变化、引用边变化、字节区间
  重叠。密码学摘要验真、证书链校验不在范围内，输入签章一律视为已验真。
- 签章自身对象被排除在正文覆盖之外；对签章对象本身的替换不会被误报为
  正文越界，其真伪应由字节范围/密码学环节负责。
- “纯新增引用边”本身不越界（增量保存允许追加结构）；只有当新边使一个
  允许字段对象在内容被替换的同一修订成为正文对象时，才按豁免失效判越界。
- 把被污染对象改回原摘要时，由于相对**上一版** digest 仍不同，会再留
  一条 `object_modified`；首次越界的修订号不受影响，这正是“历史可查”
  的要求。
