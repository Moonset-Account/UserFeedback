# 分层 Datalog 查询引擎

在有限关系（元组集合）上执行声明式规则的查询引擎。规则体可以连接多个
关系，也可以用否定文字排除某个**键已经被绑定**的匹配。引擎完成：

1. **分层（stratification）**：基于谓词依赖图的强连通分量（Tarjan SCC）
   与缩点 DAG 最长链编号；纯正向递归可成环并同层求值，含否定依赖的环
   无法分层，会连同环上谓词与规则位置一起报错；
2. **逐层求不动点**：从第 0 层开始，每层对该层规则做朴素（naive）集合
   不动点求值。所有关系都是有限集合，重复推导同一行被去重，一轮中没有
   新行即到达不动点，计算必然终止；
3. **结果解释（provenance）**：为每个输出行保留一条有限的正向推导树，
   基础事实是叶子，规则节点的子节点按规则体排列，否定文字记录为
   `NegationCheck`，并标出被否定关系在检查时**已经封闭的层**；
4. **事实更正与重算**：支持加入、撤去、整体替换基础事实后重新求值。
   新事实可沿正递归向上游扩张结果，同时使下游因“缺席”才成立的否定
   结论被撤回。

仅使用 Python 3.11.6 标准库，无第三方依赖，不联网、不读取凭据。

## 目录结构

```
workspace/
├── datalog/
│   ├── __init__.py        # 公开 API 导出
│   └── engine.py          # 规则模型、分层、不动点、解释、更正
├── tests/
│   ├── test_basic_evaluation.py       # 连接、常量选择、去重、安全检查
│   ├── test_recursion_fixpoint.py     # 正递归成环、不动点终止、确定性
│   ├── test_stratification_negation.py# 合法分层、否定即失败、否定环报错
│   ├── test_explanation.py            # 推导树、否定检查封闭层标注
│   ├── test_fact_correction.py        # 更正重算：扩张与撤回交错
│   └── test_large_finite_domain.py    # 较大有限域终止性/幂等
├── demo.py                # 端到端演示，可直接运行
└── README.md
```

## 公开 API

### 规则与事实模型（`datalog` 包顶层导出）

| 名称 | 说明 |
| --- | --- |
| `Var(name)` | 逻辑变量；规则项中 `Var` 实例是变量，其余可哈希值是常量 |
| `Atom(predicate, *terms)` | 原子，如 `Atom("edge", X, Y)` |
| `Literal(atom, negated=False)` | 规则体文字；`NOT(atom)` 是其取反简写 |
| `NOT(atom)` | 构造被否定的文字 |
| `Rule(head, body, location=None)` | 规则 `head :- body...`，`location` 标注规则位置 |
| `ProofNode` | 推导树节点（`predicate`、`tuple_value`、`stratum`、`rule`、`children`） |
| `NegationCheck` | 否定检查（`predicate`、`tuple_value`、`stratum`、`absent`） |
| `DatalogError` | 引擎错误基类 |
| `SafetyError` | 违反范围限制（安全条件）或谓词元数不一致 |
| `StratificationError` | 含否定依赖的环；含 `cycle`、`rule_locations`、`negative_edges` |

### `QueryEngine` 方法

| 方法 | 说明 |
| --- | --- |
| `add_rule(rule, location=None)` | 加入规则；缺省位置自动用 `rule#<序号>` |
| `add_fact(predicate, tuple_value)` | 加入/补入基础事实，重复自动去重 |
| `retract_fact(predicate, tuple_value) -> bool` | 撤去基础事实，返回原先是否存在 |
| `replace_facts(predicate, tuples)` | 整体更正某个基础关系 |
| `evaluate() -> QueryEngine` | 静态检查 → 分层 → 逐层不动点求值，返回自身 |
| `rows(predicate) -> list[tuple]` | 输出关系的全部行（按稳定顺序） |
| `contains(predicate, tuple_value) -> bool` | 行是否在结果中 |
| `explain(predicate, tuple_value) -> ProofNode` | 展开一条有限正向推导树；无此行抛 `KeyError` |
| `format_proof(proof) -> str` | 把推导树渲染成缩进文本 |
| `strata() -> dict` | 分层报告：每层的谓词与规则位置 |
| `predicate_stratum(predicate) -> int` | 查询谓词所在层 |

## 运行方法

运行全部测试（题目要求的命令）：

```bash
python3 -m unittest discover -s tests -v
```

运行端到端演示：

```bash
python3 demo.py
```

## 最小示例

```python
from datalog import Atom, Literal, NOT, QueryEngine, Rule, Var

X, Y, Z = Var("X"), Var("Y"), Var("Z")

engine = QueryEngine()
for n in ("a", "b", "c", "d"):
    engine.add_fact("node", (n,))
engine.add_fact("edge", ("a", "b"))
engine.add_fact("edge", ("b", "c"))

engine.add_rule(Rule(Atom("reach", X, X), [Literal(Atom("node", X))]),
                location="reach.refl")
engine.add_rule(Rule(Atom("reach", X, Y), [Literal(Atom("edge", X, Y))]),
                location="reach.base")
engine.add_rule(
    Rule(Atom("reach", X, Y),
         [Literal(Atom("reach", X, Z)), Literal(Atom("edge", Z, Y))]),
    location="reach.rec")
engine.add_rule(
    Rule(Atom("unreachable", Y),
         [Literal(Atom("node", Y)), NOT(Atom("reach", "a", Y))]),
    location="unreach.r1")

engine.evaluate()
print(engine.rows("unreachable"))          # [('d',)]
print(engine.format_proof(engine.explain("unreachable", ("d",))))

engine.add_fact("edge", ("c", "d"))        # 更正：新事实
engine.evaluate()                          # 重算
print(engine.rows("unreachable"))          # []  —— 缺席结论被撤回
print(engine.contains("reach", ("a", "d")))  # True —— 正递归扩张
```

## 安全条件（防御性判断，仅实现题面规定）

- 否定文字中的每个变量必须已被同一规则体的**正向**文字绑定（即只能
  排除“已经绑定键”的匹配）；需要“某键不存在任意匹配”的语义时，先用
  一条正向辅助规则把键投影成 IDB 关系，再对该关系取反（见测试与 demo）；
- 规则头中的每个变量必须出现在某个正向文字中；
- 同一谓词在所有事实与规则中必须元数一致；
- 违反以上条件在求值前抛出 `SafetyError`，不执行任何推导。

本项目不生成、不执行任何利用/入侵/攻击/规避载荷，仅做上述声明式规则的
防御性合法性判断与集合求值。

## 否定环报错结构

`StratificationError` 提供：

- `cycle`：首尾谓词相同的环序列，如 `["p", "p"]` 或 `["p", "q", "p"]`；
- `rule_locations`：环上每条边对应的规则位置（与 `cycle` 的边一一对应）；
- `negative_edges`：环上的否定依赖 `(源谓词, 被否定谓词)`。

## 验证范围（本地合成数据，均已实际执行）

测试命令实测 **36 个用例全部通过**（`Ran 36 tests ... OK`），覆盖：

- **正常场景**：多关系连接、常量选择、重复变量等值约束、整数/字符串
  混合常量、空关系、事实与推导双重去重；
- **正递归与不动点**：含环图的传递闭包、互递归、同层 SCC 不产生新分层、
  有限集合上去重保证终止、重复求值确定且幂等；
- **分层否定**：否定即失败、正向连接与否定交错、多层否定使层号严格
  抬高、被否定关系位于严格更低且已封闭的层；
- **否定成环**：自否定 `p :- NOT p` 与跨谓词否定环均被拒绝，且返回
  环谓词、否定边与规则位置；
- **解释**：基础事实叶子、递归推导树有限且叶子全部落到事实、否定检查
  变量已全部绑定为常量并标注封闭层、每个输出行都有可展开解释、未知行
  解释抛 `KeyError`；
- **事实更正交错**：加入 `edge(c,d)` 后 `reach` 沿正递归新增 `(a,d)`、
  `(b,d)` 等上游结果，同时 `unreachable(d)` 因不再缺席而被撤回且无解释；
  撤去事实后否定结论恢复；整体替换与重复事实幂等；重算后分层保持不变；
- **较大有限域**：60 节点链的完整闭包（1830 行）与 25 节点完全图
  （625 个有序对，海量重复推导路径）均在有限时间内终止且结果确定。

### 已知边界与设计取舍

- 采用朴素（naive）而非半朴素（semi-naive）求值，重算为全量重算；
  强调语义清晰与终止保证，未针对大规模数据优化性能。
- 规则体不能为空（不接受无条件推导规则）；不支持函数符号、聚合与
  算术表达式，常量限为可哈希值。
- 否定采用严格范围限制（否定变量须先被正向文字绑定），这与题面
  “排除某个已经绑定键的匹配”一致。
