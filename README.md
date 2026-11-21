# row-versioned-table

一个零依赖的表内核，用来演示 **行编码版本 + 物理字段槽** 下的即时加列、删列重建、
默认值演进和按主键整理单行。

* Rust 1.95.0，`edition = 2021`
* 仅使用标准库，无第三方依赖
* 数据全部为内存中的本地合成数据，不访问网络、账户或系统资源

## 核心模型

表把列定义和行数据分开保存：

| 概念 | 说明 |
| --- | --- |
| `ColumnId` | 列的稳定内部身份，只增不减，删列后不回收 |
| `slot` | 列的物理槽位，单调分配，**永不复用** |
| `version` | 行写入时的布局版本，由加列/删列推进 |
| `raw_slots` | 行真正持有的物理槽；数组可以短于当前布局 |

读取一列时：

1. 若行的 `raw_slots` **有**该列槽位：`Some(v)` 是存储值，`None` 是显式 SQL NULL；
2. 若行的 `raw_slots` **没有**该槽位（列是后来加的）：读取**加列那一刻冻结的默认值**
   （`added_default`），而不是当前的插入默认值。

因此：

* **加列**只改元数据，不重写任何旧行；旧行原物理布局原样保留。
* **改插入默认值**只影响之后省略该列的插入，不影响已存在的任何行，也不推进布局版本。
* **插入**按当前布局写满所有存活列。省略某列才用“插入时”的默认值；显式传入 `None`
  会存成真正的 NULL，永远不会被默认值替换。
* **删列**只是把列标记为退役并移除名字映射。旧行的旧槽位数据仍在，但该槽位和
  `ColumnId` 都已退役；同名重建会拿到**新的 `ColumnId` 和新的槽位**，旧物理值不可能
  顶替新列。
* **整理单行**（`compact_row`）只把目标行重写为当前布局：所有存活列的值物化到各自槽位，
  退役槽位清空。其他行既不被读取也不被修改；整理前后逐列查询结果相等。

## 公开 API

```text
Table::new() -> Table
Table::add_column(name, added_default: Option<i64>) -> Result<ColumnId, TableError>
Table::drop_column(name) -> Result<ColumnId, TableError>
Table::set_insert_default(name, Option<i64>) -> Result<Option<i64>, TableError>
Table::insert(key: i64, &[(&str, Option<i64>)]) -> Result<(), TableError>
Table::insert_defaults(key) -> Result<(), TableError>
Table::get_row(key) -> Option<RowView<'_>>
Table::value(key, name) -> Result<Option<Option<i64>>, TableError>
Table::row_values(key) -> Option<Vec<(&str, Option<i64>)>>
Table::raw_slots(key) -> Option<&[Option<i64>]>
Table::encoded_version(key) -> Option<u64>
Table::compact_row(key) -> Result<u64, TableError>

Table::schema_version() / slot_count() / row_count() / keys()
Table::columns() / all_columns() / column(name)
```

`RowView` 暴露 `key()`、`encoded_version()`、`raw_slots()`、`value(name)`、`values()`。

语义要点：

* `Table::value` 返回 `Result<Option<Option<i64>>>`：
  外层 `None` 表示行不存在，内层 `None` 表示该列值为 NULL；
  列名未知或已删返回 `TableError::UnknownColumn`。
* `RowView::value` 返回 `Result<Option<i64>, TableError>`（行必然存在）。
* `schema_version` 只在加列/删列时递增；`set_insert_default` 不递增。
* 插入采用先校验后写入：未知列、重复主键、同一列重复传值都会返回错误且不改变表。

错误类型 `TableError`：

```text
DuplicateColumn(String)   同名存活列已存在
UnknownColumn(String)     列名未知或已删除
DuplicateKey(i64)         主键已存在
UnknownKey(i64)           主键不存在
DuplicateValue(String)    一次插入中同一列出现多次
```

## 复现步骤

```sh
# 工作区为独立空目录；确保使用 Rust 1.95.0
rustc --version   # rustc 1.95.0
cargo --version   # cargo 1.95.0

cargo test --offline
```

不需要网络，不需要安装任何依赖。

## 验证范围

`tests/table.rs` 共 17 个集成测试，全部通过真实调用公开 API 验证，未使用任何预置
输出或伪造结果。

**正常场景**

* `added_column_does_not_rewrite_existing_rows` — 加列前后旧行原始槽位与编码版本不变，
  旧行读到加列默认值。
* `old_row_keeps_add_time_default_after_insert_default_changes` — 改插入默认值后旧行仍
  读加列默认值、新插入读插入默认值、显式 NULL 保留。
* `insert_omitted_vs_explicit_null_are_distinct` — 省略列与显式 NULL 结果不同。
* `explicit_null_from_before_a_later_add_is_preserved` — 后续加列不会覆盖更早的显式 NULL。
* `multi_column_round_trip_matches_insert` — `i64::MIN/MAX` 与 NULL 多列往返一致。

**边界场景**

* `empty_table_queries_are_safe` — 空表查询、越界主键、整理不存在的行。
* `duplicate_and_unknown_operations_are_rejected` — 重名列、删/改未知列、重复主键、
  插入未知列、同列重复传值；失败后表状态不变。
* `default_of_null_is_frozen_too` — 加列默认值为 NULL 也会被冻结。
* `explicit_null_survives_insert_default_change_and_compaction` — 显式 NULL 跨默认值修改
  与整理仍然存在。
* `metadata_reports_live_and_retired_columns` — 存活/退役列、槽位单调、身份不回收。
* `row_view_exposes_version_and_raw_slots` — 行视图的编码版本与原始槽位。

**交错场景**

* `dropped_column_physical_value_never_feeds_same_named_replacement` — 删列同名重建后，
  旧物理值 42 不会顶替新列默认值，整理后依然如此。
* `dropped_column_rows_inserted_while_absent_use_fresh_schema` — 删列期间插入的行使用
  当前 schema，退役槽位保持 NULL。
* `compacting_one_row_only_changes_that_row` — 只整理一行：目标行版本与槽位变为当前布局，
  另一行原始槽位、版本、逐列值全部不变；目标行整理前后逐列相等。
* `compaction_preserves_explicit_null_and_is_idempotent` — 整理保留显式 NULL，同一版本
  下重复整理幂等。
* `interleaved_schema_changes_and_targeted_compactions` — 加列、改默认、再整理、再改默认、
  再加列交错进行，新旧行分别读到正确默认值，整理前后逐列相等。
* `compaction_uses_add_time_default_even_after_insert_default_changes` — 已物化行整理时
  不会被后改的插入默认值污染。

### 明确未执行的部分

* `cargo clippy` 与 `cargo fmt --check` 在本环境需要额外授权，**均未执行**；上面的结论
  只基于 `cargo test --offline`。
* 未做持久化/磁盘存储、并发、事务测试；该内核仅覆盖题面要求的内存内编码语义。
* `src/lib.rs` 中的单元测试计数为 0，全部验证位于 `tests/table.rs`。
