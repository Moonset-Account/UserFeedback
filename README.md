# 关系查询计划谓词改写模块（predrewrite）

一个仅依赖 Python 标准库的查询计划谓词改写引擎。输入是带**左右表别名**的内连接
（inner join）与左外连接（left outer join）树，过滤表达式只包含等值/大小比较、
`IS NULL` / `IS NOT NULL` 以及 `AND` / `OR` / `NOT` 逻辑组合；输出是改写后的新计划，
以及**每条原始谓词的完整去向追踪**和**外连接转内连接的等价性依据**。

实现严格区分连接的 `ON` 匹配条件与连接后的 `WHERE` 过滤条件，并按 SQL
**三值逻辑（3VL：TRUE / FALSE / UNKNOWN）**与**袋语义（保留重复行、不去重）**
执行和验证。

## 1. 目录结构

```
workspace/
├── predrewrite/
│   ├── __init__.py        # 公开 API 汇总
│   ├── expressions.py     # 表达式树、3VL 求值、符号化“空值拒绝”证明
│   ├── plans.py           # 计划节点、袋语义执行器、计划文本渲染
│   └── rewrite.py         # 谓词下推 / 外连接转内连接引擎与去向追踪
├── tests/
│   ├── test_expressions.py            # 3VL 真值表与符号证明单测
│   ├── test_plans.py                  # 补齐空行/重复行/UNKNOWN 执行语义
│   ├── test_rewrite_structure.py      # 改写结构、去向追踪、转换事件
│   ├── test_equivalence_exhaustive.py # 小表穷举等价（含重复行）
│   ├── test_fuzz_equivalence.py       # 固定随机种子的交错/深嵌套模糊等价
│   └── test_edge_cases.py             # 边界与防御分支
├── run_tests.py           # 兼容 `python -I` 的测试入口
├── demo.py                # 端到端演示（运行时真实求值，无预置输出）
└── README.md
```

## 2. 公开 API

### 2.1 表达式（`predrewrite.expressions`）

| 构造 | 含义 |
| --- | --- |
| `Column(alias, name)` | 限定列引用 `alias.name` |
| `Literal(value)` | 常量；`value=None` 即 SQL NULL |
| `Compare(left, op, right)` | 比较，`op ∈ {"=", "<>", "<", "<=", ">", ">="}`，操作数可为列或常量 |
| `IsNull(arg, negated=False)` | `arg IS NULL`；`negated=True` 即 `IS NOT NULL` |
| `Not(arg)` | 逻辑非（3VL） |
| `And(parts)` / `Or(parts)` | 扁平 n 元合取 / 析取（自动摊平嵌套） |

函数：

- `eval_expr(expr, row) -> True | False | None`：按 3VL 求值，`None` 表示 UNKNOWN。
- `is_null_rejecting(expr, null_aliases) -> bool`：符号化证明——当指定别名的所有列
  都为 NULL（模拟外连接补齐行）时，表达式是否**可证明不为 TRUE**（即恒为 FALSE 或
  UNKNOWN）。这是“外连接可转内连接”的判定核心。
- `null_status_explanation(expr, null_aliases) -> str`：给出证明结论文本
  （在补齐行上为 FALSE / UNKNOWN / TRUE，或依赖非空列、无法仅凭表达式证明）。
- `split_conjuncts` / `and_exprs` / `referenced_aliases` / `expr_to_text`：合取摊平、
  合并、别名收集与 SQL 风格渲染。

### 2.2 计划（`predrewrite.plans`）

| 节点 | 说明 |
| --- | --- |
| `Table(name, columns, rows)` | 基表，允许重复行与 NULL |
| `Scan(alias, table, node_id)` | 绑定别名的基表扫描 |
| `Filter(source, expr, pids, node_id)` | **连接后 WHERE 过滤**；`pids` 与顶层合取项一一对应 |
| `InnerJoin(left, right, on, node_id)` | 内连接，`on` 为匹配条件 |
| `LeftOuterJoin(left, right, on, node_id)` | 左外连接，右子树为空值补齐侧 |

函数：`evaluate_plan(plan)`（返回有序行列表，袋语义）、`canonical_bag(rows)`
（转成多重集合 Counter，便于顺序无关比较）、`plan_schema(plan)`、`plan_to_text(plan)`。

### 2.3 改写引擎（`predrewrite.rewrite`）

- `rewrite(plan) -> RewriteResult`
  - `RewriteResult.plan`：改写后的新计划（输入不被修改）。
  - `RewriteResult.predicate_events`：`PredicateEvent` 列表，每条记录谓词一次“跳转”
    （`pid`、表达式文本、`action`（pushed/retained）、**真实宿主节点 id**、原因、
    涉及别名、若经由转换则带 `conversion_node`）。一个谓词多跳下推会产生多条事件。
  - `RewriteResult.conversion_events`：`ConversionEvent` 列表，记录每次
    左外连接→内连接（原节点 id、新节点 id、触发谓词、等价性依据）。
  - `destinations()`：每个原始谓词 id 的**最终**宿主节点 id；`events_for(pid)`：完整跳转链。

## 3. 改写规则（语义正确性依据）

引擎只移动 WHERE 谓词，**绝不改写任何连接的 ON 条件**——ON 的 3VL 语义与左外连接的
NULL 补齐行正是保留补齐行、重复行和 UNKNOWN 结果的关键。对每个 Filter 的顶层 AND
合取项，自底向上处理：

**内连接上方**
- 只引用左子树（或只引用右子树）别名的合取项下推到该子树；
- 同时引用两侧的合取项无法在连接下方求值，保留在连接上方；
- 不引用任何别名的常量谓词保守保留。

**左外连接上方**（右子树为空值补齐侧）
- 只引用保留侧（左）别名的合取项与内连接同样下推，它看不到补齐行；
- 把所有引用了补齐侧别名的合取项作为一个**整体合取式**，用
  `is_null_rejecting` 做符号证明：
  - 若可证明该合取式在补齐行上恒为 FALSE/UNKNOWN，则 WHERE 会删除所有补齐行，
    该外连接**等价于内连接**，执行转换，再按内连接规则重新分派各合取项，下推可继续；
  - 否则（合取式在补齐行上可能为 TRUE，或结论依赖无法证明的非空列）连接保持外连接，
    所有相关合取项保留在其上方，并在追踪中注明须保留的原因。

证明按**整个合取式**进行，因此 `r.x > 5 AND r.y IS NULL` 虽含单独不拒绝空行的
`IS NULL`，整体在补齐行上为 UNKNOWN（UNKNOWN AND TRUE），仍可合法触发转换。
对无法仅由表达式证明的情形（如与保留侧列比较、含 `IS NULL` 逃生分支的 OR），
引擎一律保守保留，绝不臆测。

## 4. 复现步骤

环境：Python 3.11.6，仅标准库，无需安装任何依赖。

```bash
# 方式一：标准 unittest 发现（从工作区根目录）
python3 -m unittest discover -s tests -p 'test_*.py'

# 方式二：隔离模式（-I 不自动加入当前目录，用自带入口显式引导路径）
python3 -I run_tests.py
python3 -I run_tests.py -v

# 端到端演示（运行时真实构建、改写、求值并核对多重集合）
python3 -I demo.py
```

## 5. 验证范围（验收场景均以真实测试为准）

共 **67 个 unittest 用例**，对 `predrewrite` 包达到 **100% 语句与分支覆盖**
（`python3 -I -m coverage run --branch --source=predrewrite run_tests.py` 后
`coverage report` 全绿；coverage 为本地已有工具，未安装新依赖）。

题面要求的故障恢复 / 边界 / 交错场景均作为验收条件显式覆盖：

1. **含 IS NULL 和 OR 的双层左外连接（必须保持外连接）**
   `test_equivalence_exhaustive.RightNestedTwoLevelLojTests.test_required_scenario_is_null_and_or_must_stay_outer`
   —— `(c.z = 1 OR c.z IS NULL) AND b.y IS NULL`，证明在补齐行上可为 TRUE，
   两个连接均不转换、谓词保留，并对 **9 261 组小表（含重复行/空表/NULL）穷举**
   比较改写前后多重集合完全一致。

2. **可合法转内连接的相似计划**
   `test_required_similar_plan_legally_converts_to_inner`（仅顶层转换、下层保持外连接）
   与 `test_two_conjunctions_both_rejecting_convert_both_joins`（两层级联转换）
   —— 同样 9 261 组小表穷举，验证转换事件、触发谓词与持续下推，且结果多重集合一致。

3. **三值逻辑与补齐/重复行**：`test_plans.py` 验证 ON 上 UNKNOWN 不匹配但仍产生补齐行、
   WHERE 只保留 TRUE、`IS NULL` 保留补齐行、OR 的 UNKNOWN 分支语义、内连接的
   笛卡尔重复度（2×2=4 份相同行）。

4. **无法证明时的保守保留**：跨侧比较（结论 `_FREE`）与含 `IS NULL` 逃生分支的 OR
   （结论可在补齐行为 TRUE）分别验证保留原因；保留侧列上的 `IS NULL` 不臆测为可判定。

5. **交错与深嵌套**：`test_fuzz_equivalence.py` 用固定种子（20261123 / 99173）随机生成
   250 + 60 个含深嵌套 AND/OR/NOT、全部比较运算符、跨侧引用、随机内外连接树与随机小表
   的计划，逐一核对改写前后多重集合、追踪完整性与重复改写的幂等性。

6. **边界**：空表、零行输入、常量谓词、堆叠 Filter 合并、pid 与合取项数量不一致的健壮处理、
   构造器非法参数拒绝等（`test_edge_cases.py`）。

> 说明：所有等价性结论都来自对原计划与改写计划的**实际求值比较**，演示脚本 `demo.py`
> 亦在运行时真实计算，未使用任何自写演示的预置输出替代功能验证。未执行的测试不存在——
> 套件中的 67 个用例在本次环境全部运行通过。

## 6. 设计边界（明确不做的事）

- 不做基于统计信息/约束/域的推理；判定只依赖表达式本身的符号化 3VL 证明，证明不了就保留。
- 不重写、不下推连接 ON 条件，不改变左右外连接方向，不引入右外连接/全外连接。
- 不进行表达式恒等化简（如常量折叠）之外的代数优化；常量谓词保守保留。
- 仅在本地合成数据上运行，不访问网络、账户或真实系统目标。
