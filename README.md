# 页式 B+ 树索引（Rust 1.95.0，零第三方依赖）

一个纯 Rust 标准库实现的页式 B+ 树索引。索引项是“整数键 + 行号”的二元组，
按 `(key, row_id)` 全序排列，**同一键的不同记录可以跨多个叶页存放**。

## 结构约定

| 项目 | 约定 |
| --- | --- |
| 叶页容量 | 非根叶页 2..=4 项；根叶页可低于下限（包括空页） |
| 内部页容量 | 非根内部页 2..=4 个孩子；根内部页可只有 1 个孩子（仅出现在降高前瞬间） |
| 内部页分隔符 | 完整索引项 `Entry`，`separators[i]` 恒等于 `children[i+1]` 子树的最小索引项，其 `key` 即右孩子最小键；重复键跨页时相邻分隔符允许相等 |
| 叶链 | 叶页通过 `prev` / `next` 双向指针串联，头页 `prev == None`，尾页 `next == None` |
| 存储 | 进程内 `HashMap<PageId, Page>`，所有算法只通过页号取页，可替换为磁盘/缓冲池 |

### 为什么分隔符是完整二元组而不是只用键

题面要求“内部分隔键始终等于右孩子子树的最小**项**”。当同一键的记录跨叶页时，
若内部页只保存键，按完整二元组下降只能落到该键的最左叶页，删除落在其他叶页上的
同键记录会失败。保存完整二元组后：

- **按完整二元组下降**（删除、存在性判断）：进入第一个严格大于目标项的分隔符右侧
  的孩子，相等走左侧，可精确命中目标二元组所在叶页；
- **按键下降**（点查、区间扫描定位）：只比较分隔符的 `key`，分隔符键小于目标键
  时走右侧、相等时走左侧，得到该键的最左落点叶页，再沿叶链收集全部同键记录。

## 公开 API

全部定义在 `src/lib.rs`，无第三方依赖。

### 类型与常量

- `type Key = i64`、`type RowId = u64`、`type PageId = u32`
- `Entry { key, row_id }`、`Entry::new(key, row_id)`，派生全序（先键后行号）
- `Page::Leaf(LeafPage { entries, next, prev })` /
  `Page::Internal(InternalPage { separators, children })`，字段对外只读
- 常量 `MIN_LEAF_ENTRIES = 2`、`MAX_LEAF_ENTRIES = 4`、
  `MIN_CHILDREN = 2`、`MAX_CHILDREN = 4`

### `BPlusTree` 方法

| 方法 | 说明 |
| --- | --- |
| `new()` / `Default::default()` | 建空树（仅一个空根叶页） |
| `insert(Entry) -> bool` | 逐条插入；重复二元组返回 `false` 且不改树；叶/内部页溢出（5 项/5 孩子）即分裂并向上传播，必要时生成新根 |
| `delete(Entry) -> bool` | 按完整二元组删除；不存在返回 `false`。非根欠载先向同父相邻兄弟借位（先左后右），借不到才合并；合并向上传播；内部根只剩一个孩子时降高 |
| `contains(Entry) -> bool` | 完整二元组存在性判断（沿树精确下降） |
| `find(Key) -> Vec<RowId>` | 树定位到最左落点叶后沿叶链收集同键全部行号（升序） |
| `scan(start, end) -> Vec<Entry>` | 正向扫描左闭右开 `[start, end)`，结果按 `(key, row_id)` 升序 |
| `scan_rev(start, end) -> Vec<Entry>` | 反向扫描同一区间，结果降序，恒等于正向结果整体反转 |
| `locate_leaf(Key) -> PageId` | 按键下降到最左落点叶页号 |
| `locate_leaf_entry(Entry) -> PageId` | 按完整二元组精确下降到所在叶页号 |
| `leaf_head() -> PageId` / `leaf_chain() -> Vec<PageId>` | 只读叶链访问 |
| `get_page(PageId) -> Option<&Page>` / `pages()` | 只读页结构遍历 |
| `len()` / `is_empty()` / `height()` / `page_count()` / `root_id()` | 元数据 |

## 复现步骤

环境要求：Rust 1.95.0（仓库根目录 `rust-toolchain.toml` 已固定该通道），
不需要任何外部 crate，离线即可构建测试。

```bash
cargo test --offline
```

输出包含 3 个单元测试（`src/lib.rs` 内 `unit_tests`）与 13 个集成测试
（`tests/bptree.rs`），共 16 个测试。也可用 release 模式复跑：

```bash
cargo test --offline --release
```

## 测试场景与验证范围

所有断言都针对真实数据结构操作后的状态，没有任何预置/硬编码输出。
`tests/bptree.rs` 中的 `check_invariants` 在大量测试的**每一步操作后**全量校验：

1. 从根可达页集合 == 实际页集合（无悬挂引用、无孤儿页，合并后页被回收）；
2. 容量限制：非根叶页 2..=4 项、非根内部页 2..=4 孩子，根页豁免下限；
3. 内部页分隔符非递减（重复键可相等），且每个分隔符恰好等于右孩子子树最小索引项；
4. 相邻子树区间正确：左子树最大项 `<` 分隔符 `<=` 右子树最小项；
5. 叶页内有序、叶链 `prev/next` 双向闭合、叶链顺序与 DFS 叶序一致；
6. 叶链拼接结果全局有序且条数等于 `len()`。

### 正常场景

- `ordered_insert_and_range_scan`：逆序插入 20 项后叶链全量有序，区间扫描与
  过滤参考结果一致，反向扫描等于正向反转。
- `internal_node_borrow_during_cascading_delete`：三层树升序删除，迫使内部页
  之间借位/合并向上传播，剩余项经树下降与双向扫描仍全部可达。

### 边界场景

- `empty_and_degenerate_ranges`：空树、起点等于/大于终点、不相交区间、
  `i64::MIN/MAX` 边界。
- `root_may_stay_below_minimum`：根叶页 1 项与删空后仍合法（根豁免下限），
  清空后可继续插入。
- `extreme_key_boundaries_and_row_ids`：`i64::MIN/MAX` 与 `u64::MIN/MAX`
  组合、极端键跨页后的点查/叶链/双向扫描一致性。
- `split_capacity_limits_are_exact`：分裂后每个页严格不超过容量上限。
- `merges_shrink_height_back_to_one`：80 项随机洗牌顺序删空，树一路合并
  降高回单叶，页数最终为 1。
- `underflow_borrows_from_sibling_before_merging`：确定性构造
  “兄弟有富余（4 项）”场景验证只借位不合并（页数不变），与
  “兄弟都只有下限（2 项）”场景验证只能合并（页数恰好减 1）。
- `separator_tracks_right_subtree_minimum_after_deletes`：连续删除子树最小项，
  每次删除后校验分隔符仍等于右孩子最小项、区间扫描不丢项。

### 重复键专项（题面要求的一致性）

- `duplicate_keys_span_pages_and_partial_delete`：同一键 10 条记录确认跨
  ≥3 个叶页；连续删除其中一部分后，**沿树定位 + 叶链续扫**（`find`）与
  **纯叶链遍历**（测试内 `find_via_leaf_chain`）返回同一批行号；两端叶子的
  `prev == None` / `next == None` 与所有相邻叶的双向指针逐一核对；重复删除
  已删二元组为无效操作。

### 交错（随机）场景

- `interleaved_small_key_space_with_duplicates`：12 键 × 6 行号、3 万次
  插入/删除/区间扫描混合操作，与 `std::collections::BTreeSet` 参考模型
  逐操作对照，每步后跑全量不变量检查。
- `interleaved_delete_heavy_large_space`：200 键 × 3 行号、4 万次删除偏重
  操作，最终清空验证降高与回收，再重新插入验证树可复用。
- `interleaved_multiple_seeds_against_reference_model`：5 个固定随机种子、
  不同键/行号空间各 8 千次操作，每步对照参考模型并校验
  “树定位结果 == 叶链扫描结果”。

随机数由测试内固定种子的 LCG 生成，失败可按种子完全复现。

## 未覆盖/限制说明

- 页存储是进程内 `HashMap`，未实现磁盘 I/O、缓冲池与并发控制；接口按分页方式
  组织，替换存储层不需要改树算法。
- 未实现崩溃恢复/WAL、页的序列化格式与压缩。
- 本工作区不包含任何基准测试（benchmark）与演示二进制；功能正确性以
  `cargo test --offline` 的真实测试结果为准。
