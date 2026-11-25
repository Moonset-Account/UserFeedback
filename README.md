# 滚动重排规划器（Rolling Re-layout Planner）

为容器调度器实现的滚动重排规划：给定节点标签与剩余槽位、当前副本分布、每个副本
各自的节点选择条件，以及机架（rack）和地域（zone）两套分散规则，在不低于最少就绪
数、不超过临时增量上限的前提下，用 BFS 在 **8 个节点以内** 求出达到目标均衡布局的
**最少动作序列**，并给出每一步两套规则的合格域与同服务副本统计值。

仅使用 Python 3.11 标准库，无第三方依赖。

## 语义约定

### 合格域（eligible domain）

对某条规则 `topology_key` 和某个副本的节点选择条件：

- 一个域是**合格域**，当且仅当域内至少存在一个节点同时满足：携带该拓扑键标签、
  且通过该副本的节点选择条件。
- 被选择器排除的机架（如 `NotIn`）不会进入合格域集合，**不会被当作零副本机架
  计入**——这是核心约束，有专门测试锁定。
- 没有拓扑键标签的节点不属于任何域，不参与统计。

### 同服务副本统计与有效全局最小值

- 每个合格域统计当前该服务所有运行副本在该域的数量（不含不合格域）。
- 当合格域数量 `< min_domains` 门槛时，有效全局最小值按 **0** 计；否则取各合格域
  副本数的真实最小值。

### 动作与门控

每一步只做一个原子动作：

- `start`：在有空槽、且满足该副本节点选择条件的节点上启动一个副本。必须同时通过
  两套规则的偏差检查：`启动后目标域副本数 - 启动前有效全局最小值 <= max_skew`。
  运行实例总数不得超过 `副本总数 + max_surge`。
- `stop`：停掉一个运行中的副本，停止后运行实例数不得低于 `min_ready`。停止动作
  不做偏差检查（旧布局允许违反新规则）。
- 允许同一副本在 surge 期间临时双跑；允许不同副本共享节点，只要该节点有空槽。

### 目标均衡布局

- 每个副本恰好运行在一个节点上；
- 该节点满足该副本自己的节点选择条件（不合规的旧布局只允许作为起点/中间态）；
- 两套规则对每个副本观测到的最大偏差均 `<= max_skew`。

BFS 以动作为边，因此首次到达目标状态的路径即动作数最少的规划；不可行时返回
`RolloutPlan(feasible=False)`。节点数超过 8 时直接抛 `ValueError`。

## 公开 API

`rolling_relayout` 包导出：

| 名称 | 说明 |
| --- | --- |
| `Node(node_id, labels, free_slots)` | 节点：ID、标签字典、初始空槽数 |
| `NodeSelector(required, excluded)` | 节点选择条件；`required` 为等值必须匹配，`excluded` 为 NotIn 语义（标签缺失不算排除） |
| `Replica(replica_id, selector, current_node)` | 副本及其选择条件与当前所在节点（可为 `None` 表示已停止） |
| `SpreadRule(name, topology_key, max_skew, min_domains)` | 一条分散规则及合格域数门槛 |
| `PlannerInput(nodes, replicas, rules, min_ready, max_surge)` | 完整输入，构造时做一致性校验 |
| `Action(kind, replica_id, node_id)` | 单步动作，`kind` 为 `"start"`/`"stop"` |
| `DomainStat` | 某规则下某副本的合格域、每域副本数、有效最小值、观测偏差等 |
| `GateEvaluation` | 一次启动动作在某规则下的门控结果（含启动后计数与原因） |
| `RolloutStep` / `RolloutPlan` | 逐步证据与完整规划 |
| `evaluate_domains(nodes, placement, replica, rule)` | 纯函数：计算合格域与统计值 |
| `evaluate_start(nodes, placement, replica, target_node, rule)` | 纯函数：模拟一次启动的偏差检查 |
| `find_balance_violations(nodes, placement, replicas, rules)` | 列出所有违反规则的（副本, 规则, 统计）三元组 |
| `Planner(planner_input).plan()` / `find_plan(...)` | 执行 BFS，返回 `RolloutPlan` |

`placement` 的类型为 `dict[replica_id, frozenset[node_id]]`，允许一个副本同时跑在
多个节点上（surge 双跑）。

### 最小示例

```python
from rolling_relayout import (
    Node, NodeSelector, Replica, SpreadRule, find_plan,
)

nodes = (
    Node("n1", {"rack": "rk-a"}, free_slots=0),
    Node("n2", {"rack": "rk-a"}, free_slots=0),
    Node("n3", {"rack": "rk-b"}, free_slots=2),
)
replicas = (
    Replica("r1", NodeSelector(), current_node="n1"),
    Replica("r2", NodeSelector(), current_node="n2"),
)
rule = SpreadRule("rack-spread", "rack", max_skew=1)
plan = find_plan(nodes, replicas, [rule], min_ready=2, max_surge=1)
print(plan.feasible, plan.length, plan.final_placement)
for step in plan.steps:
    print(step.action, step.domain_stats_before, step.gate_results)
```

可运行的完整示例（含机架+地域双规则、每步证据打印）：

```bash
python3 -m examples.smoke_rollout
```

## 运行方法

```bash
# 在仓库根目录执行
python3 -m unittest discover -s tests -v
```

## 验证范围

全部为本地合成数据，无网络访问、无第三方依赖。共 31 个测试，覆盖：

- `tests/test_domains.py`（9 个）
  - 合格域包含零副本域、被排除机架不出现在合格域/计数中；
  - `min_domains` 不足时有效最小值按零、达标时用真实最小值；
  - 缺少拓扑标签的节点被忽略；
  - 启动门控使用“启动后目标域计数 − 启动前有效最小值”，超限拒绝；
  - 选择器不匹配的节点直接拒绝；已有布局可违反新规则的偏差检测。
- `tests/test_planner.py`（14 个）
  - 最少动作数（(2,2,0,0) → (1,1,1,1) 必须 4 步）、启动/停止动作计数；
  - `min_ready` 满额时只能先启动、无 surge 且满就绪时不可行、低就绪时可先停；
  - 运行数始终落在 `[min_ready, desired + max_surge]`；
  - 选择器排除机架的副本永远不会启动到该机架；
  - 8 节点机架+地域双规则交错，每次启动同时过两套门控；
  - 空槽核算（无空槽节点不能启动）、>8 节点拒绝、非法输入校验；
  - 独立重放器从零模拟全部动作，复核槽位、选择器、门控与最终均衡。
- `tests/test_boundary.py`（6 个）
  - 每副本独立选择器、`min_domains` 门槛在规划层面导致可行/不可行的对照；
  - 初始已停止副本的启动、重复运行结果确定性、同节点共置与槽位预算。
- `tests/test_crosscheck.py`（2 个）
  - 用独立实现的 BFS 暴力枚举验证规划器给出的深度严格最短；
  - 8 节点最坏规模下两套规则同时违规时的可行性与运行时间（< 5 秒）。

## 目录结构

```
rolling_relayout/
  __init__.py     # 公开 API 再导出
  model.py        # 不可变数据模型与输入校验
  domains.py      # 合格域统计、启动门控、均衡违规检测（纯函数）
  planner.py      # BFS 最少动作搜索与逐步证据重放
tests/            # 4 个测试模块，31 个用例
examples/         # 可运行示例
```
