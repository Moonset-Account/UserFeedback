# 自适应连接计划器

一个仅使用 Python 标准库的小型精确求解器。它在最多 6 个基表、有限个相关基数场景、可用连接边、整数哈希/归并费用表，以及已有排序/物化块声明下，搜索最坏总费用最小的自适应策略树；最坏费用相同时，再最小化各场景总费用之和。

运行版本要求：**Python 3.11.6**。

## 公开 API

```python
from adaptive_planner import (
    load_problem,
    parse_problem,
    plan_strategy,
    verify_strategy,
    audit_decision_traces,
)

problem = load_problem("examples/probe_example.json")
strategy = plan_strategy(problem)                 # 允许付费物化探测
fixed = plan_strategy(problem, allow_materialization=False)

report = verify_strategy(problem, strategy, check_optimal=True)
assert report.ok, report.errors
```

主要导出：

- `parse_problem(dict) -> Problem` / `load_problem(path) -> Problem`
- `plan_strategy(problem, *, allow_materialization=True) -> dict`
- `verify_strategy(problem, strategy, *, check_optimal=False) -> ValidationReport`
- `audit_decision_traces(traces) -> ValidationReport`（别名 `validate_no_scenario_leak`）

CLI（也可用根目录 `python3 main.py ...` 代替 `python3 -m adaptive_planner.cli ...`）：

```bash
python3 -m adaptive_planner.cli solve examples/probe_example.json \
  -o examples/probe_strategy.json

python3 -m adaptive_planner.cli verify \
  examples/probe_example.json examples/probe_strategy.json \
  --check-optimal

# 不允许物化/观察时，求最佳固定计划
python3 -m adaptive_planner.cli solve examples/probe_example.json \
  --no-materialization -o /tmp/fixed.json

# 只审计决策轨迹中是否提前使用场景身份
python3 -m adaptive_planner.cli audit-traces traces.json
```

## 输入模型

完整小例见 [`examples/probe_example.json`](examples/probe_example.json)。主要字段：

```jsonc
{
  "tables": ["A", "B", "C"],          // 1..6 个基表；也可声明 ordering/memory_limit
  "edges": [
    {"id": "AB", "left": "A", "right": "B",
     "left_keys": ["A.k"], "right_keys": ["B.k"]}
  ],
  "scenarios": [
    {"id": "s1", "rows": {"A": 1, "B": 2, "A+B": 2, "A+B+C": 200}}
  ],
  "cost_model": {
    "join_costs": {
      "hash": {
        "fixed": 0,
        "per_left_row": 1,
        "per_right_row": 1,
        "rules": [
          {"left_rows": 1, "right_rows": 1, "cost": 5, "memory": 2}
        ],
        "default_cost": 99
      },
      "merge": { "...": "同样的整数费用表/仿射费用" }
    },
    "materialization_cost": {"fixed": 0, "per_row": 1, "memory_per_row": 1},
    "sort_cost": {"fixed": 3, "per_row": 2, "memory_per_row": 1}
  },
  "default_memory_limit": 100000,
  "memory_limits": {"A+B": 64},
  "seed_blocks": [
    {"tables": ["B"], "materialized": true,
     "ordering": ["B.k"], "memory_limit": 20}
  ]
}
```

约定：

- 子集用输入表顺序下的 `A+B` 表示；单字母表也接受 `AB`。
- 每个场景的 `rows` 必须提供动作实际使用到的基表/连接子集行数。
- `rules` 是有序整数费用规则，支持 `left_rows/right_rows/output_rows` 精确匹配，或 `min_left/max_left/...` 区间；第一条匹配规则覆盖费用/内存。未命中时使用 `default_cost/default_memory` 或仿射整数公式。
- `seed_blocks` 是查询开始时可重用的互不相交前沿。已物化块的行数视为**初始观察**，策略树根可按这些观察分支。
- 可用顶层 `reuse_existing_order: false` 或 `reuse_materialized_blocks: false` 声明不可沿用已有排序/物化块；单个 table/seed block 也可用同名 `reuse_order`、`reuse_materialization` 字段覆盖。禁用顺序复用后，即使块上有序也必须排序；禁用物化复用后，不把声明的物化状态/初始行数当作可见观察。
- 归并连接要求两侧顺序覆盖边声明键的前缀；已满足时不收复用排序费，否则用 `sort_cost` 计算显式排序费和内存。未声明 `sort_cost` 时，需要额外排序的归并候选不可行。
- 内存要求超过该块/边/子集声明限制的动作，不会被鲁棒策略采用。

## 策略语义与最优性

求解器维护一个**互不相交的当前前沿**：

1. 选择两个由可用边连接的块做哈希/归并连接；两个子块从前沿移除，联合子集加入前沿。
2. 对尚未物化的块选择 `materialize_and_observe(mask)`，支付物化费，并按该子集在各场景中的真实行数分支。
3. 连接动作产生流式输出；哈希输出顺序为空，归并输出沿用边声明的 `output_order`（默认左连接键）。
4. 已移除/完成的子块不能再次被连接；因此不能拆掉已完成子树重算。已有物化块/排序按输入属性直接重用。

“同观察必须同动作”由信念状态 DP 保证：一个节点只对应同一个可能场景集合、同一个前沿和同一累计费用向量。物化后，按观察值分组；同组场景进入同一个子节点并只能选择同一个后续动作。

目标是字典序：

\[
\min_\pi \left(\max_s C_\pi(s),\ \sum_s C_\pi(s)\right).
\]

实现先用无费用上限 DP 找到可行上界，再二分最小可接受最坏费用 \(W\)。对每个候选 \(W\)，递归最小化“各场景未来费用之和”，并要求每个场景的累计费用始终不超过 \(W\)。因此找到最小 \(W\) 后，在该上限下最小化费用和，正好完成题目要求的同价比较。

## 输出：路径与切换位置

`plan_strategy` 返回 JSON 可序列化字典：

- `objective.worst_total_cost`
- `objective.sum_of_scenario_total_costs`
- `objective.scenario_total_costs`
- `root_node` / `nodes`：策略树、信念场景、前沿、唯一动作、观察分支
- `paths.<scenario>`：真实重放出来的逐步动作、费用、累计费用、观察值和下一节点
- `switches.<scenario>`：探测/切换位置

每个 `switches` 项包含 `after_step`、`subset`、`observed_rows`、`selected_next_node`、所有观察分支及其下一动作。`causes_switch=true` 表示不同观察分支的下一动作确实不同。初始已有物化块造成的分支用 `location="initial_materialized_block"`、`after_step=0` 表示。

## 离线防偷看验证

```python
report = verify_strategy(problem, strategy, check_optimal=True)
```

验证器不信任求解器自报内容，而是：

1. 从输入的初始前沿逐场景重放节点树；
2. 用整数费用表独立重算每个动作和总费用；
3. 检查连接边、子集不相交、输出掩码、顺序复用、排序费、内存边界；
4. 检查后续动作引用的块仍在当前前沿，捕获重用已消费/已完成子树的行为；
5. 按“此前观察序列 + 此前动作序列”分组，若相同因果前缀出现不同动作则报告泄漏；
6. 校验 `paths`、累计费用、观察分支和目标值；
7. `check_optimal=True` 时重新运行精确优化器并比较最坏值/费用和。

通用轨迹审计不依赖求解器输出格式：

```python
audit_decision_traces({
    "s1": [{"action": "probe B", "subset": "B", "observed_rows": 2},
           {"action": "AB"}],
    "s2": [{"action": "probe B", "subset": "B", "observed_rows": 2},
           {"action": "AB"}],
})
```

只有前一步的 `observed_rows` 会进入当前决策的因果前缀；第一步就分别选 `AB/BC` 会被判为提前使用真实场景。

## 题目要求的共同探测小例

内置例由 `adaptive_planner.example_data.probe_example()` 给出，也已写入 `examples/probe_example.json`。它是 A-B-C 链，哈希费为每侧每行 1：

| 场景 | A | B | C | 场景内最便宜首连接 |
|---|---:|---:|---:|---|
| `small-A` | 1 | 2 | 100 | `AB` |
| `small-C` | 100 | 20 | 1 | `BC` |

固定计划费用：

- `AB` 优先：`small-A=105`，`small-C=2121`，最坏 2121；
- `BC` 优先：`small-A=303`，`small-C=141`，最坏 303。

多付一次共同的 B 物化探测后，观察到 `B=2` 就走 `AB`，观察到 `B=20` 就走 `BC`。总费用：

- `small-A = 2 (探测) + 3 + 102 = 107`
- `small-C = 20 (探测) + 21 + 120 = 161`

因此最坏费用从最佳固定计划的 **303** 降到 **161**；费用和为 **268**。测试精确断言这些数字，并验证两个观察分支在第 1 步后切换。

## 复现与测试

```bash
python3 --version
# Python 3.11.6

python3 -m unittest discover -s tests -p 'test_*.py' -v
# 或从根目录直接：
python3 -m unittest discover
```

最近一次真实验证结果：

```text
Ran 28 tests in about 2s

OK
```

测试覆盖：

- 正常路径：共同 B 探测、精确费用、路径、切换位置、最优性复核；
- 题目核心：两场景最便宜首连接不同，探测使最坏费用 303 → 161；
- 同最坏值时按费用和选择较低方案；
- 相同观察的第三个交错场景必须进入同一信念节点和同一动作序列；
- 防偷看审计：无观察先分叉、同观察后动作不同均失败；
- 边界：物化/连接/排序内存恰为限制值可行、超 1 单位或 1 行不可行；
- 边界：6 表可解可验证，7 表被拒绝；
- 归并顺序复用、全局/单块禁用顺序复用、缺序排序、无排序费用表时回退哈希；
- 初始物化块的初始观察分支，以及禁用物化块重用后必须重新付费探测；
- 篡改已消费块、目标费用或切换位置会被离线验证器拒绝；
- CLI 的 solve/verify/audit-traces 端到端退出码；
- 当前解释器补丁版本必须为 3.11.6。

## 验证范围与未执行项

已执行的是本仓库内的标准库单元测试、CLI 本地 JSON 往返和离线重放验证。没有也未执行真实数据库、真实网络目标、账户/凭据、外部服务、并发注入或故障恢复测试；题目未要求网络或并发协议。这里的“不能拆掉重算”由前沿不变量和离线重放检查保证，而不是通过真实数据库崩溃恢复验证。
