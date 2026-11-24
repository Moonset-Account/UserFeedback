# 列存行组裁剪模块

一个无第三方依赖的 Python 3.11 模块：读取列存行组摘要（整数列上下界、空值统计、摘要确认的列间关系）后，按 SQL 三值逻辑判断过滤表达式：

- `SKIP`：摘要允许的行中没有任何一行会使表达式得到 `TRUE`，整组确定跳过；
- `PASS`：摘要允许的每一行都使表达式得到 `TRUE`，整组确定通过；
- `READ`：既不能证明无 `TRUE` 行，也不能证明全部为 `TRUE`，仍需读取数据。

SQL `WHERE` 只接受三值逻辑中的 `TRUE`；`FALSE` 和 `UNKNOWN` 都不会产出。

## 安装与运行环境

只使用 Python 标准库，在 Python 3.11.6 下实现和验证。无需联网或安装依赖。

```bash
python3 --version
python3 -m unittest discover -s tests -v
```

## 公开 API

核心入口：

```python
from pruning import ColumnStats, prune_row_group, Decision

result = prune_row_group(
    "x > 60 AND x < 40",
    {
        "x": ColumnStats(lower=0, upper=100, may_be_null=False),
        "y": ColumnStats(lower=0, upper=100, may_be_null=True),
    },
    relationships=["x < y"],
)

print(result.decision)       # Decision.SKIP，字符串值为 "skip"
print(result.status)         # "SKIP"
print(result.skipped)        # True
print(result.reasons)        # 可复核的中文依据
print(result.to_dict())      # JSON-like 结构化证据
```

也可以使用别名 `prune`、`decide`，或用 `decide_status(..., uppercase=True)` 只取状态字符串；还可以使用原始字典参数：

```python
from pruning import prune_from_parts

result = prune_from_parts(
    "x >= 0 OR x IS NULL",
    bounds={"x": (0, 10)},
    may_be_null={"x": True},
    all_null={"x": False},
)
```

### `ColumnStats`

规范字段：

| 字段 | 含义 |
| --- | --- |
| `lower` / `upper` | 非 NULL 整数的包含性最小/最大值；`None` 表示无界或未知 |
| `may_be_null` | 是否可能出现 NULL；未知时建议保持默认 `True`，避免错误的整组放行 |
| `all_null` | 是否整列全为 NULL |

兼容关键字：`min`、`max`、`lo`、`hi`、`null_possible`、`can_be_null`、`nullable`、`has_null`、`is_all_null`、`all_values_null`，以及 `null_count` / `row_count`。

列统计也可以直接传字典或元组：

```python
{
    "x": {"min": 0, "max": 10, "may_be_null": False},
    "y": (0, 100, True, False),   # lower, upper, may_be_null, all_null
}
```

### 列间关系

`relationships` 接受下列形式，操作符包括 `=`、`!=`、`<`、`<=`、`>`、`>=`：

```python
[
    ("x", "<", "y"),
    "x <= z",
    Relation("a", "=", "b"),
    {"p": {"<": "q"}},
    ["m", "n", "o"],  # flat ascending order: m < n AND n < o
]
```

关系按“同一行中两个值均非 NULL 时成立”解释；若任一端为 NULL，SQL 比较为 `UNKNOWN`，关系不产生整数约束。

### 表达式形式

1. SQL-like 字符串：

```text
x > 10 AND y < 3
x IS NULL OR y IS NOT NULL
NOT (a = 1 OR b != 2)
x + 1 < y
```

2. Python 构造器：

```python
from pruning import col, and_, or_, not_, is_null

expr = and_(col("x") > 10, col("x") < 20)
expr = or_(col("x") >= 0, is_null("x"))
```

3. 元组/字典 DSL：

```python
("and", ("gt", "x", 10), ("lt", "x", 20))
{"op": "or", "args": [{"op": "is_null", "operand": "x"}]}
```

## 返回结果与裁剪依据

`PruneResult` 提供：

- `decision`：`Decision.SKIP` / `Decision.PASS` / `Decision.READ`；
- `skipped`、`all_passed`、`needs_read`（以及 `is_skip`、`is_pass`、`should_read`）；
- `status`：大写 `"SKIP"` / `"PASS"` / `"READ"`；
- `reasons` / `basis` / `explanation`：面向复核的文字依据；
- `true_witness`：一个摘要允许、可使表达式为 `TRUE` 的示例赋值；
- `not_true_witness`：一个可使表达式为 `FALSE` 或 `UNKNOWN` 的示例赋值；
- `truth_paths` / `not_true_paths`：每条 DNF 路径是否可行、矛盾原因、空值状态、推断区间和示例值；
- `to_dict()`：上述信息的字典形式。

### 同一列两个子条件分别可行、联合无解

```python
result = prune(
    "x > 60 AND x < 40",
    {"x": ColumnStats(0, 100, False)},
)
assert result.skipped
print("\n".join(result.reasons))
```

依据会显示严格整数比较如何变成包含性边界：

```text
TRUE 路径 1：(x > 60) AND (x < 40)
  无解依据：约束形成负环（x >= 61；x <= 39），边界和为 -22 < 0，无合法整数取值
```

其中 `x > 60` 在整数域等价于 `x >= 61`，`x < 40` 等价于 `x <= 39`。两个条件单独看都能落在 `[0,100]` 内，但交集为空。

### 空值阻止整组通过，但可被 `IS NULL` 分支恢复

```python
stats = {"x": ColumnStats(2, 10, may_be_null=True)}

prune("x >= 2", stats).decision
# Decision.READ：非 NULL 行可为 TRUE，但 NULL 行是 UNKNOWN

prune("x >= 2 OR x IS NULL", stats).decision
# Decision.PASS：非 NULL 行走比较为 TRUE，NULL 行由 IS NULL 为 TRUE
```

### 列间关系参与证明

```python
stats = {
    "x": ColumnStats(0, 100, may_be_null=False),
    "y": ColumnStats(0, 100, may_be_null=False),
}

prune("x < y", stats, relationships=["x < y"]).all_passed
# True

prune(
    "x > 50 AND y < 40",
    stats,
    relationships=["x < y"],
).skipped
# True：x >= 51、y <= 39、x < y 形成负环
```

## 实现要点

1. 将三值逻辑表达式编译成四类 DNF 分支：
   - `T`：表达式为 TRUE；
   - `F`：表达式为 FALSE；
   - `N`：不为 TRUE，即 FALSE 或 UNKNOWN；
   - `P`：不为 FALSE，即 TRUE 或 UNKNOWN。
2. 每个分支记录列的空值状态（要求 NULL / 要求 NOT NULL）。
3. 整数比较被规范化为差分约束 `u - v <= c`。例如：
   - `x > 60` → `0 - x <= -61`，即 `x >= 61`；
   - `x < 40` → `x - 0 <= 39`；
   - `x < y` → `x - y <= -1`。
4. 列上下界作为相对固定零变量的约束加入；Floyd–Warshall / Bellman–Ford 检测负环并构造摘要内整数示例。
5. `!=` 分支通过“小于或大于被禁止差值”的递归分支检查。
6. DNF 采用惰性枚举。遇到不支持的线性结构或分支数超过可复核上限时，结果安全降级为 `READ`，不会产生错误跳过或错误整组放行。

该分析把统计摘要视为实际行的超集：摘要区间内的所有整数和允许的空值状态都作为可能取值。因此：

- 超集内没有 TRUE 状态 ⇒ 实际行组可安全跳过；
- 超集内所有状态都为 TRUE ⇒ 实际行组可安全整组通过；
- 其余情况保守读取。

稀疏数据、未知空值相关性或未提供的列间关联通常只会导致 `READ`，不会破坏安全性。

## 验证范围

测试全部使用本地合成数据，不访问网络或真实目标。

- `tests/test_pruning.py`：正常场景、包含性整数边界、同列联合矛盾、AND/OR/NOT、SQL 空值三值语义、全 NULL/非空/可能为空、列间关系、原因与 witness、多种公开输入 API。
- `tests/test_exhaustive.py`：构造小规模整数域并枚举所有取值组合作为独立 oracle；覆盖所有比较/空值原子、多种空值统计和列间关系，以及 600 个固定随机种子生成的嵌套逻辑表达式，断言结果与逐行三值逻辑完全一致。
- `tests/test_parser.py`：字符串解析、关键字、括号、`IS [NOT] NULL`、函数式 `ISNULL(x)` 和简单整数偏移。
- `tests/test_synthetic_rows.py`：生成 300 个随机稀疏行组和一个满足确认关系的合成数据集，逐行执行表达式，断言：
  - `SKIP` 时真实 TRUE 行数为 0；
  - `PASS` 时所有真实行均为 TRUE；
  - 其他情况返回 `READ`。

最终命令与结果以实际运行为准。本次实现完成后的实测结果为 Python 3.11.6、25 个测试全部通过：

```bash
python3 -m unittest discover -s tests -v
# Ran 25 tests in ...
# OK
```
