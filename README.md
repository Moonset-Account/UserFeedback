# bitmap-interval-filter

按压缩位图区间筛选列批次的算子（Rust 1.95.0，仅标准库，无第三方依赖）。

## 功能

- 输入为按全局行号编号的**半开区间** `[start, end)`，分三类记录：可见行（visibility）、
  谓词为真（`Kind::True`）、谓词未知（`Kind::Unknown`）。
- 各列以**分批更正**的方式到达，批次之间的分段边界无需对齐；同一行若同时被标记为真和
  未知，按**未知（null）优先**处理；缺席的真/未知位一律视为假。
- 表达式支持 `AND` / `OR` / `NOT`，按三值逻辑求值：
  - `AND`：任一操作数为假则假，全真则真，否则未知；
  - `OR`：任一操作数为真则真，全假则假，否则未知；
  - `NOT`：**只在有效可见行内取补**（可见行中非真非未知的行变真），未知保持不变。
- 最终只输出**为真的最大连续区间**（相邻段自动合并），并提供：
  - `count()`：为真行数，由段长求和得到，不按行展开；
  - `page(cursor, limit)`：可续取的页，游标为行号，落在区间内部时返回该区间尾部，
    页与页之间不重叠、不丢行。
- **版本固定（快照）**：`Query::open` 固定当前输入版本；之后到达的迟到更正不会增删
  该查询已经返回的行。新打开的查询才看到更正。可用 `Query::open_at` 固定到任意历史版本。
- **压缩复杂度**：内存与遍历工作量正比于压缩段数，与行跨度无关；十亿行跨度按段处理，
  不做逐行展开。

## 公开 API

| 类型 / 函数 | 说明 |
| --- | --- |
| `Interval { start, end }` | 半开行区间，`Interval::new(start, end)` |
| `IntervalSet` | 规范化（排序、不相交、合并相邻）的区间集；`union` / `intersect` / `subtract` / `complement_within` / `row_count` / `contains` |
| `Store` | 版本化输入存储；`correct_visibility(iv, insert)`、`correct_column(col, kind, iv, insert)` 各返回新版本号，`current_version()` 查询当前版本 |
| `Kind` | `True` / `Unknown` |
| `Expr` | `Expr::column(id)`、`Expr::and(a,b)`、`Expr::or(a,b)`、`Expr::not(e)` |
| `Query` | `Query::open(&store, &expr)` / `open_at(&store, &expr, version)`；`count()`、`intervals()`、`page(cursor, limit)`、`pinned_version()` |
| `Page` | `intervals` + `next_cursor`（`None` 表示结果耗尽） |

### 最小示例

```rust
use bitmap_interval_filter::{Expr, Interval, Kind, Query, Store};

let mut store = Store::new();
store.correct_visibility(Interval::new(0, 1_000_000_000), true);
store.correct_column(1, Kind::True, Interval::new(0, 500_000_000), true);
store.correct_column(1, Kind::Unknown, Interval::new(100, 200), true);

let q = Query::open(&store, &Expr::not(Expr::column(1)));
assert_eq!(q.count(), 500_000_000);
let page = q.page(None, 8);          // 第一页，最多 8 段
if let Some(cursor) = page.next_cursor {
    let _next = q.page(Some(cursor), 8); // 续取
}
```

## 复现步骤

```bash
cargo test --offline
```

工具链：rustc / cargo 1.95.0。无第三方依赖，无需网络。

## 验证范围（`tests/operator_tests.rs`，共 9 个测试，全部已实际执行）

- `visibility_holes_limit_not_and_output`：可见性有空洞时，`NOT` 只在可见行内取补、
  未知被保留（含 `NOT(NOT A) == A` 与未知带不渗入合取的检查）。
- `absent_bits_mean_false`：缺席列/缺席位按假处理（`AND` 为空、`OR` 恒等于已有列）。
- `three_valued_and_or_overlapping_unknown`：未知与真交叠时的三值 `AND`/`OR` 精确区间。
- `unknown_split_across_batches_with_misaligned_boundaries`：未知区间跨三个批次、真区间
  跨两个批次且边界互不对齐，对照逐行参考模型验证。
- `late_correction_on_page_boundary_does_not_change_open_query`：结果段为 `[0,10)` 与
  `[12,20)`，第一页游标恰好落在行 10；迟到的删除/插入更正落在该页边界上——已打开查询
  的第二页与计数不变，新查询才反映更正（验收场景）。
- `visibility_correction_hole_punched_after_open`：查询打开后可见性被挖出空洞，旧查询
  不变、新查询出现空洞。
- `billion_row_span_stays_compressed`：十亿行跨度上的 `OR`/`NOT`、计数与翻页，结果段数
  保持为个位数（不按行展开）。
- `page_cursor_inside_gap_and_at_end`：游标落在空洞内与结果末尾的分页行为。
- `randomized_differential_against_row_model`：确定性 LCG 生成 200 组随机更正
  （可见性空洞、分批/错位段、插入与删除、真/未知混合）与随机表达式树，与逐行展开的
  参考模型对照区间、计数及多种页大小下的完整翻页序列；并验证每组迟到更正不影响
  已打开查询。

参考模型（测试内 `Model`）把同样的更正展开为逐行状态再按三值逻辑求值，是压缩实现的
独立对照；所有断言均来自真实测试执行，无预置输出。
