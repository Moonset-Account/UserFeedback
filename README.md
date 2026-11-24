# 审计回执归一模块（receipt_normalizer）

告警规则使用“权限动作名称”，终端原始审计回执提供的却是数字动作码。本模块依据：

1. 动作记录中的**进程实例**与**源序号**；
2. 按进程维护的**映像 / ABI 切换证据**；
3. 按源序号换版的 **ABI 解码表**；

还原“哪个进程、在哪个映像/ABI 下、对什么对象执行了哪个权限动作”。证据缺失或互相冲突时，不猜测 ABI、表版本或子操作，而是返回结构化 `evidence_gaps` 供防护规则复核。

仅使用 Python 标准库；要求 Python 3.11（实测环境为 Python 3.11.6）。

## 运行测试

在当前工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

本地合成演示：

```bash
python3 -m receipt_normalizer.demo
```

## 公开 API

推荐从包根导入：

```python
from receipt_normalizer import (
    ReceiptNormalizer,
    normalize_receipt,
    normalize_receipts,
    ActionRecord,
    ImageSwitch,
    TableVersion,
    TableSchedule,
    ImageTimeline,
    ActionMapping,
    ObjectSpec,
    MultiplexSpec,
    SubOperation,
    NormalizedReceipt,
    EvidenceGap,
    GapCode,
    ABI_NATIVE,
    ABI_COMPAT,
)
```

核心方法：

- `ReceiptNormalizer(tables=None, image_switches=None)`
- `normalizer.normalize(record) -> NormalizedReceipt`
- `normalizer.normalize_all(records) -> list[NormalizedReceipt]`
- `normalizer.add_table_version(table)`
- `normalizer.add_image_switch(switch)`
- `normalize_receipt(record, tables=..., image_switches=...)`
- `normalize_receipts(records, tables=..., image_switches=...)`

数据对象和结果均提供 `to_dict()`；结果可直接 `json.dumps(..., ensure_ascii=False)`。

## 最小示例

```python
from receipt_normalizer import ReceiptNormalizer

tables = [
    {
        "version": "table-2026-11",
        "valid_from_seq": 1,
        "mappings": {
            "native": {
                1001: {
                    "name": "file.read",
                    "target": {"kind": "file_descriptor", "identifier": 0},
                },
                2001: {
                    "name": "ipc.dispatch",
                    "multiplex": {
                        "selector_param": 0,
                        "operations": {
                            1: {
                                "name": "semaphore.acquire",
                                "target": {"kind": "semaphore", "identifier": 1},
                            }
                        },
                    },
                },
            },
            "compat": {
                # 同一个数字码在兼容 ABI 下含义不同。
                1001: {
                    "name": "registry.value.write",
                    "target": {"kind": "registry_value", "identifier": 0},
                },
                2001: {
                    "name": "compat.ipc.dispatch",
                    "multiplex": {
                        # 兼容 ABI 的子操作选择参数位置可以不同。
                        "selector_param": 2,
                        "operations": {
                            9: {
                                "name": "compat.shared_memory.attach",
                                "target": {"kind": "shm_key", "identifier": 1},
                            }
                        },
                    },
                },
            },
        },
    }
]

image_switches = [
    {"process_instance": "p-native", "source_seq": 0,
     "image": "/synthetic/native-app", "abi": "native"},
    {"process_instance": "p-compat", "source_seq": 0,
     "image": "/synthetic/compat-app", "abi": "compat"},
]

normalizer = ReceiptNormalizer(tables=tables, image_switches=image_switches)

receipt = normalizer.normalize({
    "process_instance": "p-native",
    "source_seq": 10,
    "action_code": 2001,
    "params": [1, 4242],
})

assert receipt.action_name == "semaphore.acquire"
assert receipt.target.kind == "semaphore"
assert receipt.target.identifier == 4242
assert receipt.status == "complete"
```

## 输入约定

### 动作记录

推荐字典结构：

```python
{
    "process_instance": "唯一进程实例标识",
    "source_seq": 123,
    "action_code": 2001,       # 原始审计回执中的动作码/入口码
    "params": [1, 4242],       # 列表、元组或命名字典均可
}
```

也可使用 `ActionRecord(process_instance=..., source_seq=..., params=..., action_code=...)`。

题面强调动作记录可能只有“进程实例、源序号、参数”。如果动作码位于参数中，可在表或构造器上配置 `code_selector`：

```python
{
    "version": "embedded-code-table",
    "valid_from_seq": 1,
    "code_selector": "entry",   # 也可以是位置 0 或路径 ["header", "entry"]
    "mappings": {...},
}
```

此时记录可省略顶层 `action_code`，归一器会从 `params["entry"]` 读取动作码；读取失败会产生 `action_code_missing` 缺口。

`process_instance` 应为能区分 PID 复用的唯一实例，例如 `(pid, process_start_id)`。模块只按该不透明键关联证据，不会把不同实例的映像/ABI 混用。

### 映像 / ABI 切换

```python
{
    "process_instance": "p1",
    "source_seq": 50,           # 新状态从该源序号开始（含边界）
    "image": "/usr/bin/compat-worker",
    "abi": "compat",            # 通常为 "native" 或 "compat"，也支持自定义 ABI 名
    "order": 0,                 # 可选：同源序号下已知的先后顺序
}
```

`source_seq` 是新映像/ABI 状态的第一个有效位置。因此动作与切换证据序号相同时，默认使用切换后的状态。若真实数据表示“该序号事件发生在动作之后”，应将切换状态写到下一个位置，或使用显式有效区间表达。

### 解码表

```python
{
    "version": "table-v2",
    "valid_from_seq": 100,       # 从该源序号起生效（含）
    "valid_to_seq": 199,         # 可选；普通换版通常不填，最新 from 版本自动取代旧版
    "code_selector": 0,          # 可选：动作码在 params 中的位置/名称
    "mappings": {
        "native": {
            1001: {
                "name": "file.open",
                "target": {
                    "kind": "path",
                    "identifier": 1,
                    "fields": {"dir_fd": 0, "flags": 2},
                    "optional_fields": {"mode": 3},
                },
            }
        },
        "compat": {
            "1001": "some.compat.action"   # JSON 的字符串数字键也可识别
        },
        "*": {                             # 可选：显式声明的 ABI 无关映射
            7: "common.close"
        },
    },
}
```

参数引用规则：

- 整数 `0`：位置参数 `params[0]`；
- 字符串 `"path"`：命名参数 `params["path"]`；
- 序列 `["header", "code"]` 或 `[0, "id"]`：嵌套路径；
- `identifier` 缺失会产生 `target_parameter_missing`，但已确定的动作名仍会保留；
- `fields` 中声明的是必填附加属性，`optional_fields` 仅在存在时补充。

### 单入口多种子操作

当一个 ABI 入口码承载多个子操作时，在映射中给出选择参数：

```python
2001: {
    "name": "ipc.dispatch",       # 入口名，输出在 entry_name
    "multiplex": {
        "selector_param": 0,      # 题面要求：按映射表指定的参数判定
        "operations": {
            1: {"name": "semaphore.acquire", "target": {...}},
            2: {"name": "shared_memory.attach", "target": {...}},
        },
    },
}
```

若选择参数缺失，输出 `selector_parameter_missing`；若值不在表中，输出 `suboperation_unknown`。这两种情况下不会猜成某个子操作。

## 选择与判定规则

对每条动作记录：

1. 用 `process_instance` 找到该进程的切换证据；
2. 用动作的实际 `source_seq` 选择 `source_seq <= action.source_seq` 的最新映像/ABI 状态；
3. 用同一个 `source_seq` 选择覆盖该位置的解码表版本；
4. 用 `(table_version, abi, action_code)` 找动作映射；
5. 若是复合入口，再用该映射指定的参数选择子操作；
6. 按 `ObjectSpec` 提取作用对象；
7. 任何不确定项都追加到 `evidence_gaps`，并将 `status` 标为 `evidence_gap`、`needs_review=True`。

选择只取决于动作的实际源序号，不取决于记录、切换事件或表版本传入列表的顺序。批量结果默认保持输入记录顺序，便于调用方对应原始回执。

## 交错与边界处理

已覆盖并测试：

- 主机上原生与兼容进程同时运行，相同动作码映射到不同动作/对象；
- 同一进程在源序号边界切换映像和 ABI；部分切换证据只提供映像或 ABI 时，另一字段沿用上一已知状态，从未提供则输出缺口；
- 解码表在源序号边界换版，乱序传入仍按序号选择；
- 多个进程、ABI、复合入口选择参数位置差异和表版本边界交错；
- 同一 PID 被不同进程实例复用时不串映像/ABI；
- 同源序号有多条切换证据：
  - 完全一致：去重后使用；
  - 有显式不同 `order`：按已知顺序取最后状态；
  - 同顺序且映像或 ABI 冲突：输出冲突缺口；若仅映像冲突而 ABI 一致，仍可安全解码动作，但结果必须复核；
- 动作早于第一条映像证据、进程实例完全未知；
- 没有表覆盖源序号、表区间重叠、同一起点存在不同版本；
- 表的版本标签为 `unknown`：即使映射内容可用，也输出表版本证据缺口；
- 动作码、子操作选择参数、目标参数缺失或值未登记；
- 证据晚于动作到达：可先得到带缺口的临时结果，加入证据后对同一记录重新归一即可消除缺口。

流式使用时，`normalize()` 只依据当前已经加载的证据判断；需要“证据到齐后的最终结果”，可在补充 `add_image_switch()` / `add_table_version()` 后重新调用 `normalize(record)`，或最后使用 `normalize_receipts(..., image_switches=..., tables=...)` 批量计算。

## 输出结构

```python
NormalizedReceipt(
    status="complete" | "evidence_gap",
    source_seq=...,
    process=ProcessInfo(process_instance=..., image=..., abi=..., evidence_seq=...),
    action_code=...,
    action_name="权限动作名或 None",
    entry_name="复合入口名或普通动作名",
    target=Target(kind=..., identifier=..., attributes=...),
    table_version="...",
    selector_value=...,
    params=...,
    gaps=(EvidenceGap(...), ...),
)
```

`to_dict()` 同时提供规则常用别名字段：

- `permission_action` 与 `action_name` / `action.permission_action`；
- `object` 与 `target`；
- `action.name` 嵌套动作对象。

### 缺口码（`GapCode`）

| code | 含义 |
| --- | --- |
| `process_evidence_missing` | 没有该进程实例的映像/ABI 证据 |
| `image_unknown` | 序号过早、证据未给出映像或位置不可比较 |
| `image_conflict` | 同一有效位置存在多个不一致映像 |
| `abi_unknown` | 无法确定 ABI |
| `abi_conflict` | 同一有效位置存在多个冲突 ABI |
| `table_version_unknown` | 无表、无覆盖版本或版本标签未知 |
| `table_version_conflict` | 表版本起点冲突或有效区间重叠 |
| `action_code_missing` | 原始记录和 `code_selector` 都无法提供动作码 |
| `action_unmapped` | 当前表/ABI 中没有该动作码 |
| `selector_parameter_missing` | 复合入口的子操作选择参数缺失 |
| `suboperation_unknown` | 选择参数值未在子操作表中登记 |
| `target_parameter_missing` | 映射要求的对象标识或属性参数缺失 |

## 验证范围与实测结果

截至本次实现，使用本地合成数据执行：

```text
python3 -m unittest discover -s tests -v
# 最近一次实测：Python 3.11.6，Ran 34 tests ... OK
```

测试文件：

- `tests/test_core_normalization.py`：原生/兼容映射、对象提取、复合入口、映像边界、表版本边界、类型化 API；
- `tests/test_evidence_gaps.py`：缺失、冲突、重叠、未知值、参数缺失、JSON 序列化和输出别名；
- `tests/test_interleaving.py`：多进程/双 ABI/表版本交错、乱序输入、迟到证据重算、PID 复用和元组输入；
- `tests/test_demo.py`：断言合成演示的 JSON 输出包含预期完整结果与证据缺口。

演示命令也已实际运行，输出为可解析 JSON，包含完整归一结果和一个未知进程的证据缺口。

未连接真实终端、未联网、未读取凭据，也未加载任何外部依赖；因此没有进行真实审计回执或真实规则集验证。所有动作码、映像路径和映射表均为测试内合成数据。
