# grouping_sets — 分批增删的分组集合聚合算子

一个纯 Python 3.11 的库：按调用方给定的**分组列**、**分组集合列表**和**聚合列**
维护一份分组集合聚合状态，支持**分批**接收新增行（adds）与撤回行（retracts），
每批返回**相对上一批真正改变的结果行**。

无第三方依赖，仅使用标准库。

## 快速开始

```python
from grouping_sets import GroupingSetsSpec, GroupingSetsEngine, format_rows

spec = GroupingSetsSpec(
    grouping_columns=["region", "product"],       # 分组列（输出列顺序）
    grouping_sets=[["region", "product"],         # 明细
                   ["region"],                    # 按 region 小计
                   []],                           # 空分组集合 = 总计行
    aggregates=[("count", "*", "n"),              # COUNT(*)
                ("count", "amount", "n_amount"),  # COUNT(amount)，跳过 NULL
                ("sum", "amount", "total"),       # SUM
                ("min", "amount", "lowest")],     # MIN
)
engine = GroupingSetsEngine(spec)

batch = engine.apply(
    adds=[{"region": "north", "product": "a", "amount": 10},
          {"region": "north", "product": "a", "amount": 4}],
    retracts=[],
)
print(format_rows(spec, batch.rows))
batch.upserts          # 本批新增或内容变化的结果行（已排序）
batch.deletes          # 本批消失的结果行（已排序，携带消失前的值）
batch.snapshot         # 本批之后的完整快照
batch.changed          # 本批是否有任何变化
```

## 公开 API

### `GroupingSetsSpec(grouping_columns, grouping_sets=(), aggregates=(), *, include_grand_total=False)`

一次性校验并冻结规格，之后可被多个引擎共享。

* `grouping_columns`：全部可选分组列，**顺序即输出列顺序**。
* `grouping_sets`：每个元素是一个分组集合（列名序列）；**允许重复**，重复的集合
  各自维护状态、各输出一份结果。空序列 `[]` 表示空分组集合（总计行）。
* `aggregates`：`(func, column)` / `(func, column, output)` 元组或 `AggregateColumn`。
  `func` 为 `count` / `sum` / `min`（大小写与首尾空格不敏感）；`column="*"` 只对
  `count` 合法（COUNT(*) 计行数，不读数据）。`output` 缺省等于 `column`。
* `include_grand_total=True`：在末尾追加一个空分组集合。

校验失败抛 `SchemaError` 的子类：
`DuplicateColumnError`（分组列重名、同一集合内列重复、聚合输出列重名）、
`UnknownColumnError`（集合引用了未声明列）、`SchemaError`（函数不支持、`*` 用于
非 COUNT、聚合输出与分组列同名、形状不合法）。

属性：`grouping_columns`、`grouping_sets`、`aggregates`、`aggregate_outputs`、
`output_columns`。

### `GroupingSetsEngine(spec, *, strict=True)`

| 方法/属性 | 说明 |
| --- | --- |
| `apply(adds=(), retracts=())` | 应用一批增删，返回 `BatchResult`。参数必须是**行的可迭代对象**（单个 mapping 会报错，请用 `[row]`）。 |
| `add(row)` / `retract(row)` | 单行便捷入口，等价于 `apply(adds=[row])` / `apply(retracts=[row])`。 |
| `current_snapshot()` / `current_rows()` | 最近一次成功提交后的快照 / 拍平的行。 |
| `batch_index` | 成功提交的批次序号（从 0 开始，从未提交为 `-1`）。 |
| `stored_row_counts()` | 每个分组集合状态里保存的输入行数（诊断"每个状态各扣一次"）。 |
| `group_counts()` | 每个分组集合当前的有效分组数。 |
| `reset()` | 清空状态，回到初始快照。 |

`strict=True`（默认）：任一撤回在批前状态里找不到足够匹配的行，就抛
`UnmatchedRetractionError` 且**整批不生效**（状态、批次序号都不变）。
`strict=False`：忽略无法匹配的部分，把被忽略的撤回行数记在
`BatchResult.unmatched_retractions`（以及按集合分布的 `unmatched_by_set`）。

### `BatchResult`

`batch_index`、`upserts`、`deletes`、`snapshot`、`unmatched_retractions`、
`unmatched_by_set`、`changed`、`rows`。

* `upserts`：本批**新增**或**内容真正变化**的行（内容没变不算变化）；
* `deletes`：本批**消失**的行，携带消失前的值；
* 行的身份是 `(set_index, grouping_values)`。组内 COUNT/SUM/MIN 变化时报告为
  `upserts`（更新），而不是"先删后插"。

### `ResultRow`

不可变 dataclass，字段：`set_index`（规格中的分组集合序号）、`grouping_values`
（按分组列顺序，被省略的列为 `None`）、`grouping_bits`（GROUPING 位图）、
`aggregates`（按聚合列顺序）。属性 `grouping`（位图打包的整数，首列最高位）、
`is_grand_total`；方法 `as_dict(grouping_columns, aggregate_outputs)`。

### 其它导出

`AggregateColumn`、`GroupingSet`、`AGGREGATE_FUNCS`、`COUNT_STAR`、
`normalize_value`、`is_null`、`diff_snapshots`、`snapshot_map`、`sort_key_for`、
`make_row`、`rows_as_dicts`、`format_rows`，以及全部异常类型
（共同基类 `GroupingSetsError(ValueError)`）。

## 语义细节

### 分组与输出

* 每个分组集合的每一组输出一行；输出行的分组列按**规格的分组列顺序**排列，
  与该集合书写顺序无关。
* 非空分组集合的组在**最后一行被撤回后从结果中消失**（出现在 `deletes`）。
* **空分组集合永远输出一行**：没有数据时 `COUNT = 0`、`SUM/MIN = None`。
  它在构造引擎时就已经作为初始快照的基线存在（所以第一批"没有数据"不会产生
  一次 upsert）。
* 空分组列（`grouping_columns=[]`）同样合法，此时只有一个 `()` 分组键。

### 小计空值 vs 数据 NULL —— 用位图区分

被某分组集合省略的列在输出里是 `None`；数据本身就是 NULL 时也是 `None`。
两者**不使用任何哨兵对象**，而是由 `ResultRow.grouping_bits` 区分：
位为 `0` 表示该列参与本行分组（此处的 `None` 是数据 NULL），
位为 `1` 表示该列被省略（此处的 `None` 是小计空值）。

例：`grouping_columns=["region","product"]`、集合 `["region"]` 的行
`("n", None)` 若 `grouping_bits == (0, 1)`，则 `product` 是小计空值；
集合 `["region","product"]` 的行 `("n", None)` 若 `grouping_bits == (0, 0)`，
则 `product` 是真 NULL，二者是不同的组、可以同时存在。

### 重复分组集合与"一次撤回只扣一次"

状态按 `(分组集合序号, 分组键)` 组织，重复的集合只是两个不同序号的独立状态：
它们各有一份计数、各输出一份结果；而**一次撤回对每个状态至多扣减一次**，
不会因为在列表里出现两遍就在同一份状态里扣两次。

### 聚合语义

* `COUNT(*)`：行数（含聚合列为 NULL 的行）；`COUNT(col)`：该列非空值个数。
* `SUM` / `MIN`：忽略 NULL；没有非空值时结果为 `None`（不是 0）。
* 状态不保存"运行中的最小值/和"，而是保存该组每行的聚合输入载荷的多重集合
  （`Counter`），每批由多重集重新归约。因此：
  * 重复最小值在撤掉一份后仍然正确，撤到最后一份时最小值会上移；
  * 全空聚合列在加入/撤回非空值时可在 `None` 与数值之间来回切换；
  * 撤回不需要"反向聚合"，不会产生累积漂移。
* 求和：整数用精确整数加；浮点用 `math.fsum`（精确舍入）；`Decimal` 用
  十进制精确加；`float` 与 `Decimal` 混在同一组里会抛 `AggregateTypeError`
  （拒绝静默丢精度），并保证**状态不被这批改动污染**。

### 输入校验与值规范化

* 输入行必须是 `Mapping`，必须包含所有分组列与聚合源列，缺列抛
  `MissingColumnError`；多余列被忽略。
* 分组值经 `normalize_value` 规范化成可哈希的不可变值：
  `list/tuple → tuple`、`set → frozenset`、`dict → 排序后的 tuple`，逐元素递归。
  不可哈希且无已知规则的值抛 `UnhashableGroupingValueError`。
* 分组键或聚合输入中的 `NaN` / 非有限 `Decimal` 抛 `NonFiniteValueError`
  （NaN 不等于自身，无法作为分组键；MIN/排序也失去良定义）。`±inf` 在聚合输入中
  允许。
* 相等语义沿用 Python：`1 == 1.0 == Decimal("1")` 落到同一个组。
* 排序：分组键按"None 优先、再类型名、再 `repr`"的确定全序排序，
  混合类型也不会抛 `TypeError`；同进程内输出顺序稳定。

### 批内语义

* 先规范化全部输入，再把新增与撤回合成**净增量**；
* 严格模式只在净增量为负且超出可用数量时报错，因此"同一批里先加后撤同一行"
  是合法的，并且报告为无变化；
* 聚合计算出错（如类型不兼容）时整批不提交。

## 运行方法

```bash
python3 -m unittest discover -s tests -v     # 全部测试
python3 demo.py                              # 可运行示例
```

（测试用 `tests/` 作为包，故请从仓库根目录运行；`python3 -m unittest` 会把
当前目录加入 `sys.path`。）

## 三个可复算的例子

完整断言见 `tests/test_worked_examples.py`，可运行输出见 `demo.py`。
三例的规格都是 `grouping_columns=["grp"]`、集合 `[["grp"], []]`、
聚合 `COUNT(*) n`、`COUNT(v) n_v`、`SUM(v) total`、`MIN(v) lowest`。

### 1. 重复最小值

| 数据（组 `g`） | `v` | n | n_v | total | lowest |
| --- | --- | --- | --- | --- | --- |
| 初始 | 4, 1, 1, 7 | 4 | 4 | 13 | 1 |
| 撤一个 `1` | 4, 1, 7 | 3 | 3 | 12 | 1 |
| 再撤一个 `1` | 4, 7 | 2 | 2 | 11 | **4** |

只记"当前最小值"的实现会在第三步停在 1；本实现由多重集重算，正确上移到 4。

### 2. 全空聚合列

| 数据（组 `g`） | `v` | n | n_v | total | lowest |
| --- | --- | --- | --- | --- | --- |
| 初始 | None, None, None | 3 | 0 | None | None |
| 加入 8 | None×3, 8 | 4 | 1 | 8 | 8 |
| 撤掉 8 | None, None, None | 3 | 0 | None | None |

`COUNT(*)` 计行（3），`COUNT(v)` 跳过 NULL（0），`SUM/MIN` 无非空值时为 `None`。
空分组集合在完全无数据时是 `n=0, n_v=0, total=None, lowest=None`。

### 3. 负数

| 数据（组 `g`） | `v` | n | n_v | total | lowest |
| --- | --- | --- | --- | --- | --- |
| 初始 | -5, -2, -10 | 3 | 3 | -17 | -10 |
| 撤掉 -10 | -5, -2 | 2 | 2 | -7 | -5 |
| 加入 -3 | -5, -2, -3 | 3 | 3 | -10 | -5 |

另测 `-5` 与 `5` 相加为 0 的组仍然存在（COUNT 不为 0），以及负数浮点
`-0.1 + -0.2` 用 `math.fsum` 得到精确舍入结果。

## 验证范围

`python3 -m unittest discover -s tests -v` 共 **128** 个测试，全部通过（实测输出
见下）。测试全部使用本地合成的内存数据，不联网、不读外部文件、不安装依赖。

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_spec.py` | 分组列/集合/聚合列的校验、重复集合保留、输出列顺序、各种非法输入 |
| `tests/test_aggregates.py` | 单批结果手算比对、小计与总计、COUNT(*) vs COUNT(col)、重复行、缺列、额外列、NULL 分组、整数/浮点求和 |
| `tests/test_batches.py` | 每批差量只含真正变化的行、内容不变不报告、批内抵消、删除携带旧值、快照分组与排序、多小批与单大批一致 |
| `tests/test_retractions.py` | 最后一行撤回后组消失、小计消失、总计不消失、重复行撤回一份、严格/非严格模式、状态不被失败批次污染、重复集合各扣一次 |
| `tests/test_grouping_bits.py` | 小计空值与数据 NULL 都是 `None` 但位图不同、位图整数打包、集合列序不影响输出、空分组列、重复集合两行 |
| `tests/test_worked_examples.py` | 题面要求的三个可复算例子（重复最小值 / 全空聚合列 / 负数） |
| `tests/test_values.py` | 值规范化、NaN 与非有限 Decimal 拒绝、inf 允许、`Decimal` 精确求和、float+Decimal 拒绝且不污染状态、相等数值同组 |
| `tests/test_interleaved.py` | 与"从零重算"参考实现逐批对照：固定交错脚本、40 批随机增删、重复集合始终同步、组消失后再现、5×400 行大批量 |
| `tests/test_edge_cases.py` | 撤回只在部分集合可匹配、非严格模式计数不按集合数放大、批内净额为零、聚合输出命名、多列混合类型键排序 |
| `tests/test_public_api.py` | 辅助函数、可见性、异常层次、`ResultRow` 可哈希可比较 |

未覆盖/未验证的部分（如实记录）：

* 没有做跨进程持久化或状态序列化，也没有与真实数据库/真实查询引擎对接；
* 没有做并发/多线程压力测试，引擎本身不是线程安全的；
* 浮点极值（溢出为 `inf` 的求和）未单独断言；
* 未测超大规模（百万行级）的性能，仅有一个 2000 行、10 批的对照测试。

## 设计取舍

* **多重集而非可减聚合**：撤回、重复最小值、全空列都靠"由载荷重算"保证正确；
  代价是每批按受影响组的大小重算，换来的是无累积漂移。
* **不引入哨兵值**：小计空值仍是 `None`，身份由 GROUPING 位图承担，
  这样输出行可以直接和 SQL 的 `GROUPING()` 语义对照，也避免哨兵泄漏到结果里。
* **重复分组集合保留**：不合并、不去重，按列表位置各算一份。
