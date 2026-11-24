# 开示核查模块

一个仅使用 Python 标准库的本地开示核查原型。它把调查员陈述中的段落，通过引用区间和段落映射，追到**实际使用的精确材料版本**，再按该版本已批准公开的段落范围、内容指纹、修订承接声明和嵌套来源逐层裁决。

- Python：3.11.6（无需安装第三方依赖）
- 范围约定：段落编号从 **1** 开始，区间两端均包含
- 裁决倾向：来源缺失、引用循环、内容指纹不一致等无法完成核查的情形，均不开示
- 数据：仓库内合成数据均为虚构，不包含真实访谈、凭据或目标系统信息

## 运行方法

在题目工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成案例并输出 JSON 报告：

```bash
python3 -m disclosure_review examples/synthetic_case.json --no-traces
```

也可以在 Python 中使用字典 API：

```python
from disclosure_review import review_dict

report = review_dict({
    "submission": {
        "record_id": "statement-1",
        "version": "1",
        "paragraphs": ["引言", "引用访谈第1段"],
        "citations": [{
            "citation_id": "c1",
            "holder_range": 2,
            "source": "interview-1@2",
            "source_range": 1
        }]
    },
    "records": [{
        "record_id": "interview-1",
        "version": "2",
        "paragraphs": ["已批准的访谈段落。"]
    }],
    "approvals": [{
        "record_id": "interview-1",
        "version": "2",
        "approved_ranges": [1]
    }]
})

print(report["disclosable_ranges"])
print(report["withheld_ranges"])
print(report["remediations"])
```

## 公开 API

主要导出位于 `disclosure_review` 包：

| API | 用途 |
| --- | --- |
| `review_dict(payload)` | 从 JSON 风格字典构建案例并返回可 JSON 序列化报告 |
| `review_payload(payload)` | 同上，但返回 `ReviewReport` 对象 |
| `case_from_dict(payload)` | 只解析并生成 `ReviewCase` |
| `ReviewEngine` | 类型化核查引擎，可配合 `MaterialRegistry` 复用 |
| `review_case(case)` | 类型化函数入口 |
| `Range` | 1-based 闭区间，支持相交、相减、合并 |
| `RecordRef` / `NodeRef` | 精确版本引用 / “版本 + 段落”节点 |
| `VersionedRecord` | 陈述、访谈版本或其他材料版本 |
| `Citation` | 外层段落区间到精确来源版本/区间的引用 |
| `Approval` | 某一精确版本的批准公开范围、留置范围和内容指纹 |
| `CarryForward` | 修订版中新旧段落的显式原样承接映射 |
| `MaterialRegistry` | 记录版本、批准和承接声明的本地注册表 |
| `ReviewReport` / `Segment` / `Remediation` | 结构化结果 |

类型化示例：

```python
from disclosure_review import (
    Approval, CarryForward, Citation, MaterialRegistry,
    Range, RecordRef, ReviewEngine, VersionedRecord,
)

source_ref = RecordRef("interview-lin", "2")
source = VersionedRecord.create(
    "interview-lin", 2,
    ["第一段", "第二段"],
)
statement = VersionedRecord.create(
    "statement", 3,
    ["引言", "摘录"],
    citations=[Citation("c1", Range(2, 2), source_ref, Range(1, 1))],
)

registry = MaterialRegistry([source, statement])
registry.approve(source_ref, [Range(1, 1)], approval_id="ap-1")
report = ReviewEngine(registry).review_current(statement)

assert report.status_for(2) == "disclosable"
```

## JSON 输入格式

### 引用

```json
{
  "citation_id": "c1",
  "holder_range": [2, 4],
  "source": "interview-lin@2",
  "source_range": [1, 3],
  "verbatim": false,
  "paragraph_map": {
    "2": [1],
    "3": [2, 4]
  }
}
```

- `holder_range`：引用/摘录位于当前记录的哪些段落。
- `source`：必须落到 `record_id@version`；这是“实际使用版本”。
- `source_range`：来源中的引用范围。
- 两个范围长度相同且未提供 `paragraph_map` 时，按段落一一映射。
- 长度不同且没有显式映射时，采取保守策略：每个外层段落都依赖整个来源区间；只要其中有未批准段，就留置。
- `paragraph_map` 可表达一对多、跳过无关段或交错映射。

### 批准与内容绑定

字典 API 中的批准若省略 `reviewed_hashes`，会自动用当前提供的材料文本计算 SHA-256 绑定。也可以显式给出：

```json
{
  "record_id": "interview-lin",
  "version": "2",
  "approved_ranges": [[1, 2], 5],
  "withheld_ranges": [3],
  "reviewed_hashes": {
    "1": "sha256:...",
    "2": "sha256:..."
  }
}
```

区间列表的规则是：**外层数组的每一项是一个区间说明**。

- `[1, 3]` 作为 `approved_ranges` 的值：表示两个单点段 `1` 和 `3`。
- `[[1, 3]]`：表示一个完整闭区间 `1-3`。
- `[[1, 2], 5]`：表示 `1-2` 和第 `5` 段。

引用字段中的 `holder_range` / `source_range` 本身是单个区间，因此 `[2, 4]` 表示 `2-4`。

### 修订承接

```json
{
  "target": "statement@2",
  "source": "statement@1",
  "paragraph_map": {"1": 1, "3": 2},
  "declaration_id": "carry-001"
}
```

引擎会重新计算两端文本指纹。只有：

1. 有显式承接声明（默认策略）；
2. 新旧段落指纹完全一致；
3. 旧段落的批准/复核链有效；

该段才能沿用旧结论。指纹不一致会返回 `REWRITTEN_NEEDS_CONFIRMATION`。即使文字恰好相同但没有声明，在提供 `baseline_submission` 的修订场景中也会返回 `CARRY_FORWARD_DECLARATION_REQUIRED`。

若设置：

```json
"policy": {"require_carry_declaration_for_revision": false}
```

则允许按同号段落自动比对基线；指纹一致且基线已批准时可继承，改写或新增段仍需重新确认。

## 嵌套引用规则

核查图同时包含：

- **引用边**：当前段落到来源版本段落；
- **承接边**：修订段落到旧版本段落。

每个到达的材料节点都必须满足：

1. 该精确版本的批准范围覆盖此段，或存在有效的原样承接；
2. 没有有效的明确留置决定；
3. 内容指纹与已复核内容一致；
4. 该段自身的嵌套引用继续通过内层来源核查。

因此，即使外层访谈段落已批准，只要它引用的内层备忘录未批准、缺失或处于循环中，对应的陈述区段仍会被留置。多个引用交错或重叠时，所有依赖取交集式的严格条件：任一来源失败，该段即留置。

## 输出结构

`ReviewReport.to_dict()` 的主要字段：

```jsonc
{
  "submission": {"record_id": "...", "version": "..."},
  "summary": {
    "disclosable_paragraphs": [1, 2],
    "withheld_paragraphs": [3],
    "disclosable_ranges": [[1, 2]],
    "withheld_ranges": [/* ... */]
  },
  "disclosable_ranges": [[1, 2]],
  "withheld_ranges": [
    {
      "range": [3, 3],
      "reason_codes": ["NOT_IN_APPROVED_SCOPE"],
      "reasons": [/* 含 node、chain、detail 的完整原因 */]
    }
  ],
  "segments": [/* 按状态和原因签名切分的有序文本区段 */],
  "paragraphs": [/* 每段状态、原因、来源节点和可选递归 trace */],
  "remediations": [/* 应从哪条记录/版本/段落补起 */],
  "validation_errors": []
}
```

相邻且状态、阻塞原因代码集合相同的段落会合并为一个区段；原因集合不同则拆开，避免把不同审查原因混在同一个范围里。`segment.text` 包含对应的陈述原文。

### 常见原因代码

| 代码 | 含义 |
| --- | --- |
| `NOT_IN_APPROVED_SCOPE` | 到达的材料段落不在批准公开范围内 |
| `EXPLICITLY_WITHHELD` | 批准决定中明确列为留置/不公开 |
| `APPROVAL_CONTENT_MISMATCH` | 当前内容与批准时绑定的指纹不一致 |
| `APPROVAL_BINDING_INCOMPLETE` | 批准范围包含该段，但缺少该段指纹 |
| `REWRITTEN_NEEDS_CONFIRMATION` | 修订、误引或承接指纹不一致，需重新确认 |
| `CARRY_FORWARD_DECLARATION_REQUIRED` | 内容看似未变，但未显式声明原样承接 |
| `MATERIAL_NOT_FOUND` | 引用的材料 ID 完全未提供 |
| `VERSION_NOT_FOUND` | 材料 ID 存在，但引用的精确版本缺失 |
| `PARAGRAPH_NOT_FOUND` | 记录版本存在，但引用段落/范围不存在 |
| `CITATION_CYCLE` | 引用链形成循环 |
| `CARRY_FORWARD_CYCLE` | 承接声明链形成循环 |
| `VERBATIM_EXCERPT_MISMATCH` | 声明逐字摘录，但指纹与实际来源不一致 |
| `INVALID_CITATION_MAP` | 段落映射缺失或越界 |

## 缺失与循环时从哪里补起

`remediations[]` 会去重聚合，并给出：

- `repair_from`：应首先补入或修正的对象；
- `action`：建议的防御性审查动作；
- `chain`：从陈述段落到问题节点的追踪链；
- `affected_submission_paragraphs`：受影响的陈述段落。

判定约定：

- **材料/版本缺失**：`repair_from` 是精确的 `{record_id, version}`。若同 ID 的其他版本存在，`detail.available_versions` 会列出已提供版本，但不会自动用其他版本替代实际使用版本。
- **段落缺失或范围越界**：`repair_from` 是 `{record_id, version, paragraph}`，应补入该段或修正引用范围。
- **引用循环**：`repair_from` 是循环再次进入的入口段落节点；`chain` 会给出完整闭环，例如 `A#p1 → B#p1 → A#p1`。审查应从该入口记录开始，替换其中一条循环引用或补入独立原始来源。
- **承接来源缺失**：`repair_from` 指向缺失的旧版本段落；应补入基线，或把该段按改写内容重新确认。

## 验证范围

测试全部使用本地合成数据，覆盖：

- `tests/test_ranges.py`：闭区间边界、相邻/重叠合并、区间相减、段落编号分组。
- `tests/test_basic_disclosure.py`：正常批准/未批准切分、明确留置优先、内容指纹失配、逐字摘录改写检测。
- `tests/test_nested_and_failures.py`：嵌套来源必须单独批准；材料缺失、版本缺失、嵌套缺失的补起点；双节点引用循环和循环链。
- `tests/test_revision.py`：显式原样承接、未声明同号段不自动继承、虚假承接指纹检测、重新批准、缺失基线、多跳承接链、承接循环、可选自动比对策略。
- `tests/test_interleaving.py`：重叠/交错引用、多来源同时约束、不等长区间的保守依赖、显式段落映射跳过无关段、非法映射。
- `tests/test_json_api.py`：JSON 风格 API、自动指纹绑定和 JSON 序列化。
- `tests/test_example_file.py`：校验 `examples/synthetic_case.json` 的完整范围，并实际运行 CLI 入口。

最近一次实测环境为 Python 3.11.6，命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：`Ran 31 tests ... OK`。

## 设计边界

- 模块只根据输入中显式给出的引用和段落映射追踪；它不会从自然语言中猜测未声明的引用。
- 主要粒度是段落。段落内发生改写时，指纹变化会使整个段落等待重新确认，避免放行未复核的局部文字。
- 不等长范围若没有显式映射，不会猜测对齐，而是保守依赖整个来源区间。
- 本模块不联网、不抓取材料、不处理凭据，也不生成攻击或规避内容；它只实现题面要求的开示核查与留置判断。
