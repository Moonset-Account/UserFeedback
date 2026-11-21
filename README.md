# relationindex — 关系查询的有序索引与双向游标分页

一个纯本地 Go 库，把关系型行集冻结成不可变快照，在快照上提供
「地区升序 → 分数降序 → 空分数置末 → 主键升序」的**全序**索引，
并用游标做前后双向翻页。没有任何网络、文件或外部依赖。

## 入口

| 位置 | 作用 |
| --- | --- |
| `store.go` | `Store`（可变写入端）、`Session`（快照读视图） |
| `index.go` | `Snapshot`（不可变有序索引）、`Page`、`Stats`、排序比较 |
| `cursor.go` | 游标编解码（base64url + uvarint 长度前缀） |
| `index_test.go` / `pagination_test.go` / `store_test.go` | 验收用例 |

主要 API：

```go
store := relationindex.NewStore()
_ = store.Upsert(relationindex.Row{ID: "a", Region: "eu", Score: relationindex.NewScore(3)})

session, _ := store.BeginSession()      // 冻结快照
page, _ := session.FirstPage(20)        // 首页，含 PrevCursor / NextCursor
next, _ := session.FetchAfter(page.NextCursor, 20)
prev, _ := session.FetchBefore(next.PrevCursor, 20) // 仍按展示顺序返回
prev.Stats.IndexEntries()               // 本次定位比较 + 访问的索引项数
```

- 游标为空时的语义：`FetchAfter("")` 从头开始，`FetchBefore("")` 从尾开始；
  `FirstPage` / `LastPage` 是它们的具名封装。
- 游标不透明、带快照 ID；跨快照或损坏的游标返回
  `ErrCursorSnapshotMismatch` / `ErrInvalidCursor`，不会被静默接受。
- `Page.HasPrev` / `HasNext` 表示两端是否还有行；空页（只可能出现在越界游标）
  不带行也不带游标。

## 排序与全序保证

1. `Region` 升序（字节序）。
2. 有分数的行排在无分数的行之前（同一地区内），即空分数在地区末尾。
3. 分数降序。
4. `ID` 升序（字节序）作为最终决定性 tie-break。

第 4 条让顺序成为全序，因此「地区与分数完全相同的行」可以安全地跨页，
既不重复也不丢失。

## 快照与写入隔离

`Store` 是可变的；`BeginSession` / `Snapshot` 会把当前行复制并排序成
不可变索引。之后对 `Store` 的 `Upsert` / `Delete` 只影响更晚开始的会话，
已有会话及其游标继续在本快照上解析。

## 定位成本

游标里编码的是边界行的完整排序键，服务端用二分查找 (`lowerBound` /
`upperBound`) 直接定位，不从头扫描。`Page.Stats` 给出：

- `Comparisons`：二分查找中比较的索引项数（随 log₂n 增长）；
- `Visited`：实际取出并返回的索引项数（随页长增长）；
- `IndexEntries()`：二者之和。

## 校验与拒绝

构建/写入时拒绝空主键、重复主键、`NaN`/`±Inf` 分数
（`ErrInvalidRow`、`ErrDuplicateID`）；翻页时拒绝非正页长
（`ErrInvalidPageSize`）。

## 验证

```sh
go test ./...
go test -race ./...
```

测试全部使用 Go 内置 `testing`，覆盖：

| 用例 | 覆盖点 |
| --- | --- |
| `TestOrderRegionScoreAndKey` | 地区/分数/主键排序的可接受结果 |
| `TestUnscoredRowsSortAfterScoredWithinRegion` | 空分数在地区末尾 |
| `TestNonFiniteAndEmptyRowsRejected` / `TestDuplicatePrimaryKeyRejected` | 应被拒绝的输入 |
| `TestPageSizeMustBePositive` / `TestCorruptCursorRejected` / `TestCursorFromOtherSnapshotRejected` | 应被拒绝的请求 |
| `TestReferenceOrderMatchesOverRandomData` | 与独立参考排序对拍 |
| `TestForwardAndBackwardWalkAgree` | 两种方向、各种页长都能还原同一顺序 |
| `TestTiedRowsStraddlePageBoundaries` | 同地区同分数行跨页，双向不重不漏 |
| `TestUnscoredRowsAtRegionEndAcrossPages` | 空分数跨页边界 |
| `TestPageEdgesAndCursors` | 首末页标志、边界游标往返 |
| `TestSnapshotIsolationFromLaterWrites` | 快照建立后的增删改只影响新会话 |
| `TestSnapshotCopiesInput` | 快照与调用方切片解耦 |
| `TestConcurrentUpsertsWithSessions` | 并发写入下的快照一致性 |
| `TestLargeSnapshotDirectLocate` | 10 万行中跳回末页前一页：访问量 O(log n + 页长)，页长增大时访问量随之增长，1k→100k 比较次数仅对数增长 |
| `TestDirectLocateFromArbitraryDepth` | 任意深度页面可由游标直接恢复且成本有界 |

`TestLargeSnapshotDirectLocate` 是性能边界的真实断言：它同时检查
`Visited == 页长`、`Comparisons` 落在 `⌈log₂n⌉+1` 之内，并验证
页长翻四倍时 `Visited` 也翻四倍，而比较次数不随之线性膨胀。
