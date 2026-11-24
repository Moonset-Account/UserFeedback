# 合成巡检探针布置组件

一个纯 Python 3.11 标准库实现的可靠性巡检布置组件。它根据候选巡检站点、探针入口/依赖路径、请求成本、预算、指定入口和单故障清单，选择满足入口覆盖与预算约束、且能区分最多单故障场景对的探针集合，并返回选择后仍会得到相同观测结果的故障对。

不访问网络、不连接真实站点、不需要安装第三方依赖。

## 1. 核心判定语义

对每个“单故障场景 × 候选探针”，观测值只有三种：

- `probe_planner.MISSING = "missing"`：巡检站点不可用，该探针没有回执；调用方知道这次请求缺测。
- `probe_planner.FAILED = "failed"`：站点正常回执，但探针目标入口或路径上的某个依赖故障。
- `probe_planner.OK = "ok"`：站点正常且路径/目标没有命中故障资源。

判定顺序固定为：

1. 如果 `probe.site in fault.unavailable_sites`，结果是 `MISSING`。因此同一站点的任意两条探针在该站点失联时都会同时是 `MISSING`。
2. 否则，如果探针依赖集合或目标入口命中 `fault.failed_resources`，结果是 `FAILED`。
3. 否则为 `OK`。

目标 `entry` 会自动视为可故障资源，因此“业务/入口坏掉”可以建模为入口资源故障。

两个单故障场景可区分，当且仅当它们在所选探针上的完整状态向量不同。组件直接构造完整签名，所以同一站点多条探针同时缺测的相关性会被自然计入，而不会把两条缺测误当成相互独立的信息。

## 2. 公开 API

### 2.1 数据对象

```python
from probe_planner import Probe, Fault, MISSING, FAILED, OK

probe = Probe(
    site="beijing",
    entry="checkout",
    dependencies=("edge", "auth", "database"),
    cost=2,
    id="bj-checkout",       # 可省略；规范化时会得到 P1、P2、...
)

site_fault = Fault.site_outage("beijing", id="site:beijing")
dep_fault = Fault.dependency_failure("auth", id="fault-auth")
entry_fault = Fault.entry_failure("checkout", id="fault-checkout")
```

也支持直接传普通字典、元组和易读字符串：

```python
probes = [
    {
        "id": "bj-checkout",
        "site": "bj",
        "entry": "checkout",
        "dependencies": ["edge", "auth"],
        "cost": 2,
    },
    ("sh", "search", ["catalog"], 1),
]

faults = [
    {"id": "f1", "kind": "site", "site": "bj"},
    {"id": "f2", "kind": "dependency", "dependency": "auth"},
    "site:sh",       # 前缀 site:/station: 表示站点失联
    "dep:catalog",   # dep:/dependency:/resource:/entry: 表示资源故障
    "bj",            # 若 bj 是候选探针站点，裸 ID 会推断为站点故障
]
```

### 2.2 主入口

```python
from probe_planner import plan_probes

result = plan_probes(
    probes,
    faults,
    budget=5,                         # None 表示不限预算
    required_entries=["checkout"],    # None 表示覆盖 probes 中出现的全部入口
    request_cost=1,                   # 未显式填写 cost 时的默认成本或 (site, entry) 回调
    cost_for=None,                    # 可选：按 id/(site, entry)/"site->entry" 等键提供成本
)

result.selected_probes       # 选中的 Probe 对象，按候选顺序返回
result.selected_ids          # 选中探针 ID
result.total_cost            # 实际总成本
result.ambiguous_pairs       # 仍相同结果的无序故障对，元素为 (fault_id_a, fault_id_b)
result.same_result_pairs     # ambiguous_pairs 的别名
result.distinguished_pairs   # 已区分的故障对
result.signatures            # {fault_id: (所选探针状态, ...)}
result.equivalence_classes   # 仍有相同签名的故障等价组
result.covered_entries       # 所选探针实际覆盖的入口
result.required_entries      # 约束要求覆盖的入口
result.pair_count
result.distinguished_pair_count
result.ambiguous_pair_count
result.to_dict()             # 普通 list/dict 数据，可 json.dumps
```

约束与目标：

- 每个 `required_entries` 中的入口至少被一条所选探针覆盖。
- `sum(probe.cost) <= budget`。
- 在所有可行子集中最大化已区分的无序单故障场景对数量。
- 若多个方案区分对数相同，采用确定性 tie-break：总成本更低 → 探针数更少 → 候选 ID/原始顺序的字典序更靠前。
- 如果指定入口没有候选探针，或最小覆盖成本超过预算，抛出 `probe_planner.InfeasibleCoverageError`；异常带 `missing_entries`、`minimum_cost`、`budget` 等信息。

### 2.3 站点/入口/路径风格的一体化 API

```python
from probe_planner import plan_placement, ProbePlacementPlanner

result = plan_placement(
    sites=["bj", "sh"],
    entries=["checkout", "search"],
    paths={
        "bj": {"checkout": ["edge", "auth"]},
        "sh": {"checkout": ["edge", "auth"]},
        ("bj", "search"): ["catalog"],
    },
    faults=["site:bj", "dep:auth", "dep:catalog"],
    budget=4,
    request_cost=1,
)

planner = ProbePlacementPlanner(
    probes=[
        {"id": "p1", "site": "bj", "entry": "checkout", "dependencies": ["auth"]},
        {"id": "p2", "site": "sh", "entry": "search", "dependencies": ["catalog"]},
    ],
    faults=["site:bj", "dep:auth", "dep:catalog"],
    required_entries=["checkout", "search"],
    budget=3,
)
result = planner.plan()
report = planner.evaluate(result.selected_ids)
```

`paths` 支持：

- `{site: {entry: [dependencies]}}` 嵌套字典；
- `{(site, entry): [dependencies]}`；
- `"site->entry"`、`"site:entry"`、`"site/entry"` 字符串键；
- `lambda site, entry: [...]` 回调；返回 `None`/`False` 表示该站点不能探测该入口。

### 2.4 手工评估一组探针

```python
from probe_planner import evaluate_selection

probes = [
    {"id": "p1", "site": "bj", "entry": "checkout", "dependencies": ["auth"]},
    {"id": "p2", "site": "sh", "entry": "search", "dependencies": ["catalog"]},
]
faults = ["site:bj", "dep:auth", "dep:catalog"]

report = evaluate_selection(probes, faults, ["p1", "p2"])
report["signatures"]
report["ambiguous_pairs"]
report["equivalence_classes"]
```

### 2.5 JSON 字典入口

```python
from probe_planner import solve

payload = {
    "sites": ["bj", "sh"],
    "entries": ["checkout"],
    "paths": {"bj": {"checkout": ["auth"]}, "sh": {"checkout": ["auth"]}},
    "faults": ["site:bj", "dep:auth"],
    "budget": 2,
    "request_cost": 1,
}
plain_result = solve(payload)
```

仓库根目录还提供 `solution.py` 和 `probe_placement.py` 兼容导出名，例如 `from solution import solve, plan_placement`。

## 3. 实现与可区分性判断

每个探针先预算一个整数位掩码 `distinction_mask`：第 `k` 位表示它是否能区分第 `k` 个无序故障对。选择一组探针后，对这些掩码做 OR：

- 某位为 `1`：至少一条所选探针在该故障对上的状态不同，故障对已区分。
- 某位为 `0`：所有所选探针在两个场景下的状态都相同，该对进入 `ambiguous_pairs`。

站点失联不是“把某条探针改成失败”，而是对该站点所有探针都写入 `MISSING`。例如同一站点的两条探针在站点故障下签名为 `(MISSING, MISSING)`；若路径依赖故障且站点能回执，则可能是 `(FAILED, FAILED)`。二者不同，因此仍可区分。

优化器是精确的分支限界子集搜索，包含：

- 后缀动态规划计算最小入口覆盖成本；
- 所有剩余探针的 OR 上界；
- 预算感知的分数背包松弛上界；
- 无新增区分位时直接取最便宜覆盖尾部；
- 确定性 tie-break。

该带预算覆盖的最大区分问题本质上是 NP-hard 最大覆盖/背包组合，最坏情况为指数复杂度；本实现面向题面所述少量巡检站点和关键入口的合成巡检布局。代码没有用启发式结果冒充最优解。

## 4. 运行测试

环境：

```bash
python3 --version
# Python 3.11.6
```

在题目工作区根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

本次实现完成后的实测结果为：

```text
Ran 18 tests in ...

OK
```

其中 `tests/test_exhaustive_random.py` 虽然是一个测试方法，但内部包含 250 个固定随机种子的子测试；每个小规模布局都会枚举全部 `2^n` 个子集作为参照。

## 5. 已验证范围

单元测试和本地合成数据覆盖：

- 正常的多站点、多入口、多依赖路径选择。
- 预算足够/紧张时的最大故障对区分。
- 预算约束下必须先满足指定入口覆盖；高区分度但不覆盖指定入口的探针不会抢占可行方案。
- 预算不足、指定入口无候选探针等不可行情况及异常字段。
- 空探针、空故障清单、零故障对、单故障、零预算与零成本探针。
- 字符串作为单个入口、故障或所选 ID 时不会被拆成字符。
- `Decimal` 成本保持精确，并可经 `to_dict()`/`solve()` 输出为 JSON 数字。
- 两个故障对所有候选探针效果相同而保持歧义，并验证等价分组。
- 同一站点两条探针在站点失联时同时 `MISSING`。
- 站点失联 `MISSING` 与共享路径依赖故障 `FAILED` 不能被错误合并。
- 缺失回执优先于同场景中的路径效果。
- 目标入口本身作为业务/入口资源故障。
- 重叠路径、共享依赖、共享站点等交错情况。
- 嵌套 `paths`、元组输入、字典 `solve()`、状态类 API、JSON 可序列化输出。
- 250 个确定性随机合成布局中，优化器的所选 ID、成本、区分对数、歧义对数均与暴力枚举一致；并独立根据签名复核每个故障对是否相等。

未覆盖/未声明的范围：

- 未连接真实巡检站点、真实网络或真实凭据；所有验证均基于本地合成数据。
- 未进行大规模性能或并发 SLA 测试；精确搜索的最坏复杂度为指数级。
- 当前模型处理题面要求的单故障场景，不枚举多故障组合。
