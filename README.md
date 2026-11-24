# graphwalk — 惰性 Top-K 标签游走游标

纯 Go 1.20 标准库实现：在多个不可变、带版本的图页上，惰性枚举满足标签表达式的源点到目标点游走。模块没有第三方依赖。

## 复现步骤

环境要求与题面一致：

```bash
go version
# go version go1.20.2 darwin/arm64（或其它 GOOS/GOARCH）

go test -count=1 ./...
go test -race -count=1 ./...
```

只看穷举核对：

```bash
go test -count=1 -run TestExhaustiveSmallGraphsTopK -v
```

公开 API 的可执行示例在 `example_test.go`，会被 `go test ./...` 实际校验输出。

## 公开 API

### 表达式

- `ParseExpr(string) (Expr, error)`：支持隐式串联、`|` 选择、空串（`ε`、`()` 或 `ParseExpr("")`）、`*`、`+`、`?`、`{n}`、`{m,}`、`{m,n}`。
- `Lit(label)`、`Epsilon()`、`Seq(...Expr)`、`Alt(...Expr)`、`Star(Expr)`、`Repeat(Expr, min, max)`。
- `MatchLabels(Expr, []string) bool`。
- 多字符标签可写为 `"quoted label"`、`` `raw label` `` 或 `{braced label}`；普通未引用字符按单字符标签处理。

表达式先构造 Thompson NFA。一个边 ID 序列只对应一个 path 状态，其 NFA 状态是所有表达式分支的 epsilon-closure 并集，因此多条表达式分支不会产生重复游走。

### 图页与游标

- `Vertex`、`EdgeID`（`uint64`；`0` 保留为下界哨兵，真实边 ID 必须大于 0）、`Edge`。
- `PageRef{ID, Version}`、`PageInfo{ID, Version, From, MinLabel, MaxLabel, MinCost}`。
- `Catalog` 接口与 `NewCatalog(version, pages...)` 固定目录快照。
- `ReadPageFunc func(context.Context, PageRef) ([]Edge, error)`：只有该回调返回边明细。
- `MemoryReader` 是本地合成页存储，记录每个精确 `PageRef` 的打开次数，用于验证惰性。
- `Query{Source, Target, Expression, Expr, MaxHops}`；`MaxHops==0` 默认为 32，大于 32 报错。
- `NewCursor(query, k, catalog, reader)`。
- `(*Cursor) Next(ctx) (Walk, error)`：后续无结果时返回可 `errors.Is(err, graphwalk.ErrDone)` 识别的错误。
- `Results()`、`Rank()`、`K()`、`Proof()`。

排序键严格为：

```text
(total cost, edge count, edge IDs lexicographically)
```

边费非负；零费环由跳数上界终止。`Source == Target` 且表达式可空时，空游走是第一条结果。

### 惰性依据

对路径 `P` 和未读页 `Q`，放入优先队列的页守卫下界是：

```text
(P.cost + Q.MinCost, P.hops + 1, append(P.IDs, 0))
```

由于：

1. 后续边费非负；
2. 每扩展一条边，跳数增加 1；
3. 哨兵边 ID `0` 小于所有真实 ID；

所以该守卫键不大于此页可能产生的任何子路径或更远后代。全局优先队列在弹出一条可输出游走前，会先弹出所有键更小或同键竞争的页守卫；下界仍高于下一名的页不会调用读取回调。

`Proof()` / `RankCertificate` 维护：

- 每个已发名次后的全局 `FrontierLowerBound`；
- 每页是 `Fetched` 还是未取；
- 目录声明的 `DirectoryMinCost`；
- 已取页实际观察到的 `ObservedMinCost`；
- 每个待展开前缀对应的页守卫下界 `Guards` / `MinimumGuardBound`。

### 断点与固定版本续读

- `(*Cursor) ExportCheckpoint() Checkpoint`
- `(*Cursor) MarshalCheckpoint() ([]byte, error)`
- `RestoreCursor(JSON, RestoreOptions{Reader, Availability})`
- `RestoreCheckpoint(Checkpoint, RestoreOptions{...})`

断点包含：

- 查询、k、目录版本和每个 `PageInfo.Version`；
- 已发前缀；
- 优先队列中的待展开路径和未读页守卫；
- 已取不可变页的边明细缓存；
- 名次证明快照。

恢复出的游标只使用断点内的固定目录/页版本，不接收新目录。若读取回调返回包装的 `ErrPageVersionUnavailable`，`Next` 返回 `*StaleVersionError` 并列出不可用 `PageRef`。也可传入 `Availability func(PageRef) bool` 后调用 `ValidateVersions()`，在不读取页明细的情况下列出全部过期页。

### 目录换版与修订游标

- `DiffCatalogs(oldCatalog, newCatalog) ChangeSummary`，也可由调用方手工给出 `ChangeSummary`。
- `(*Cursor) DeriveRevision(ctx, RevisionInput{...}) (*RevisionCursor, error)`。
- `RevisionPlan` 给出：
  - `PrefixLength`：旧已发结果在新版中仍构成正确前缀的最大长度；
  - `Retractions`：需要撤回的旧名次和旧游走；
  - `Reissues`：从 `PrefixLength+1` 到新 Top-K/实际末尾的补发序列；
  - `NewResults`、版本号和页变更摘要。
- `RevisionCursor.NextEvent(ctx)` 依次产生 `retract`、`reissue`、`complete` 事件。
- `AckRetraction(rank)` 确认下一个连续撤回名次；`AckRetractionsThrough(rank)` 累计确认边界。
- `MarshalCheckpoint()` / `RestoreRevisionCursor(JSON)` 持久化并恢复撤回确认边界。已确认名次不会再次进入待撤回序列。

派生游标把旧游标已取的相同 `PageRef` 作为不可变缓存：读取回调不会重开这些页。未取且下界仍高于新 Top-K 名次的页继续保持未读。旧游标不被派生过程修改，仍可继续读取旧页版本。

## 最小示例

```go
reader := graphwalk.NewMemoryReader()
page := graphwalk.PageInfo{
    ID: "p", Version: "v1", From: "s",
    MinLabel: "a", MaxLabel: "a", MinCost: 0,
}
reader.Put(page, []graphwalk.Edge{
    {ID: 1, From: "s", To: "s", Label: "a", Cost: 0},
})
catalog := graphwalk.NewCatalog("catalog-v1", page)
expr, _ := graphwalk.ParseExpr("a*")
cursor, _ := graphwalk.NewCursor(graphwalk.Query{
    Source: "s", Target: "s", Expr: expr, MaxHops: 32,
}, 10, catalog, reader.ReadPage)

w, err := cursor.Next(context.Background()) // 空游走
```

## 验证范围

当前测试包含并实际通过：

- **正常路径**：40 个确定性随机小图；测试内 DFS 穷举所有不超过 5 跳的边序列，独立表达式 oracle 过滤后排序，逐项核对游标前 k 项。
- **排序/去重**：费用、跳数、边 ID 字典序；同一 `a | (a){1,1} | a*` 歧义串只返回一次。
- **边界**：空匹配、源等于目标、零费多环、同跳边 ID 排序、2 跳边界、32 跳上限校验、空结果/`ErrDone`。
- **惰性**：昂贵隔离页的下界远高于第 k 项时，读取回调打开次数为 0；放大 k 后该页恰好打开一次并产出正确结果。
- **故障恢复**：在第 0 到第 4 个前缀切点分别 JSON 断电/恢复，比较完整前 4 项；过期 `v1` 页返回并指出不可用 `PageRef`。
- **修订交错**：覆盖新版插入零费便宜边导致前缀长度为 0，以及新版删除曾入选边后前 2 项仍为正确前缀；验证撤回/补发名次、ack 后断电恢复、已确认撤回不重复。
- **页依据/不重扫**：断言未取高费页的 `PageBound` 不低于第 k 项；未变已取页在派生修订时读取回调计数不增加，变更页的新版本只打开一次。
- **并发交错**：多个 goroutine 同时调用 `Next` 时结果不重复；`go test -race` 通过。
- 覆盖率摘要可用 `go test -cover ./...` 复现。

未执行/不适用的范围：没有访问真实网络、真实图数据库或系统账户；`MemoryReader` 只使用本地合成数据。32 跳全量穷举在稠密图上是指数级，因此穷举核对按题面“有限小图 + 边数上界”使用 5 跳；32 跳本身由配置校验和有界搜索保证。
