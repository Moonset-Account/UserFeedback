# warmgrid

内存道路空间索引（Go 1.20.2，纯标准库，无外部依赖）。

把整数坐标平面按**半开放格**（half-open grid）划分，几何（折线/路线）只以**主记录**存一份；
跨格几何在每个被覆盖的格子里放一条轻量索引项（`record id + version`）。查询先按包围盒收集候选格、
按 `(id, version)` 去重，再**回表**取主记录做精确包围盒与边界关系过滤。

快照采用不可变 + 写时复制的发布模型：一次局部改绘只重建旧/新覆盖格的并集上的格子桶，
其余桶与上一个快照共享。

## 公开 API

### 网格

```go
grid, err := warmgrid.NewGrid(10)          // 格子边长，1..MaxCoord
cell := grid.CellOf(warmgrid.Point{X: 10, Y: 0}) // => CellKey{1,0}（边界点归正方向格）
cells, err := grid.CellsForBox(bbox)       // 闭盒覆盖的全部格子（行主序）
box, err := grid.CellBounds(cell)          // 格子对应的半开矩形
warmgrid.FormatCells(cells)                // 确定性字符串，便于诊断
```

常量与错误：`MaxCoord`、`MaxCellsPerObject`、`ErrInvalidCellSize`、`ErrInvalidBBox`、
`ErrInvalidGeometry`、`ErrTooManyCells`、`ErrEmptyID`、`ErrExists`、`ErrNotFound`、
`ErrInvalidFilter`、`ErrDuplicateChange`（均可用 `errors.Is` 判断）。

### 几何与关系

```go
p := warmgrid.Polyline{Points: []warmgrid.Point{{X:0,Y:0},{X:10,Y:0}}}
b := p.Bounds()
rel := warmgrid.PolylineRelation(p, box)   // Disjoint / Touches / Crosses / Inside
ok := p.Intersects(other)                  // 闭集是否有公共点（共端点算相交）
ok = p.Crosses(other)                      // 是否共享内部几何（穿越/T 形/重叠）；共端点路口不算
d := p.DistanceToPolyline(point)
```

`SegmentContactKind` 精确区分 `ContactNone / ContactEndpoint / ContactCross`。

### 索引

```go
ix := warmgrid.NewIndex(grid)
v, err := ix.Insert("road-1", geom)        // 新 id；已存在返回 ErrExists
v, err = ix.Update("road-1", newGeom)      // 局部改绘；未知 id 返回 ErrNotFound
err = ix.Delete("road-1")
seq, err := ix.Apply(                      // 原子批量改绘，全部生效或全部不生效
    warmgrid.Change{Kind: warmgrid.ChangeUpsert, ID: "road-1", Geom: g1},
    warmgrid.Change{Kind: warmgrid.ChangeDelete, ID: "road-2"},
)
```

`ChangeUpsert` 对已存在 id 等价于局部改绘（upsert）。

快照与查询：

```go
snap := ix.Snapshot()                      // 不可变读视图，之后写入不影响它
results, err := snap.Query(warmgrid.Query{
    BBox:   warmgrid.BBox{MinX: 0, MinY: 0, MaxX: 40, MaxY: 40},
    Filter: warmgrid.FilterInside,         // Any / Intersects / Inside / Touches / Crosses
})
// Result{ID, Version, Geom, Bounds, Relation}
rec, ok := snap.Record("road-1")
all := snap.All()                          // 按 ID 排序的全量记录（诊断/线性扫描基准）
```

诊断：`ix.Stats()`（`Records`、`CellTerms`、`LiveCells`、`Seq`）、`ix.CellTerms(cell)`。

## 边界语义（重点）

- **半开放格**：格 `(x,y)` 覆盖 `[x*S,(x+1)*S) × [y*S,(y+1)*S)`。坐标恰在格线上时归**正方向**格：
  尺寸 10 时 `(10,10)` 属于格 `(1,1)`。
- **闭合查询盒**：`BBox` 四条边都属于它。因此 `[0,10]×[0,10]` 覆盖角点周围四个格子；
  尺寸 10 下盒子覆盖的格子集合 = 盒内所有格点 `CellOf` 的并集（测试逐个格点验证）。
- **沿界线的几何**：恰好压在格线上的路线被索引到正方向格，且只被“够得着该线”的查询命中；
  差一格（`MaxX: 9`）的查询看不到它。
- **关系判定与网格一致**：点/顶点/线段落在盒边上算“在内”，所以整条压在盒边上的路线是
  `Inside`；有部分严格在盒内、部分在外是 `Crosses`；只与盒边相接是 `Touches`。
  关系判定使用整数精确谓词 + `math/big.Rat` 精确参数裁剪，无浮点误判。

## 版本、去重与快照隔离

- 每条记录的**主记录只存一份**；跨 N 格只产生 N 条 `(id, version)` 索引项。
- 查询把所有覆盖格里的候选按 `(id, version)` 去重后**只回表一次**；陈旧 version 的 term 会被
  “记录当前 version 不一致”这一判断丢弃，绝不产生同一记录的第二个结果。
- 改绘后：**旧快照**仍按旧 version 在旧多格候选中去重，看到旧几何；**新快照**立刻看到新的覆盖格。
  一次查询始终只读一个不可变快照对象，因此**不会混读两个版本**。
- 局部性：`Update`/`Apply` 只重建旧∪新覆盖格上的桶，其余桶与新快照共享（测试用桶地址验证）。

## 运行

```bash
go test ./...          # 全部测试
go test -race ./...    # 竞态检测（并发写 + 快照读）
go test -cover ./...   # 覆盖率
go vet ./...           # 静态检查（如环境允许）
```

模块声明在 `go.mod`（`go 1.20`）。

## 验证范围（均基于本地合成数据实测）

| 测试文件 | 覆盖内容 |
| --- | --- |
| `grid_test.go` | 半开放边界 `CellOf`、`CellsForBox` 恰在格线时取正方向格、格子集合=格点 `CellOf` 并集、参数校验、超大盒拒绝 |
| `geometry_test.go` | 精确线段相交（真穿越/共端点/共线重叠/近平行未命中/退化点）、四类边界关系、点查询、点到线段距离 |
| `contact_test.go` | 路口（共端点）与冲突（穿越/T 形/共线重叠）的区分，双向一致 |
| `index_test.go` | 多格几何只出一条结果、`Inside/Touches/Crosses` 过滤、插入/删除错误与状态不变、**改绘后新旧快照各自只读到一个版本**、多次改绘只出一条、`Apply` 原子性与失败回滚、删除清空所有格项、结果拷贝隔离、并发写 + 快照读（`-race`） |
| `filter_test.go` | `FilterAny` 与精确 `FilterIntersects` 的假阳性差异、超多格拒绝、查询参数校验、version/seq 单调、混合批量 `Apply` 的旧/新快照对比 |
| `redraw_test.go` | 未触碰格桶与旧快照共享（写时复制）、空桶/新桶被重建、注入陈旧 term 后仍只返回当前版本一条 |
| `scenario_test.go` | **同一包围盒内多条候选路线选取不相交集合**（贪心，含 T 形与共角点语义）、端点恰在格线可被两侧查询命中、跨格线路线两格索引不重复、负坐标跨原点、无网格参与的线性扫描差分测试（随机 120 条 + 200 次随机查询 × 5 种过滤器） |

当前实测：`go test ./...` 通过；`go test -race ./...` 通过；语句覆盖率约 92%。

## 未覆盖 / 说明

- 仅做内存索引，不含持久化、并发多写者事务冲突检测（写者是单锁串行）、地理坐标投影。
- 精确关系判定对每条候选线段使用 `big.Rat` 裁剪，候选很多时偏慢；这是为精确性做的取舍，
  已用包围盒做前置剪枝。
- 坐标为整数；折线不做自相交检查，也不强制简单折线。
