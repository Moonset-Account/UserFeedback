# budget_guard — 归档导入前的受限操作图资源预算静态分析

导入服务把归档元数据解析成一组操作描述后，`budget_guard` 在**真正解包之前**
静态读取这张受限操作图，给出两个相互独立的**可证明资源上界**：

- `total.bytes`：按“实际新分配对象”累计的内存字节上界；
- `total.records`：展开记录数上界。

无法证明有限上界（缺少外部证据、递归可能无界、静态调用环等）的分支会得到
`None`，并保留**原因码、文字原因和操作位置 `pos`**；导入端可以据此拒绝整张图，
也可以按 `branch_id` **只拒绝某个分支后重新核算**。

本模块是纯防御性质量门禁：只解析内存中的 dict / 本地 JSON 并做整数运算，
**不执行任何操作图节点、不解包归档、不访问网络、不安装依赖、不读取凭据**。

- 运行环境：Python 3.11.6（3.10+ 均可），仅标准库
- 测试命令：`python3 -m unittest discover -s tests -v`

---

## 30 秒快速开始

```python
from budget_guard import analyze, evaluate

graph = {
    "op": "seq", "id": "root",
    "steps": [
        {"op": "alloc", "bytes": 4096, "records": 1, "share": "manifest"},
        {"op": "segments", "id": "seg",
         "count": {"evidence": "max_segments"},
         "body": {"op": "alloc", "bytes": 512, "records": 1}},
    ],
}
evidence = {"max_segments": 64}          # 来自受信任的外部证据/清单上限

report = analyze(graph, evidence)
print(report.total)                       # Cost(bytes=36864, records=65)

decision = evaluate(report, max_bytes=1 << 20, max_records=10_000)
print(decision.allowed)                   # True / False
```

当证据缺失时：

```python
report = analyze(graph, evidence={})
print(report.total.bytes)                 # None（无法证明有限上界）
for v in report.rejectable_branches:
    print(v.branch_id, v.pos, v.reason_codes)
# seg <path> ('UNBOUNDED_COUNT',)

# 导入端决定拒绝 seg 分支后重算：
report2 = report.reject_branches("seg")
print(report2.total)                      # Cost(bytes=4096, records=1)
```

也可以直接跑仓库自带示例：

```bash
python3 -m budget_guard examples/operation_graph.json \
    --evidence examples/evidence.json \
    --max-bytes 65536 --max-records 100
```

---

## 公开 API

包 `budget_guard` 导出：

| 名称 | 说明 |
| --- | --- |
| `analyze(graph, evidence=None, *, reject=()) -> AnalysisReport` | 主入口，分析操作图 |
| `evaluate(report, *, max_bytes=None, max_records=None, strict=False) -> GateDecision` | 与预算比较并给门禁结论 |
| `load_operation_graph(source)` | 从 dict/list 或 JSON 路径加载图 |
| `load_evidence(source=None)` | 从 dict 或 JSON 路径加载证据 |
| `analyze_graph` / `check_budget` | 上述两个主函数的语义化别名 |
| `Cost / Issue / NodeReport / BranchVerdict / AnalysisReport / GateDecision` | 数据模型 |
| `GraphValidationError` | 图不符合受限 schema（元数据本身错误） |
| `B` | 全部原因码常量 |

`analyze` 的 `graph` 可以是：

1. 单个节点 dict；
2. 顶层操作描述**数组**（等价于隐式 `seq`）；
3. `{"root": <节点或 definitions 中的名字>, "definitions": {名字: 节点}}`；
4. 上述任意形态的本地 JSON 文件路径。

`evidence` 可以是 dict、JSON 路径或 `None`。证据条目支持两种形式：

```json
{"max_segments": 64,
 "max_depth": {"limit": 4, "source": "signed-manifest:depth"}}
```

`AnalysisReport` 常用成员：

- `.total: Cost`（`.bytes` / `.records`，`None` 表示无有限可证明上界）
- `.blocking_issues` / `.warnings` / `.issues`（含已静音问题）
- `.rejectable_branches: list[BranchVerdict]`（`branch_id/pos/reason_codes/reasons/cost`）
- `.known_region_ids`、`.find_region(id)`、`.root.walk()`
- `.reject_branches(*ids) -> AnalysisReport`
- `.as_dict()`（可直接 `json.dumps(..., ensure_ascii=False)`）

`GateDecision` 常用成员：`.allowed`、`.violations`、`.blocking_issues`、
`.warnings`、`.rejectable_branches`、`.as_dict()`。

---

## 操作图节点（受限 schema）

每个节点是一个 JSON 对象，通用字段：`op`（必填）、`id`、`pos`
（也接受 `location` / `at`）。没有显式 `pos` 时，分析器合成唯一 JSON 路径，
保证每个 Issue 都有非空操作位置。

所有“整数上界字段”都接受非负整数字面量，或证据引用
`{"evidence": "证据键"}`。

| `op`（含别名） | 关键字段 | 成本语义 |
| --- | --- | --- |
| `alloc`（`object`/`allocate`） | `bytes`（默认 0）、`records`（默认 1）、`share`/`shared`/`key` | 一次新分配 |
| `seq`（`sequence`/`block`，字段 `steps`/`children`/`ops`） | 有序节点数组 | 各步成本**相加**，并顺序传递共享环境 |
| `branch`（`choice`/`if`/`switch`，`arms`/`cases` 或 `then`/`else`） | 互斥臂 `{name?, id?, node}` | 各臂逐维度取 **max，绝不相加** |
| `segments`（`segment_expand`/`repeat`/`loop`） | `count`（必填上界）、`body`/`each`（必填） | `count × body` |
| `recurse`（`subpackage`/`recursion`） | `depth`（必填层数上界）、`fan_out`（默认 1）、`body` 或 `target` | 有界递归树实例数 × 每包成本 |
| `call`（`invoke`） | `target`（definitions 名称） | 上下文敏感地展开命名子包；静态环判为无界 |
| `ref`（`reference`） | `share`/`key`（可省略=匿名）、`bytes`、`records`（新分配时默认 1）、`reuse_records`（共享时默认 0） | 证明共享则 0 内存；否则保守按新分配 |
| `reuse_if`（`reuse`/`conditional_reuse`） | `key`、`reuse` 节点或 `reuse_records`、`fresh` 节点或 `bytes/records` | reuse/fresh 互斥取 max；证明必命中时只计 reuse |
| `share`（`mark_shared`/`pin`） | `key` | 0 成本，标记对象从此位置可被具名复用 |

### 递归实例数公式

`depth = d` 为一条根到叶链上的最大包层数（`d=1` 只有根包），
`fan_out = f` 为每个包的直接子包数上界。实例总数上界：

```
I(d, f) = 1 + f + f² + … + f^(d-1)
        = d              if f = 1
        = (f^d - 1)/(f-1) otherwise
```

使用闭式整数运算，证据值即使是 `10^12` 也不会逐层循环。`depth=0` 成本为 0；
`depth=1` 时即使 `fan_out` 证据缺失，实例数仍可证明为 1（该证据问题保留但不阻断）。

对于 `definitions` 中未经 `recurse` 的静态调用环（如 `a -> b -> a`），
分析器报 `RECURSION_CYCLE` 并给 `None` 上界——受限图要求用带
`depth/fan_out` 证据的 `recurse` 显式建模有界子包递归。

---

## 三条核心计算规则

### 1. 互斥分支取 max，不相加

`branch` / `reuse_if` 的臂由运行时条件选择，静态分析保守假设每臂都可能执行，
因此逐资源维度取最大值。两个维度独立取 max（最大值可能来自不同臂），
仍是合法的可证明上界。

### 2. 只为“实际新分配”计数；共享需要支配性证据

分析器维护“在此操作位置之前必定已解包/注册的共享键集合”：

- `alloc`/`share` 用 `share: "key"` 注册；
- `seq` 顺序传递该集合；
- `branch` 之后只保留**所有臂都注册**的键（交集）——只在某一臂注册的对象，
  分支后不能假定存在；
- 次数/深度只是**上界**，实际可能为 0，因此循环/递归体里注册的键不会被当作
  区域之后必定可用（保守，只会多算不会漏算）；
- `call` 继承调用点的共享环境。

于是：

- **匿名 `ref`（没有 `share`）永远不能假定共享**：若给了 `bytes`，
  保守按一次新分配计数并给 `ANONYMOUS_REFERENCE` 警告；若没给大小，
  内存维度为 `None` 并给 `MISSING_OBJECT_SIZE` 阻断问题；
- 具名 `ref` 找不到支配性注册时，同样保守按新分配计数
  （`UNPROVEN_SHARING` 警告）；
- 被支配性分配证明的具名 `ref` 成本为 `Cost(0, reuse_records=0)`。

### 3. `None` 上界与“保留原因 + 位置 + 可拒绝”

`Cost.bytes/records` 为 `None` 表示现有证据下没有有限可证明上界。
每个导致 `None` 的事实都对应一条活动的 blocking `Issue`，含：

```
code, severity, message, pos, node_id, region_id, evidence_key, active, mute_reason
```

原因码：

| code | 级别 | 含义 |
| --- | --- | --- |
| `UNBOUNDED_COUNT` | blocking | 分段次数缺少有效证据上界 |
| `UNBOUNDED_RECURSION` | blocking | 递归深度/扇出缺少有效证据上界 |
| `RECURSION_CYCLE` | blocking | 命名调用形成无深度保护的静态环 |
| `UNKNOWN_TARGET` | blocking | `call`/`recurse target` 引用的 definition 不存在 |
| `MISSING_OBJECT_SIZE` | blocking | 不能证明共享且没有保守新分配大小 |
| `MISSING_EVIDENCE` / `INVALID_EVIDENCE` | blocking | 普通数值字段的证据缺失/非法 |
| `ANONYMOUS_REFERENCE` | warning | 匿名引用已保守按新分配计数 |
| `UNPROVEN_SHARING` | warning | 具名引用无支配性共享证据，已按新分配计数 |
| `REUSE_WITHOUT_EVIDENCE` | warning | `reuse_if` 的键在此处未证明已解包，已计入 fresh 臂 |
| `BRANCH_REJECTED` | info | 该 region 被导入端显式拒绝 |
| `BYTES_BUDGET_EXCEEDED` / `RECORDS_BUDGET_EXCEEDED` / `UNPROVABLE_BOUND` | gate | 预算违规 |

**拒绝闭环**：`report.rejectable_branches[i].branch_id` 就是可传给
`analyze(..., reject=[...])` 或 `report.reject_branches(...)` 的 region id。
被拒绝区域仍被完整分析（原因保留），但成本按 0 回传父节点，原始 blocking
Issue 变为 `active=False, mute_reason="rejected:<id>"`；因此互斥 `max`
会自动改取剩余臂。拒绝不存在的 id 会抛 `ValueError`，避免拼写错误被静默忽略。

可证明不可达的区域也会被静音保留：`count=0`、`depth=0`
（`mute_reason="zero_iterations"/"zero_depth"`），以及共享被证明必命中时的
`reuse_if.fresh` 臂（`mute_reason="sharing_proven"`）。

---

## 门禁 `evaluate`

```python
decision = evaluate(
    report,
    max_bytes=65536,      # None 表示不限制该维度
    max_records=100,
    strict=False,         # True 时任何活动 warning 也拒绝
)
```

放行条件：无活动 blocking Issue；设置了预算的维度必须有限且不超；
`strict=True` 时还要求无活动 warning。上界为 `None` 却设置了预算，会得到
`UNPROVABLE_BOUND` 违规（不能保证满足预算）。

---

## 命令行

```bash
python3 -m budget_guard <graph.json> \
    [--evidence ev.json] \
    [--max-bytes N] [--max-records N] \
    [--reject REGION_ID] ... \
    [--strict] [--format text|json]
```

退出码：`0` 允许；`3` 门禁拒绝（超预算/blocking/strict 警告）；
`4` 输入图或证据不合法、未知 reject id；`2` 参数错误。

示例图（`examples/operation_graph.json` + `examples/evidence.json`）的
手工推导：共享 manifest `4096B/1rec`；segmented 臂 `64×512 = 32768B/64rec`
（其中对 manifest 的引用为 0）；nested 臂 `15×1024 = 15360B/15rec`；
互斥取 max = `32768B/64rec`；末尾匿名引用 `256B/1rec`。
**总计 `37120 bytes / 66 records`**，与测试和 CLI 实际输出一致。

拒绝两个证据区域后的实际 CLI 结果为 `4352 bytes / 2 records`
（`4096` manifest + `256` 匿名引用；记录为 manifest 1 条 + 匿名引用保守 1 条）。

---

## 测试与验证范围

运行：

```bash
python3 -m unittest discover -s tests -v
```

当前实际结果：**113 个测试全部通过**（每次改动后均重新执行该命令复核）。
所有数据均为本地合成数据（内联 dict 与 `tests/fixtures/`、`examples/`），
CLI 测试通过真实子进程执行 `python3 -m budget_guard` 并断言退出码与 JSON。

覆盖范围：

- **正常场景**：alloc/seq 默认值与求和、op 别名、顶层数组/`root` 名字形态、
  证据字面量/对象形式、文件加载、报告 JSON 可序列化；
- **题面核心陷阱**：
  - 互斥分支逐维度 max 而非相加（含“互斥的展开”“多层分段×递归交错”）；
  - 匿名引用一律保守新分配；具名引用的支配性/分支交集/先后顺序；
- **边界**：空 seq、零次分段、零层递归、`depth=1` 时扇出证据无关、
  未知次数但某维度单位成本为 0（维度独立）、超大证据的闭式精确整数结果、
  负数字面量/布尔值/坏 schema 的 `GraphValidationError`；
- **无界/缺证据**：缺失或非法证据、缺失对象大小、未知 target、
  自环与 `a→b→a` 调用环，均断言 `None` 上界 + 原因码 + `pos` + `branch_id`；
- **拒绝闭环**：拒绝具体节点 / `"<branch-id>#arm:<name>"` 整臂 / 根区域 /
  未知 id 报错，拒绝后 max 与总计正确变化且原因以非活动 Issue 保留；
- **交错综合场景**：共享分配 + 互斥的线性/指数递归臂 + 分段 + 末尾匿名引用，
  测试中给出逐步手工推导值 `107 bytes / 17 records`；
- **门禁与 CLI**：两维度独立超预算、`UNPROVABLE_BOUND`、strict 警告策略、
  allow/deny/输入错误退出码、`--reject` 端到端。

### 已如实记录的限制

- 递归成本采用题面“受限操作图”下的显式 `recurse(depth, fan_out)` 模型；
  对 definitions 中任意复杂的递归调用方程（多调用点在 seq/max 中混合的
  指数多项式）不做符号求解，而是按 `RECURSION_CYCLE` 保守拒绝，要求改用
  `recurse` 表达有界结构。这是有意的防御性选择：不替无证据递归假设终止。
- 证据被当作受信任的上界；模块不验证证据来源/签名（那是调用方与外部证据
  系统的职责），只校验其是否为非负整数上界形态。
- 不建模对象释放/失效、别名相等性之外的运行时条件；条件内容是不透明元数据，
  所有分支默认都可能执行。

---

## 目录结构

```
budget_guard/
  __init__.py        # 公开 API
  model.py           # Cost/Issue/报告/门禁数据模型与格运算
  errors.py          # GraphValidationError
  loader.py          # JSON/dict 加载与证据归一化
  analyzer.py        # 核心静态分析（schema 校验 + 支配性共享 + 成本）
  gate.py            # 预算门禁 evaluate
  cli.py / __main__.py
examples/            # 合成示例图与证据
tests/               # test_*.py（unittest，无第三方依赖）
  fixtures/          # 合成 JSON / 非法 JSON
```
