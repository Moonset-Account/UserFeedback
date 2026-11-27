# authflow — 授权绑定与持久化写入的静态分析

核查生成服务代码里**授权检查与实际持久化操作是否绑定在同一个对象版本上**。
分析输入是一张由分支、别名、服务调用及可选事务提交节点构成的控制流图
（CFG），每个调用的摘要注明会替换哪些对象或推进哪些版本；输出定位到
**无法证明操作受授权约束的写入位置**，并给出断开的依赖链，同时区分
对象副本与不可达调用。

## 分析语义

1. **授权事实是三元组** `(对象 object_id, 版本 version, 操作 operation)`，
   由成功的 `AuthCheckNode` 产生，并记录产生它的检查节点 id（事实来源）。
2. **沿可执行路径前向传播**；多前驱合流取交集——must 事实必须在所有到达
   路径上成立，只在部分路径成立的事实在合流点丢失并记录为 `MergeGap`。
3. **调用摘要驱动失效**：
   - `replaces`（整对象替换）：该对象所有版本上的事实立即失效；
   - `advances`（版本推进）：该对象旧版本上的事实失效，新版本不自动继承；
   - `rebindings` / `unbinds`：变量句柄被重绑定到外部来源或解绑，
     未经授权的新来源不携带事实。
4. **别名与副本区分**：别名共享同一对象引用与版本，事实互通；对象副本
   获得新对象标识，**不继承**原对象的授权事实。
5. **事务语义**：`TxBegin` 冻结绑定/版本/事实快照；`TxCommit` 保留事务内
   事实；`TxRollback` 恢复快照，事务内产生或扩展的事实按回滚语义失效，
   支持事务嵌套。
6. **可达性区分**：从入口不可达的调用节点不参与数据流，单独列入
   `unreachable_calls`，不产生 `Finding`；可达但无法证明授权的写入才是
   `Finding`。

## 包结构

```
authflow/
  __init__.py    # 公开 API 再导出
  model.py       # IR 节点、Program、ProgramBuilder、结构校验
  analysis.py    # 数据流环境、转移函数、合流、事务、写入判定与断链
  errors.py      # ProgramStructureError
tests/
  test_core_flow.py      # 授权命中/版本陈旧/对象替换/操作不匹配/未绑定
  test_alias_branch.py   # 别名/副本/分支合流/交错路径
  test_transactions.py   # 提交/回滚/嵌套事务
  test_structure.py      # 结构校验/可达性/显式版本断言
  test_edges.py          # 断链角色/多事实/确定性/环境快照
```

## 公开 API

### 构建程序（`authflow.model.ProgramBuilder`，流式 API）

| 方法 | 作用 |
| --- | --- |
| `entry(node_id, initial_bindings=None)` | 入口；初始绑定 `变量 -> (oid, version)` |
| `branch(node_id, condition="")` | 条件分支，必须配 `branch_edges` 的 true/false 出边 |
| `merge(node_id, label="")` | 显式合流点 |
| `auth_check(node_id, object_id, operation, version=-1, target=None)` | 成功授权检查；`version=-1` 延迟绑定到 `target` 当前版本 |
| `alias(node_id, lhs, rhs, is_copy=False, new_object_id=None, new_version=None)` | 别名或对象副本，可同时推进版本 |
| `call(node_id, target=None, operation=None, is_write=False, replaces=(), advances=None, rebindings=None, unbinds=())` | 服务调用摘要；`is_write=True` 标记持久化写入位置 |
| `tx_begin / tx_commit / tx_rollback(node_id)` | 事务边界 |
| `connect(src, dst, label="flow")` / `branch_edges(branch_id, true_id, false_id)` | 加边 |
| `build()` | 校验并返回 `Program`（要求 DAG） |

### 执行分析

```python
from authflow import ProgramBuilder, analyze

program = (
    ProgramBuilder()
    .entry("entry", {"order": ("oid-1", 1)})
    .auth_check("check", object_id="oid-1", operation="write", target="order")
    .call("advance", target="order",
          advances={"oid-1": 2},
          rebindings={"order": ("oid-1", 2)})
    .call("write", target="order", operation="write", is_write=True)
    .connect("entry", "check")
    .connect("check", "advance")
    .connect("advance", "write")
    .build()
)
result = analyze(program)
```

### 结果对象（`authflow.analysis.AnalysisResult`）

- `findings: list[Finding]` — 无法证明授权的写入。`Finding` 含
  `node_id / target / object_id / version / operation / reason / detail /
  chain / matched_fact / fact_origins`；`chain` 是 `ChainLink` 序列，
  `is_break=True` 的环节即依赖链断开点。
- `constrained_writes: list[str]` — 已证明受授权约束的写入节点。
- `reachable_calls` / `unreachable_calls: list[str]` — 可达与不可达调用
  的严格区分。
- `evaluations: dict[str, WriteEvaluation]` — 每个可达写入的原始判定。
- `invalidated_facts: list[InvalidatedFact]` — 事实失效证据（对象替换、
  版本推进、事务回滚），含来源检查节点。
- `merge_gaps: list[MergeGap]` — 合流点上绑定/版本/事实的路径差异。
- `input_envs / output_envs: dict[str, Env]` — 每个可达节点的数据流环境
  快照，便于外部复核。
- `facts_at_exit()` — 叶子节点输出环境中仍存活的授权事实。

### 判定原因常量（`authflow.analysis`）

| 常量 | 含义 |
| --- | --- |
| `authorized` | 存在同对象、同版本、同操作的事实 |
| `unbound` | 写入目标无有效对象绑定 |
| `divergent_variable` | 合流后变量绑定到不同对象，状态发散 |
| `divergent_object` | 同一对象在路径间版本不一致 |
| `object_copy` | 写入目标是对象副本，副本不继承授权 |
| `overwritten_binding` | 句柄被未经授权的外部来源重绑定，且无同对象事实 |
| `stale_version` | 授权绑定在旧版本，对象已被推进/替换 |
| `operation_mismatch` | 同对象同版本只有其他操作的授权 |
| `no_authorization` | 路径上不存在任何可用的同对象授权事实 |
| `tx_rollback` | 授权事实随事务回滚失效 |

## 运行方法

无需安装依赖，仅使用 Python 3.11 标准库：

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围

本地合成数据共 **52 个 unittest 用例**，覆盖题面要求的正常、边界与
交错场景：

- **正常路径**：授权检查后写入被证明受约束；多跳别名共享事实；
  事务提交保留事实；事务前事实在回滚后仍存活。
- **版本/对象失效**：版本推进、整对象替换后旧事实失效并产生
  `InvalidatedFact`；推进到相同版本不误伤；无关对象推进不影响事实；
  推进后重新授权可恢复约束。
- **合流（交错）**：事实只在单分支成立时合流丢失；两分支都成立时保留；
  分支间绑定对象发散、版本发散分别归类并记录 `MergeGap`；合流后重绑定
  再授权的交错路径。
- **别名与副本**：别名共享、副本隔离（原对象事实仍存活，副本写入报
  `object_copy`）、别名推进版本后旧事实不覆盖新版本。
- **事务**：提交、回滚恢复快照、回滚使事务内事实失效、内层回滚保留
  外层状态、外层回滚丢弃内层已提交事实、未匹配回滚报错。
- **可达性**：孤岛调用进入 `unreachable_calls` 且不产生 Finding；
  不可达区域完全排除在数据流之外。
- **结构校验**：重复节点、缺失入口、悬空边、分支缺 true/false 边、
  CFG 环、授权检查目标未绑定/对象不符/显式版本不符等均抛
  `ProgramStructureError`。
- **断链与确定性**：断链从授权检查来源起、经过具体失效节点、终止于
  写入位置并标注断点角色；重复分析结果一致；节点输入/输出环境可复核。

## 已知限制

- CFG 必须是 **DAG**：循环需要额外的 widening，当前在 `build()` 阶段
  以 `ProgramStructureError` 明确拒绝。
- 授权事实是 must 风格的流敏感分析，不做指针/别名的全程序指向分析，
  对象身份完全由 IR 摘要显式给出。
- 分析只判断"能否证明授权约束存在"，不评估授权策略本身的正确性，
  也不生成或执行任何利用载荷。
