# scq_compiler — 安全合取查询的视图改写编译器

一个只读副本只暴露若干张已批准的关系**视图**。本编译器判断给定查询能否只用这些
视图回答：若能，输出**引用数最少**（预算默认 ≤ 4 次视图引用）的等价改写；引用数
相同时输出规范化后字典序最小者。另提供候选核验入口：当候选与原查询只满足单向
包含时，生成一个小基表实例，并指出两者在哪个结果元组上分歧。

纯 Python 3.11 标准库实现，无第三方依赖。

## 数据模型

- **项（Term）**：`Var(name)` 变量；`Const(value)` 常量（任意可哈希值）。
- **原子（Atom）**：`Atom(relation, args)`，具名关系 + 项序列；同一关系可在体内
  多次出现（自连接）。
- **查询 / 视图（Query）**：`Query(head, body, name)`。`head` 是输出变量元组，
  **每个输出变量必须在体内出现**（安全性，`check_safe` 强制）；`body` 是原子元组，
  按**集合语义**解释（重复原子、重复结果元组自动去重）。
- **视图定义**：一个 `Query`，其 `head` 给出视图输出列；同一视图可被多次引用。
- **改写（候选）**：`Query`，其体内原子引用视图名而非基表。

文本记法（`parse_query` / `parse_views`）：大写字母或 `_` 开头为变量；`'引号'`、
整数、小写标识符为常量。例：

```
V(A, C) :- R(A, B), R(B, C)          # 视图：2-路径，B 被投影掉（内部列）
Q(X, Z) :- R(X, P), R(P, Q), R(Q, R), R(R, Z)   # 查询：4-路径
Q(X, Z) :- V(X, W), V(W, Z)          # 改写：两次引用 V，经 W 连接
```

## 公开 API

| 函数 | 说明 |
| --- | --- |
| `check_safe(q)` | 安全性校验；输出变量不在体内则抛 `UnsafeQueryError` |
| `parse_query(text)` / `parse_views(text)` | 文本记法 → `Query` / 视图表 |
| `evaluate(q, instance)` | 在基表实例上求值（集合语义），返回结果元组集合 |
| `expand(candidate, views, alpha_rename=True)` | 把视图候选展开为基表上的合取查询 |
| `contains(q1, q2)` / `equivalent(q1, q2)` | 包含 / 等价判定，对**所有有限实例**成立 |
| `compute_mcds(q, views)` | MiniCon 式视图覆盖（MCD）枚举 |
| `find_rewritings(q, views, budget=4)` | 求引用数最少的等价改写；返回 `RewriteResult`（`.best`、`.rewritings`、`.num_refs`），同数按 `canonical_form` 字典序排列 |
| `verify_candidate(q, views, candidate, alpha_rename=True)` | 核验候选；返回 `Verification`（含双向包含标志、`Counterexample` 列表、展开式） |
| `canonical_form(q)` | 规范化串（变量按出现顺序重命名、原子排序），用于字典序比较 |
| `format_query` / `format_instance` / `Counterexample.explain()` | 可读化输出 |

## 正确性依据

- **等价 = 双向包含**。`contains(q1, q2)` 用同态定理（Chandra–Merlin）：把 `q1`
  的体冻结成实例（每个变量变成一个与任何真实常量都不冲突的 `FrozenVar` 单元），
  存在从 `q2` 到该冻结体、且头部对应的同态，当且仅当 `q1 ⊆ q2` 在**所有有限实例**
  上成立。因此等价性不是靠样例归纳，而是可判定的。
- **改写搜索**：对每个查询原子枚举能覆盖它的视图引用（MCD：视图体到查询体的
  同态，含常量匹配），按引用数 1..budget 组合，要求并集覆盖全部查询原子，再用
  上面的等价判定过滤。第一个产生等价改写的大小即为最少引用数。
- **反例生成**：若某方向包含不成立，把较大一侧的体冻结成基表实例，在其上分别
  求值原查询与展开后的候选，差集中的元组即分歧元组（同态定理保证非空）。

## 内部列必须逐次重命名（α-renaming）

`expand` 对每次视图引用把被投影掉的内部变量改成全新名字
（`__<视图>_<引用序号>_<原名>`）。若两次引用的同名内部列被误绑在一起
（诊断模式 `alpha_rename=False` 复现此错误），展开式会多出一个等值约束而
**丢失元组**：此时正向样例可能全部通过——例如实例
`R={(1,2),(2,3),(3,2),(2,4)}` 上，正确展开与错误展开对 4-路径查询的结果完全
一致——但 `verify_candidate` 生成的反例实例（冻结的 4-路径，内部节点互不相同）
能给出被丢掉的元组：该元组在原查询结果中、不在错误候选结果中
（`query_not_subset_of_candidate`）。对应测试见
`tests/test_scq_compiler.py` 的 `TestInternalColumnPitfall`。

## 复现步骤

```bash
cd <工作区根目录>
python3 -m unittest discover -s tests -v   # Python 3.11.6，仅用标准库
```

最小使用示例：

```python
from scq_compiler import parse_query, parse_views, find_rewritings, verify_candidate, format_query

views = parse_views("V(A, C) :- R(A, B), R(B, C)")
q = parse_query("Q(X, Z) :- R(X, P), R(P, Q), R(Q, R), R(R, Z)")

result = find_rewritings(q, views, budget=4)
print(format_query(result.best))                 # Q(X, Z) :- V(Q, Z), V(X, Q)

v = verify_candidate(q, views, result.best)
assert v.equivalent and not v.counterexamples

buggy = verify_candidate(q, views, result.best, alpha_rename=False)
for cex in buggy.counterexamples:
    print(cex.explain())                         # 小实例 + 分歧元组
```

## 验证范围（tests/test_scq_compiler.py，29 个用例，全部真实执行）

- **正常场景**：2-路径视图改写 4-路径 / 8-路径查询（同一视图引用 2 次、4 次）；
  自连接查询（`R(X,Y), R(Y,X)`）；视图与查询中的常量绑定（含错误常量的候选被
  拒绝）；字典序 tie-break；解析/格式化往返。
- **边界场景**：输出变量不在体内（拒绝）；空体（拒绝）；预算不足（budget=1/3
  无解）；无法覆盖的关系（无解）；头部元数不同（不包含）；集合语义去重。
- **交错/共享变量场景**：同一视图两次引用的内部列重命名；误绑内部列时正向样例
  全过、反例入口找出丢失元组；候选过松 / 过严 / 双向不可比三种单向包含情形均
  产生小实例与分歧元组，并断言该元组确实只出现在一侧的结果中。

## 限制

- 仅安全合取查询（无否定、聚合、算术比较、不等式）；头部为变量，常量出现在体内。
- 改写枚举基于 MiniCon 式 MCD 覆盖，预算默认 ≤ 4；视图/查询规模假设较小
  （同态与组合枚举为指数级，本工具面向小型编译场景）。
- 等价与包含结论针对所有有限实例（同态定理），不针对无限实例单独声明。
