# querybind：小型查询树的绑定、去相关改写与行集执行

本包面向一个小型 SQL 查询树，实现三件事：

1. **名称绑定（binder）**：把 `ColumnRef` 绑定到具体作用域中的列，处理基表/派生表
   别名、星号展开、别名遮蔽、同层歧义、LATERAL 可见性与 USING 合并列；
2. **自由列分析**：为每个子查询输出自由列及其绑定深度，并基于元数据维护列血缘与
   唯一性；
3. **去相关改写（rewriter）+ 行集执行（executor）**：当相关标量子查询的内外等值
   内键能被元数据证明唯一时，把它提升为一次 `LEFT JOIN`；绑定树与改写树都能用
   小型本地行集执行并核对结果。

仅依赖 Python 3.11 标准库，无第三方依赖，不访问网络。

## 目录结构

```
querybind/
  __init__.py    公开 API 汇总
  model.py       查询树节点（TableRef / DerivedTable / Join / 表达式 / SelectNode）
  catalog.py     Catalog / TableData：本地合成行集与唯一键元数据
  errors.py      绑定错误类型
  binder.py      作用域、绑定、自由列分析、USING 合并、唯一性推断
  rewrite.py     相关标量子查询 -> LEFT JOIN 改写
  executor.py    小型行集执行器
tests/
  _fixtures.py             共享合成数据
  test_binding.py          绑定规则（正常 + 边界）
  test_free_vars.py        自由列与绑定深度
  test_rewrite.py          去相关改写条件与等价性
  test_executor.py         连接/LATERAL/子查询/列序执行
  test_interleaving.py     复合键、多子查询、USING 原列、连接键传播等交错场景
```

## 公开 API

### 构造查询树（`querybind.model`，并在包顶层导出便捷函数）

| 构造器 | 含义 |
| --- | --- |
| `TableRef(name, alias)` | 带别名的基表（省略别名时别名等于表名） |
| `DerivedTable(query, alias, lateral=False)` | 派生表，`lateral=True` 即 LATERAL |
| `Join(join_type, left, right, on=None, using=None, lateral=False)` | `inner/left/right/full` 连接 |
| `SelectNode(projections, from_, where)` | 查询块；投影支持 `Star` 与 `SubqueryExpr` |
| `col(name, table=None, alias=None)` / `lit(value)` / `star(table=None)` | 列、字面量、星号 |
| `eq(a, b)` / `and_(*preds)` / `or_` / `not_` | 谓词便捷构造 |
| `subq(query, alias=None)` | 投影中的标量子查询表达式 |

### 元数据

```python
TableData(name, columns, rows, unique_keys=[("id",), ("tenant", "seq")])
Catalog(tables)          # Catalog.get(name) 取表；unique_keys 是唯一键列名元组列表
```

### 绑定与执行

```python
bound = bind(query, catalog)
bound.report                         # BindReport：free_vars / outputs / scope
bound.report.free_vars               # list[FreeVar(name, qualifier, depth, sibling, base_refs, location)]
bound.report.outputs                # list[OutputDescriptor(name, qualifier, base_refs, unique, position)]
col_ref.resolved                     # ResolvedColumn：限定名、depth、位置、血缘、USING 来源表达式

outcome = rewrite_query(query, catalog)
outcome.bound_before                 # 原始绑定树
outcome.bound_after                  # 改写后重新绑定的树（执行用这棵）
outcome.applied / outcome.skipped / outcome.join_aliases

result = Executor(catalog).execute(bound)
result.names()                       # 输出列名（允许重复）
result.ordered_rows()                # 按输出列顺序的值列表 list[list]
result.columns                       # list[ResultColumn(name, qualifier, position)]
```

### 错误类型（`querybind.errors`）

- `UnresolvedColumnError`：名称无法解析（含 `qualifier/column/location`）。
- `AmbiguousColumnError`：同层多解，`candidates` 给出**全部候选**的限定名、深度、
  兄弟标记、位置与血缘。
- `IllegalLateralError`：非横向派生表引用了左邻表（外层查询块仍可见）。
- `DuplicateAliasError`：派生表缺少别名等。

## 关键语义

### 作用域与绑定深度

- 每个 `SelectNode` 是一个查询块，深度从最外层 `0` 开始递增；`ResolvedColumn.depth`
  记录列所在块的深度。
- 未限定列由内向外查找，**第一个含该列名的作用域胜出**；同一作用域内多个同名候选
  立即抛 `AmbiguousColumnError`，错误中带全部候选位置。
- 限定列 `t.c` 只在名为 `t` 的表符号中查找；别名遮蔽外层同名表。
- 非横向派生表的作用域链只含自身与外层块；LATERAL 派生表额外把左邻作用域追加到链上，
  这些作用域标记 `is_sibling`，自由列报告中对应 `sibling=True`。

### USING 合并列

- USING 列在未限定星号序列中**只出现一次**，排在最前；其余列按“左表后右表”排列。
- 限定表名仍可访问左右原列（`d.id` / `e.id`），原列血缘分别保留。
- 合并列的 `source` 表达式：
  - `inner` / `left`：取左侧值；
  - `right` / `full`：`coalesce(左, 右)`，即首个非空来源。
- 执行器内部用哨兵键 `(None, using_name)` 存放合并值，左右原键独立存放，互不覆盖。

### 唯一性推断与去相关

- 基表唯一键来自 `TableData.unique_keys`（支持单列与复合键）。
- 列血缘 `base_refs` 为 `(基表名, 基表列名)` 元组；派生表原样暴露的列传播血缘，
  内层作用域的唯一键在输出列覆盖时向上继承。
- 内连接中，若连接等值覆盖某侧的一个唯一键，则另一侧的唯一键在结果中保留；
  交叉连接与全外连接不保留任何键。
- 标量子查询提升为左连接的条件：
  1. 投影顶层的**标量**子查询（单列输出）；
  2. WHERE 含“内列 = 外列”相关等值，且相关引用只跨一层；
  3. 内列集合覆盖子查询作用域中可证明的唯一键（单列或复合键）；
  4. 子查询输出表达式本身不引用外层列。
- 内表专属过滤下推到派生表 WHERE；无法下推的残余谓词进入 JOIN ON。
- 左连接零匹配自然产生 NULL，因此改写前后对零匹配行都返回空值。

### 执行器

- 环境是按深度组织的 `list[dict]`，键为 `(限定名, 列名)`；LATERAL 左邻行并入同层 dict。
- 支持 `inner/left/right/full` 连接、ON 与 USING、相关/非相关派生表、相关标量子查询、
  比较/逻辑/算术/`coalesce`，SQL 风格三值逻辑（NULL 比较返回 NULL，AND/OR 短路传播）。
- 标量子查询零行返回 NULL，多于一行抛 `RuntimeError`。
- 输出行按列位置存储，允许同名输出列并存，列序与投影顺序严格一致。

## 复现步骤

要求 Python 3.11.6（本仓库开发与验证版本），标准库即可。

```bash
# 在工作区根目录执行
python3 --version            # 期望 Python 3.11.6
python3 -m unittest discover -s tests -v
```

也可以只跑某个模块：

```bash
python3 -m unittest tests.test_binding
python3 -m unittest tests.test_rewrite
python3 -m unittest tests.test_executor
```

一个最小端到端示例：

```python
from querybind import Catalog, TableData, Executor, bind, rewrite_query
from querybind.model import SelectNode, TableRef, SubqueryExpr, col, eq

catalog = Catalog([
    TableData("emp", ["id"], [{"id": 1}, {"id": 2}], [("id",)]),
    TableData("pay", ["emp_id", "bonus"], [{"emp_id": 1, "bonus": 5}],
              [("emp_id",)]),
])
scalar = SelectNode(
    projections=[col("bonus", table="p", alias="bonus")],
    from_=TableRef(name="pay", alias="p"),
    where=eq(col("emp_id", table="p"), col("id", table="e")),
)
query = SelectNode(
    projections=[col("id", table="e", alias="eid"),
                 SubqueryExpr(query=scalar, alias="b")],
    from_=TableRef(name="emp", alias="e"),
)

original = bind(query, catalog)
outcome = rewrite_query(query, catalog)
assert outcome.applied == 1
assert (Executor(catalog).execute(original).ordered_rows()
        == Executor(catalog).execute(outcome.bound_after).ordered_rows())
# [[1, 5], [2, None]]
```

## 验证范围

测试共 **58** 个 `unittest` 用例，全部从工作区根目录可发现，覆盖题面要求的正常、
边界与交错场景：

- **正常路径**：基表别名、限定/未限定列、星号当前顺序展开、内/左/右/全外连接、
  ON 与 USING、派生表、LATERAL、相关标量子查询、去相关改写及改写前后结果等价。
- **边界**：未限定 USING 列只出现一次、全外 COALESCE 首个非空、限定表名访问原列、
  零匹配补 NULL、标量子查询零行/多行、无 FROM 常量查询、USING 列一侧缺失报错、
  复合唯一键单列不足时禁止改写、非唯一内键禁止改写、非相关子查询不改写、
  交叉连接丢弃唯一键。
- **交错**：从左到右的 LATERAL 链（第二个 LATERAL 同时看到左邻与前一个 LATERAL）、
  同一投影中两个相关标量子查询分别提升为嵌套左连接、内层非横向派生表禁止偷看左邻但
  允许引用外层块、两层嵌套自由列深度、嵌套别名与合并列同名时的结果/列序/歧义位置分别
  核对、唯一键沿内连接链传播。

所有结果均来自对 `querybind` 真实执行的断言；测试夹具是本地合成的小型行集，
没有使用预置输出或外部服务。
