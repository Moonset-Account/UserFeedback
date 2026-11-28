# 分步装载规划器（Staged Load Planner）

为一批互相引用的迁移记录规划 INSERT / UPDATE（补写）语句序列。规划器先在
行级依赖图上选出**总代价最低的外键暂缓集合**（临时置空），再排出插入与
补写顺序，并对产生的**每一条语句**模拟检查主键、非空和外键约束。

仅使用 Python 3.11 标准库，无第三方依赖。

## 问题模型与规则

- 节点是一行记录 `(表名, 主键元组)`；有向边 `source -> target` 表示
  source 的某个外键引用 target，因此 **target 必须先装载**。
- 库中已存在的目标键（`existing_keys`）不产生依赖，也不需要暂缓。
- 一条外键边可暂缓，当且仅当它的**所有列**都允许临时置空
  （`nullable=True` 且声明了 `defer_cost`）。复合外键只能**整组暂缓**，
  不能只置空其中一列；组内任一列为非空或未授权置空，整条外键不可暂缓。
- 非空列（`nullable=False`）必须在首次 INSERT 时就有值，因此涉及非空列
  的外键不能暂缓。
- 暂缓一条边的代价是其各列 `defer_cost` 之和。
- 若不可暂缓的边本身构成环，则无解，规划器返回该硬子图中的一条
  **不可打断的依赖环**。
- 求解目标等价于行级有向图上的**最小代价反馈边集**（NP 难）：

  - `minimum_feedback_edge_set`：带剪枝的分支定界精确算法；
  - `brute_force_minimum_feedback_edge_set`：2^n 穷举，仅允许
    可暂缓外键 ≤ 12 组，用于交叉核对。

## 公开 API

```python
from migration_planner import (
    # 结构与数据
    ColumnSpec,        # 列：name, nullable, defer_cost
    ForeignKeySpec,    # 外键：name, columns, referenced_table, referenced_columns
    TableSpec,         # 表：name, key_columns, columns, foreign_keys
    Row,               # 记录：table, values（None 表示 SQL NULL）
    # 语句与结果
    Insert, Update, DeferredFK, CycleEdge, PlanResult,
    # 入口与检查器
    plan_load,
    ConstraintChecker,
    # 求解器（可单独使用 / 核对）
    minimum_feedback_edge_set,
    brute_force_minimum_feedback_edge_set,
    find_unbreakable_cycle,
    # 异常
    PlanningError, SchemaError, DanglingReferenceError,
    ConstraintViolationError,
)
```

### `plan_load(tables, rows, existing_keys=None) -> PlanResult`

- `tables`：`dict[str, TableSpec]` 或 `TableSpec` 列表。
- `rows`：本批待装载的 `Row` 列表，键值用普通标量，复合键给多列。
- `existing_keys`：`{表名: {主键元组, ...}}`，库中已存在的目标键。
- 返回 `PlanResult`：
  - 可解：`feasible=True`，含 `statements`（INSERT 与 UPDATE 的有序序列）、
    `inserts`、`updates`、`deferred`（暂缓决策与代价）、`total_cost`；
    `to_sql()` 返回可读 SQL 文本。
  - 无解：`feasible=False`，`cycle` 为 `CycleEdge` 元组（首尾闭合的环），
    `describe_cycle()` 返回可读描述，`reason` 说明原因。

### `ConstraintChecker`

维护模拟数据库状态，逐条语句检查：

- `apply_insert(Insert)`：主键齐全/非空/不重复、非空列首次满足、
  外键目标存在、复合外键整组 NULL 或整组有值；
- `apply_update(Update)`：源行已插入、补写整组赋值、不能写 NULL、
  目标存在、该外键确实处于暂缓状态且不能重复补写；
- `assert_all_closed()`：结束时所有暂缓外键都已补写。

## 快速示例

```python
from migration_planner import ColumnSpec, ForeignKeySpec, TableSpec, Row, plan_load

t_a = TableSpec("a", ("id",),
                (ColumnSpec("id", nullable=False),
                 ColumnSpec("b_id", nullable=True, defer_cost=5)),
                (ForeignKeySpec("fk_b", ("b_id",), "b", ("id",)),))
t_b = TableSpec("b", ("id",),
                (ColumnSpec("id", nullable=False),
                 ColumnSpec("a_id", nullable=True, defer_cost=7)),
                (ForeignKeySpec("fk_a", ("a_id",), "a", ("id",)),))

result = plan_load(
    [t_a, t_b],
    [Row("a", {"id": 1, "b_id": 10}),
     Row("b", {"id": 10, "a_id": 1})],
)
print(result.total_cost)          # 5.0：暂缓代价更低的 a.fk_b
print(result.to_sql())
# INSERT INTO a (id, b_id) VALUES (1, NULL);  -- 暂缓外键: fk_b
# INSERT INTO b (id, a_id) VALUES (10, 1);
# UPDATE a SET b_id = 10 WHERE key = (1);  -- fk_b
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

共 29 个测试，分布在四个文件：

| 文件 | 覆盖范围 |
| --- | --- |
| `tests/test_basic.py` | 互引两表选最低代价暂缓、语句顺序与 NULL 占位、独立检查器复核、同价平局、库中已有键、悬空引用、无环 DAG 零暂缓、自引用环、非空列不可暂缓、双组件交错 |
| `tests/test_composite_fk.py` | 复合外键整组暂缓与整组补写、部分置空在建图期与检查器两处被拒、组内非空列/未授权列使整组不可暂缓、补写必须整组赋值 |
| `tests/test_unsat.py` | 全非空三行硬环的环闭合性与描述、软硬边混合、检查器负面断言（重复主键、缺失外键目标、先 UPDATE 后 INSERT、补写 NULL、重复补写） |
| `tests/test_bruteforce.py` | 60 组随机图上分支定界与 2^n 穷举的最优代价/边集完全一致；恰好 12 组可暂缓边（4096 子集）核对；超过 12 组时穷举拒绝而分支定界仍可解；端到端计划逐语句过检查器 |

## 验证范围与实测证据

- 全部 29 个 `unittest` 用例通过（`Ran 29 tests ... OK`）。
- 穷举交叉核对：随机实例中分支定界与穷举返回的最优代价和边 id 集合逐一
  相等；端到端计划的 `total_cost` 与求解器一致，且每条 INSERT/UPDATE 都由
  独立的 `ConstraintChecker` 重新执行并通过，终态无未闭合暂缓。
- 边界与交错：库中已有目标键免暂缓、悬空引用报错、无环图零代价、
  自引用环、多组件独立成环交错排列、复合键整组规则、非空列首次插入规则、
  无解硬环的首尾闭合性。
- 确定性：同价方案按边 id 字典序决胜；拓扑序与补写序均有确定的排序键，
  重复运行结果稳定。
- 扩展性（开发期实测，非 unittest 套件）：30 条可暂缓边的稠密 10 节点图，
  分支定界约 0.15 秒给出最优反馈边集。

## 目录结构

```
migration_planner/
  __init__.py     # 公开 API 再导出
  model.py        # ColumnSpec / ForeignKeySpec / TableSpec / Row / 语句 / 结果
  errors.py       # SchemaError / DanglingReferenceError / ConstraintViolationError
  graph.py        # 行级依赖图、不可打断环检测、确定性拓扑排序
  solver.py       # 分支定界最小反馈边集 + 2^n 穷举核对
  checker.py      # 逐语句约束检查器
  planner.py      # plan_load 编排：校验 -> 选暂缓集 -> 拓扑 -> 生成并检查语句
tests/
  helpers.py
  test_basic.py
  test_composite_fk.py
  test_unsat.py
  test_bruteforce.py
```
