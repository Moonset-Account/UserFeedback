# 错误处理流程的诊断公开静态检查器

本模块面向“作业代理把错误码和可显示诊断位置返回调用方”的场景，对受限错误处理流程图做纯本地静态分析。它会：

- 从入口计算可达响应出口；
- 传播异常包装、恢复/替换、普通恢复响应、聚合错误和身份筛选；
- 按调用方身份核对错误码、诊断标识、来源和响应字段；
- 给出每个出口的违规项与只保留可公开内容的安全响应投影；
- 支持从响应出口反向切片，并按身份筛选真正传播到该出口的诊断。

仅使用 Python 标准库，目标版本为 Python 3.11.6，不访问网络、不读取凭据、不安装依赖。

## 运行测试

在当前工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

也可以运行命令行检查本地合成图：

```bash
python3 -m static_check examples/job_error_flow.json \
  --caller agent-1 \
  --public diag.input.path,diag.job.id,diag.recovery.id \
  --codes E_IO,E_JOB
```

当检查失败时，CLI 返回退出码 `1`；`--json` 可输出完整报告。

## 公开 API

### `audit_flow(graph, public_diagnostics=None, caller=None, ...) -> AuditReport`

最常用的位置参数形式是：

```python
from static_check import audit_flow

report = audit_flow(graph, {"D1", "D2"}, "agent-a")
```

也可以显式提供策略：

```python
from static_check import DisclosurePolicy

policy = DisclosurePolicy(
    caller={"id": "agent-a", "roles": ["ops"]},
    public_diagnostics={
        "D_PUBLIC": True,
        "D_ALICE": ["alice"],
        "D_OPS": {"roles": ["ops"], "sources": ["approved-source"]},
    },
    public_codes={"E_PUBLIC": True},
    allowed_fields={"diagnostics": True, "stack": ["security-team"]},
)
report = audit_flow(graph, policy=policy)
```

常用结果接口：

- `report.ok` / `report.valid` / `report.passed`：所有**可达**出口是否无错误级违规；
- `report.exits`：全部响应出口，包括不可达出口的 `reachable=False` 记录；
- `report.exit(exit_id)`：读取单个出口；
- `report.reachable_exits`；
- `report.violations` / `report.errors` / `report.all_findings`；
- `report.codes()`：违规代码集合；
- `report.safe_responses`：按出口 ID 分组的安全投影；
- `report.to_dict()`：JSON 友好的完整报告。

`ExitReport` / `ResponseScenario` 还提供：

- `allowed_diagnostics`
- `blocked_diagnostics`
- `safe_response` / `safe_responses`
- `scenarios`：同一出口收到多个可选传播状态时，每条交错路径一个场景。

### 反向身份筛选

```python
from static_check import reverse_identity_filter, reverse_scan

# 所有可达出口：dict[exit_id, ReverseSlice]
slices = reverse_identity_filter(graph, "agent-a", {"D1": True})

# 单个出口
slice_ = reverse_scan(graph, "error_exit", caller="agent-a",
                      public_diagnostics={"D1": True})

slice_.nodes                 # 语义反向经过的节点
slice_.diagnostics           # 实际传播到出口的诊断
slice_.allowed_diagnostics   # 该身份可看的诊断
slice_.blocked_diagnostics
```

这里的反向切片不是简单沿所有图边回溯：若恢复分支已经替换或丢弃原故障，原故障中的诊断不会进入语义切片。

## 输入图格式

图可以是 `dict`、JSON 字符串、JSON 文件路径，或节点列表。规范形式如下：

```json
{
  "entry": "start",
  "nodes": [
    {"id": "start", "type": "start"},
    {
      "id": "io_fault",
      "type": "raise",
      "code": "E_IO",
      "diagnostics": [
        {"id": "diag.input.path", "source": "input-validator"}
      ]
    },
    {
      "id": "wrap",
      "type": "wrap",
      "code": "E_JOB",
      "diagnostics": ["diag.job.id"]
    },
    {"id": "error_exit", "type": "response", "kind": "error"}
  ],
  "edges": [
    {"from": "start", "to": "io_fault"},
    {"from": "io_fault", "to": "wrap", "branch": "error"},
    {"from": "wrap", "to": "error_exit", "branch": "error"}
  ]
}
```

边的 `branch` 可为：

- `error` / `exception` / `catch`：只传错误状态；
- `normal` / `success` / `ok`：只传普通状态；
- `member`：作为聚合错误成员；
- `any`（默认）：不按状态种类过滤。

也支持 `[from, to, branch?]` 简写边，以及节点上的 `next`、`on_error`、`error_to`、`success_to` 等便捷字段。没有显式边列表时，节点数组顺序会被视为流水线。

### 节点类型

| 类型 | 语义 |
| --- | --- |
| `start` / `task` / `step` | 入口或普通步骤；`task` 也可用 `error`/`raises` 声明一个可能故障 |
| `raise` / `throw` / `fault` | 创建新的故障值 |
| `wrap` | 用外层 `code`/`diagnostics` 包装传入错误；默认保留 `cause` |
| `replace` / `recover` | 从恢复分支替换故障；默认丢弃原故障 |
| `recover` | 给 `replacement` 时替换故障；否则可恢复为普通响应 |
| `aggregate` | 汇总多个故障成员 |
| `filter` / `redact` | 在继续传播前按当前策略移除不可见诊断/码 |
| `response` / `exit` | 响应出口；`kind` 为 `error` 或 `normal` |

### 恢复与普通响应

替换故障默认不保留原故障：

```json
{
  "id": "recover_and_replace",
  "type": "replace",
  "code": "E_FALLBACK",
  "diagnostics": ["diag.fallback"]
}
```

只有显式设置 `"keep_cause": true`（或 `cause: "@input"`）才会把原错误作为内层上下文继续检查；设置 `carry_diagnostics: "all"` 或 ID 列表才会继承指定诊断。

恢复到普通响应时，默认不携带故障诊断。只有 `propagate_diagnostics` 显式指定的诊断，或恢复节点自己新增到普通状态的诊断，才被视为“实际传播”。如果响应字段引用了未传播的已声明 ID，会产生 `DIAGNOSTIC_NOT_PROPAGATED`。

### 响应字段与来源

响应节点可用 `permitted_fields` / `allowed_fields` 声明该出口许可字段，用 `fields` 描述实际字段来源：

```json
{
  "id": "ok_exit",
  "type": "response",
  "kind": "normal",
  "permitted_fields": ["status", "trace"],
  "fields": {
    "status": {"literal": "recovered"},
    "trace": {"source": "diag.trace.id"}
  }
}
```

紧凑形式也支持：

```json
{"diagnostics": ["D1", "D2"]}
```

特殊引用包括：

- `@code`：当前最外层错误码；
- `@diagnostics`：当前传播错误树或普通状态中的全部诊断；
- `@cause`：包装的内层错误；
- `@members`：聚合成员；
- `@error`：完整安全错误投影。

裸字符串若匹配当前传播中的诊断、或已在策略中声明，会被当作诊断来源；其他裸字符串按字面量处理。为避免歧义，字面量建议写成 `{"literal": "ok"}`，显式来源写成 `{"source": "D1"}` 或 `"ref:D1"`。

## 策略声明形式

`public_diagnostics` 接受：

```python
{"D1", "D2"}                       # 全部对所有身份公开
["D1", {"id": "D2", "callers": ["agent-a"]}]
{
  "D1": ["agent-a", "agent-b"],
  "D2": True,
  "D3": {"public": True, "sources": ["approved-source"]},
  "D4": {"roles": ["ops"]},
}
```

调用方身份可以是字符串，也可以是：

```python
{"id": "agent-a", "roles": ["ops"]}
```

错误码可用 `public_codes` 单独声明；若不传，`audit_flow` 会在同一个 `public_diagnostics` 声明映射/集合中查找错误码。图中的错误码对象或节点也可显式标记 `"public": true` / `"visibility": "public"`。注意：外层码公开只允许该码本身，不会授权内层错误码、内层诊断或错误来源。

## 聚合规则

聚合错误递归检查每个成员。只有成员的错误码、直接/内层诊断、cause 和嵌套聚合全部通过时，该成员才是 `independently_passable` / `admissible`，并进入安全投影的 `members`。未独立通过的成员整体不保留；普通错误中的违规诊断叶子则会从 `diagnostics` 投影中移除。

## 主要违规代码

| 代码 | 含义 |
| --- | --- |
| `UNDECLARED_DIAGNOSTIC` | 诊断标识没有公开声明 |
| `DIAGNOSTIC_NOT_VISIBLE_TO_CALLER` | 已声明，但身份、角色或来源不允许当前调用方 |
| `DIAGNOSTIC_NOT_PROPAGATED` | 响应引用了未实际传播到该出口的诊断 |
| `UNDECLARED_ERROR_CODE` | 错误码没有公开依据 |
| `ERROR_CODE_NOT_VISIBLE_TO_CALLER` | 错误码不对当前身份公开 |
| `FIELD_NOT_PERMITTED` | 字段不在出口许可字段列表中 |
| `FIELD_NOT_AUTHORIZED_FOR_CALLER` | 字段策略不允许该身份 |
| `UNSUPPORTED_FLOW_CYCLE` | 图含无法在受限静态模型中可靠汇总的环 |
| `INVALID_GRAPH` / `INVALID_EDGE_*` / `NO_ENTRY` | 图结构不合法 |

## 验证范围

测试全部使用本地合成图，覆盖：

1. 外层公开错误码不能给内层上下文背书；
2. 按身份/角色/来源允许或拒绝诊断；
3. 异常包装及内层 cause 的递归检查；
4. 恢复分支替换故障时默认丢弃原故障，以及显式保留 cause/诊断；
5. 恢复为普通响应时“未显式传播即不存在”；
6. 响应引用已声明但未传播诊断的边界；
7. 多故障聚合中只保留独立通过检查的成员；
8. 多出口、不可达出口、同一出口的多条交错可选路径；
9. 反向切片与按身份筛选；
10. 字段许可、来源许可、内联 JSON、空图、空聚合、不可分析环、坏边和匿名调用方。

最近一次实际运行：

```text
python3 -m unittest discover -s tests -v
Ran 28 tests ... OK
```

CLI 合成样例也已分别用宽松策略（退出码 `0`）和严格策略（输出违规并返回 `1`）实际执行验证。未进行任何真实目标联网、凭据读取或外部服务测试。
