# 单扫描共享计划：计划器 + 小型关系解释器

优化器把多个报表请求合成**一次扫描**。本项目实现计划器，按“可用聚合状态”
判定一组受限关系计划能否共享，并产出一个共享计划 + 每个原始请求的投影；
无法共享时定位具体的阻塞条件。配套一个小型关系解释器，对**改写前**
（逐请求参考执行）与**改写后**（共享计划投影）的具体行和类型做逐行对照。

仅使用 Python 3.11 标准库，无第三方依赖、无网络访问。

## 目录结构

```
sharedscan/
  model.py       领域模型：值/类型族、谓词、聚合、请求、目录（含互斥标示）
  predicates.py  三值逻辑(3VL)谓词求值与谓词树遍历
  states.py      可合并聚合状态机（含去重计数的部分状态与互斥合并约束）
  planner.py     计划器：共享判定、条件 intern、状态注册、阻塞定位
  interpreter.py 小解释器：参考执行 + 共享计划执行（共用同一状态机）
  verify.py      改写前后的行多重集 + 类型族对照
tests/
  synth_data.py  本地合成数据（含 NULL、跨分区重复值、空分区、多类型）
  test_interpreter.py  3VL 与参考解释器语义
  test_planner.py      共享状态注册、AVG 重用、阻塞定位
  test_states.py       状态合并语义（含非互斥合并的防御性拦截）
  test_end_to_end.py   逐行逐类型对照的端到端测试
  test_fuzz.py         随机化差分测试（155 轮随机批次 + 拒绝用例）
demo.py          可运行的端到端演示
```

## 运行方法

```bash
# 运行全部测试（题面指定命令）
python3 -m unittest discover -s tests -v

# 运行演示
python3 demo.py
```

## 公开 API

### 模型（`sharedscan.model`）

- `Request(name, table, partitions, group_keys, aggs, where=None, having=None,
  semantics=Semantics(), key_domain=())` —— 一个原始报表请求。
- `Agg(alias, fn, column, include_null=True)`，`fn` 取 `AggFn`：
  `COUNT_STAR / COUNT / COUNT_DISTINCT / SUM / AVG / MIN / MAX`。
  `include_null` 只对 `COUNT_DISTINCT` 有意义（NULL 是否算一个去重值）。
- 谓词：`Cmp(column, op, literal)`、`IsNull(column)`、`IsNotNull(column)`、
  `And(left, right)`、`Or(left, right)`。
- `Semantics(empty_input=..., empty_group=...)`：
  - `EmptyInputPolicy.ONE_NULL_ROW / ZERO_ROWS`：无分组键且零输入行时输出一行
    空聚合值，或零行；
  - `EmptyGroupPolicy.DROP / KEEP_DOMAIN`：有分组键时丢弃缺席组，或按
    `key_domain` 用空聚合值补齐。
- `make_catalog(tables, exclusive_distinct=...)`：构造目录。
  `exclusive_distinct` 是 `(表名, 去重列名)` 的集合，**显式标示**该列在分区
  之间取值互斥（跨分区去重计数可直接相加的前提）。

### 计划器（`sharedscan.planner`）

- `build_shared_plan(requests, catalog) -> SharedPlan`：可共享时返回共享计划；
  否则抛出 `BlockingReasons`，其 `.reasons` 是带请求名、原因代码与说明的
  `Reason` 列表（一次收集全部阻塞条件）。
- `SharedPlan`：`partitions`（联合扫描分区）、`group_keys`、`conditions`
  （条件 id → WHERE 谓词）、`state_specs`（状态键 → `StateSpec`）、
  `projections`（请求名 → `Projection`），以及 `describe()` 文本摘要。
- 阻塞原因代码：`TABLE_MISMATCH`、`UNKNOWN_TABLE/PARTITION/COLUMN`、
  `GROUP_KEY_MISMATCH`、`TYPE_MISMATCH`、`NON_NUMERIC_AGG`、
  `DISTINCT_MERGE_NOT_EXCLUSIVE`、`BAD_HAVING_REF`、`INVALID_LITERAL_TYPE`、
  `DUPLICATE_ALIAS`、`DOMAIN_ARITY`、`AGG_EMPTY`、`NAME_DUPLICATE`。

### 解释器与对照（`sharedscan.interpreter` / `sharedscan.verify`）

- `execute_request(request, catalog) -> RequestResult`：改写前参考执行。
- `execute_shared_plan(plan, catalog) -> {请求名: RequestResult}`：改写后执行。
- `verify_plan(requests, plan, catalog) -> VerifyReport`：逐请求对照，
  `.ok` 为总判定，`.mismatches` 给出 `COLUMNS / ROW_MULTISET / TYPE_FAMILIES`
  三类差异；`report.raise_if_failed()` 可直接断言。
- `type_strict_equal(a, b)`：类型敏感相等（`1 ≠ 1.0`、`1 ≠ True`、
  `None` 只等于 `None`）。

## 核心设计与判定规则

### 1. 一次扫描与条件聚合

所有请求必须读同一张表、分组键集合一致、分区都存在。联合扫描按稳定次序遍历
各分区的每一行；不同的 WHERE 谓词被 intern 成条件 id，相同谓词复用同一 id，
每个聚合状态在“通过对应条件”的行上累积（条件聚合）。

状态键为 `(种类, 列, 条件id, include_null, 分区范围 frozenset)`。**分区范围是
状态身份的一部分**：谓词相同但分区子集不同的请求各持一份状态，避免把不属于
某请求的分区行计入它的聚合。

### 2. AVG 由成对状态重用

AVG 不建立专属状态，而是绑定同一对 `sum` 与 `count(非空)` 状态，投影阶段做
`sum / count`，count 为 0 时结果为 NULL。因此 AVG、SUM、COUNT 请求之间共享
同一批状态；AVG 的非空输出类型恒为 `float`，即使输入列是 `int`。

### 3. 去重计数的互斥前提

- 单分区：状态保留具体去重值集合（`distinct_set`），结果天然精确，无需互斥标示。
- 跨分区：跨分区传递的部分状态只保留计数（`distinct_partial`），值已被丢弃。
  两个部分计数能相加，当且仅当分区在该列上互斥（相加 == 集合并集基数）。
  计划器要求 `(表, 列)` 在目录中**显式标示** `exclusive_distinct`，否则定位
  `DISTINCT_MERGE_NOT_EXCLUSIVE` 并拒绝共享。
- 状态机对非互斥的部分计数合并另有防御性 `InvalidDistinctMerge` 拦截。

### 4. 空组、缺失值与过滤顺序的语义差异

这些差异**不阻塞共享**，而在投影阶段承载：

- WHERE 用 SQL 三值逻辑：与 NULL 比较为 UNKNOWN，UNKNOWN 不放行；
  `IS NULL / IS NOT NULL` 恒为真/假；AND/OR 按 Kleene 规则组合。
- 执行顺序固定为：拼分区 → WHERE → 分组聚合 → 空输入/空组处理 → HAVING。
  WHERE 在聚合前、HAVING 在聚合后，二者不可互换。
- 无分组键零行：`ONE_NULL_ROW` 给空聚合值（计数 0、SUM/MIN/MAX/AVG 为 NULL），
  `ZERO_ROWS` 给零行。
- 有分组键：`DROP` 只留真实组；`KEEP_DOMAIN` 补齐缺席组。**NULL 键本身是合法
  真实组**，与“缺席待补齐”是两回事，测试中有专门断言。
- HAVING 在空组补齐之后求值，因此可以基于 0/NULL 过滤补齐行。

### 5. 类型族

`int / float / str / bool` 为不同类型族，NULL 无类型族。bool 不与 int 混比，
字符串不与数字混比（结果为 UNKNOWN）；int 与 float 同属数值族可比较。验证层用
类型敏感的多重集比较，能抓出 `1` 与 `1.0`、`1` 与 `True` 之类的隐性差异。

## 验证范围（基于实际执行与断言）

`python3 -m unittest discover -s tests -v` 共 62 个测试，全部通过，覆盖：

- 三值逻辑真值表、NULL 比较、类型族不混比；
- 全部聚合函数、AVG 的 sum/count 成对投影与 float 输出；
- 空输入两种策略、空组 DROP/KEEP_DOMAIN、NULL 键与补齐组的区分、HAVING 顺序；
- 共享状态注册：相同谓词 intern、不同谓词条件态分离、分区范围进入状态身份、
  AVG/SUM/COUNT 的状态重用；
- 去重计数：单分区值集合精确、跨分区未标示互斥被阻塞、标示后部分计数相加、
  非互斥合并的防御性异常、`include_null` 开关；
- 全部阻塞原因代码，且一次返回多个原因；
- 端到端逐行（多重集）逐类型对照，并对关键场景断言手工期望值；
- 随机化差分测试：固定随机种子，155 轮随机请求批次（分组/grand-total/去重/
  互斥目录）全部改写前后一致，40 个非法跨分区去重用例全部被计划器拒绝；
- `demo.py` 还实证：当“互斥标示与实际数据不符”时，解释器对照会把错误结果
  显式抓出（参考侧精确集合 3 vs 错误相加 4）。

## 已知边界（如实记录）

- 计划器要求分组键**集合完全一致**，不做不同分组粒度之间的 ROLLUP 上卷推导；
  无分组键请求与分组请求因此不能共享，会定位 `GROUP_KEY_MISMATCH`。
- 谓词限定为原子比较（含 `IS [NOT] NULL`）的 AND/OR 组合，无算术表达式、
  无 NOT 节点（可用 `!=` / `IS NOT NULL` 表达相应语义）。
- “互斥”是目录给出的可信元数据前提；计划器校验“是否标示”，解释器对照负责
  抓出“标示与数据不符”的情形，但不会在计划期扫描数据去证伪互斥性。
