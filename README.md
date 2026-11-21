# 三列行值 NOT IN 算子（三值逻辑 + 预建哈希索引）

用 Rust 1.95.0 标准库实现的行值 `NOT IN` 算子。执行器用固定三列、列值可为
`NULL` 的整数行反复探测一张固定右表，算子对每个探测返回 SQL 三值结果
（真 / 假 / 未知），并给出用作 `WHERE` 过滤时应保留的原行号。

- 仅依赖 Rust 标准库（`std::collections::HashMap`），**无第三方依赖**。
- 离线构建与测试：`cargo test --offline`。

## 语义

行相等 `l = r`（三值逻辑）：

1. 任一对**非空**值不等 → `FALSE`；
2. 否则，只要任一列在任意一侧为 `NULL` → `UNKNOWN`；
3. 三列全部非空且相等 → `TRUE`。

`l NOT IN (r1, r2, …)`：

- 存在 `ri` 使行相等为 `TRUE` → `FALSE`；
- 否则存在 `ri` 使行相等为 `UNKNOWN` → `UNKNOWN`；
- 否则（右表为空也属于此情况）→ `TRUE`。

用作 `WHERE` 时只保留 `TRUE` 的行；`UNKNOWN` 与 `FALSE` 都被过滤。

题面给定示例：右表仅 `(2, NULL, 7)`，左行 `(1, NULL, 7)` 第一列 `1 ≠ 2`
（一对非空值不等），行比较直接为假，不存在未知/真匹配，故 `NOT IN` 为真，
该行被 `WHERE` 保留。测试 `spec_example_first_column_distinguishes_and_row_is_retained`
覆盖此场景。

## 公开 API

类型与函数均定义在 `src/lib.rs`，crate 名 `row_not_in`。

| 项 | 说明 |
| --- | --- |
| `type Row = (Option<i64>, Option<i64>, Option<i64>)` | 三列行值，`None` 表示 `NULL`。 |
| `enum TriBool { True, False, Unknown }` | 三值逻辑结果；`TriBool::retained_in_where()` 告知是否被 WHERE 保留。 |
| `struct NotInIndex` | 右表上的预建索引，可重复探测。 |
| `NotInIndex::build(right_table: Vec<Row>) -> NotInIndex` | 一次性预建索引，取得右表所有权。 |
| `NotInIndex::probe(left: Row) -> (TriBool, ProbeReport)` | 探测单行，返回结果与查找计数报告。 |
| `NotInIndex::probe_all(left_rows: &[Row]) -> Vec<TriBool>` | 批量探测，顺序与输入一致。 |
| `NotInIndex::retain_where(left_rows: &[Row]) -> Vec<usize>` | 返回结果为真的行在输入中的原始下标（0 起）。 |
| `NotInIndex::len() / is_empty()` | 右表行数 / 是否为空。 |
| `struct ProbeReport { index_lookups, found_true_match, saw_unknown }` | 单次探测的实际哈希查找次数与匹配标志，供调用方计数。 |
| `row_equals(left, right) -> TriBool` | 朴素三值行相等，作为语义参照。 |
| `naive_not_in(left, right_table) -> TriBool` | 逐条扫描右表的朴素 NOT IN，供交叉验证。 |

## 索引原理与查找次数保证

右表每行按其非空列掩码 `m`（3 位，位 `i` 表示第 `i` 列非空）展开为 `m` 的全部
子集 `sub ⊆ m`。索引键为 `(sub, sub 各列上的值)`，载荷是 8 位掩码，记录哪些
右行空值模式 `m` 能产生该键。

探测左行（非空列掩码 `P`）时枚举 `P` 的全部子集，每个子集执行**一次**
`HashMap` 查找：

- 键 `sub = 0b111` 命中 ⇒ 存在三列全非空且全等的右行 ⇒ 真匹配，结果为假；
- 否则若命中键的载荷中存在右行模式 `m` 满足 `m & (P \ sub) = 0`，即右行在双方
  都非空的列上全部相等、其余列至少一侧为空 ⇒ 未知匹配；
- 载荷中其他模式在 `P \ sub` 上有非空列，其值必与左行不等（否则会被更大的子掩码
  键命中），对应假匹配，不改变结论。

因此单行探测的哈希查找次数恒为 `2^p`（`p` = 左行非空列数）：

| 左行空值组合（非空列数 p） | 查找次数 |
| --- | --- |
| 三列全空（p=0） | 1 |
| 恰一列非空（p=1） | 2 |
| 恰两列非空（p=2） | 4 |
| 三列全非空（p=3） | 8 |

该次数**只取决于三列的空值组合，与右表行数无关**；构建期为 O(右表行数 × 8)，
每次探测 O(2^p) 次哈希查找，最坏 8 次。`ProbeReport::index_lookups` 如实回报
每次探测的实际查找次数，调用方可直接计数。测试
`index_lookup_count_depends_only_on_null_pattern` 用 1 行与 2500 行两张右表
验证同一空值组合的查找次数完全相同。

## 复现步骤

```bash
rustc --version          # 应为 rustc 1.95.0
cargo test --offline     # 离线运行全部单元测试与集成测试
```

期望输出：单元测试 2 个、集成测试 14 个全部通过（共 16 个 `#[test]`）。

## 验证范围

所有结论均来自真实执行的 `cargo test --offline`，未使用任何预置/伪造输出。

正常场景：

- 全非空左行的真匹配 → 假；不同值 → 真；首列不同即整体不等。
- 右行含空、非空列全等 → 未知；真匹配与未知匹配并存时真匹配优先（结果为假）。
- 题面示例：`(1,NULL,7)` 对 `(2,NULL,7)` 为真并被 WHERE 保留。

边界场景：

- 空右表：任意左行（含全空左行）结果恒为真。
- 全空左行对非空右表恒为未知；全空右行对任意左行只能产生未知。
- 右表重复行不改变结果；八种空值模式各一行的交错表。
- 负数与 0 值、小整数域。

交错/混合场景：

- 同一批左行混合真/假/未知结果，`retain_where` 仅保留真行并保持原始下标。
- 含全空行的右表上 NOT IN 不可能为真；移除全空行后可得到真（同测试内双索引对照）。

计数与交叉验证：

- 八种左行空值组合的查找次数恰为 1/2/4/8，且在 1 行与 2500 行右表上一致。
- 确定性伪随机（自带 LCG，无随机依赖）多轮交叉验证：右表规模 0/1/2/7/25/80，
  每轮 400 个随机左行，索引实现与 `naive_not_in` 逐条扫描结果逐例一致，
  并校验报告标志与查找次数自洽。
- 120 个随机左行的 `retain_where` 结果与朴素过滤完全一致；
  100 组单右行场景与 `row_equals` 的 NOT IN 转换一致。

## 项目结构

```
Cargo.toml            包配置（rust-version = 1.95.0，无依赖）
src/lib.rs            算子实现：TriBool / NotInIndex / ProbeReport / 朴素参照
tests/integration.rs  14 个集成测试（正常、边界、交错、计数、随机交叉验证）
```
