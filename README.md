# idmapaudit — 命名空间用户编号映射只读核验模块

容器管理器挂载共享卷前，核验申请的一段用户编号（UID/GID）能否经过多层命名空间
平移映射，最终落到批准的主机账号范围内。模块只做核验，不修改系统映射、不产生
任何外部副作用。

## 入口与代码结构

| 文件 | 作用 |
| --- | --- |
| `go.mod` | 模块名 `idmapaudit`，`go 1.20`（本机工具链 go1.20.2 darwin/arm64） |
| `idmap/idmap.go` | 核验模块实现（包 `idmap`，无第三方依赖） |
| `idmap/idmap_test.go` | Go 内置 `testing` 验收用例与 benchmark（外部测试包 `idmap_test`） |

实际 API 入口：

- `idmap.NewVerifier(cfg idmap.Config) (*idmap.Verifier, error)`
  构造并校验只读核验器；内部保留输入切片的规范化副本，不保留调用方切片。
- `(*idmap.Verifier).Verify(request idmap.Range) (idmap.Result, error)`
  核验一个半开区间 `[Start, End)`，返回三类**原始坐标**子区间：
  - `Result.Confirmed`：可确认，最终主机编号落在批准区间内；
  - `Result.OutOfBounds`：越出批准边界（能到主机，但不在批准账号范围）；
  - `Result.MissingMapping`：缺少映射，在某层落入空洞；空洞不继承父层同号身份。
  三个桶两两不交、各自排序并合并相邻段，其并集恰为请求区间。
- 哨兵错误 `idmap.ErrOverlappingSegments`、`idmap.ErrInvalidRange`，可用
  `errors.Is` 判别。

## 建模约定（与题面逐项对应）

1. **标识符域**：Linux `uid_t`/`gid_t` 的 uint32 域 `[0, 2^32-1]`；区间统一为
   半开 `[Start, End)`，`End` 最大可取 `2^32`。越域或零长度段直接报
   `ErrInvalidRange`。
2. **每层命名空间**用若干 `Segment{SrcStart, DstStart, Length}` 描述到父层的
   恒偏移平移；层按“最内层 → 主机”的顺序排列，请求从最内层坐标进入。
3. **空洞不继承**：某层未被任何段覆盖的编号立即进入 `MissingMapping`，即使上
   层曾给过它合法身份（见 `TestHoleDoesNotInheritParentIdentity`）。
4. **配置错误**：同一层源段重叠、或平移后目标段重叠均报
   `ErrOverlappingSegments`；仅相接（半开不重叠）允许。构造时分别按源起点、
   目标起点排序做两次不交校验。
5. **只读**：`NewVerifier` 深拷贝输入，`Verify` 不改动任何入参；`Verifier`
   无内部可变状态，`go test -race` 通过，可并发调用。

## 复杂度

设 S 为映射段总数、K 为切分点数量。每层用 `sort.Search` 定位首个相关段后线性
扫描，跨层产生的片段数只随段数增长；桶结果再做一次 `O((S+K) log(S+K))` 的
排序合并。**不遍历任何单个编号**，因此横跨 40 亿编号的请求与横跨 10 个编号的
请求开销同量级。

实测（本机）：

```
BenchmarkVerifyBillionSpan-10   ~628 ns/op   1064 B/op   24 allocs/op
```

## 验证范围与用例

运行方式：

```
go test ./...            # 全部验收用例（真实断言，无占位）
go test -short ./...     # 跳过十亿跨度性能断言
go test -race ./...      # 竞态检测
go test -bench=. ./idmap # 十亿跨度 benchmark
```

用例对题面交叉约束的覆盖：

| 约束 | 用例 | 性质 |
| --- | --- | --- |
| 可接受结果 | `TestSingleLayerAllThreeOutcomes` 全确认请求；`TestApprovedBoundaryHalfOpen` 精确批准切片 | 应通过 |
| 应被拒绝/阻止 | 源重叠 `TestNewRejectsOverlappingSource`、目标重叠 `TestNewRejectsOverlappingDestination`、零长度/越域 `TestNewRejectsInvalidRanges`、非法请求 `TestVerifyInvalidRequest`、越界桶 `TestNoLayersApprovedRangeStillApplies` | 拒绝或进 OOB 桶 |
| 边界场景 | 半开批准边界双侧跨切 `TestApprovedBoundaryHalfOpen`；uint32 端点 `MaxID`/`MaxID+1`；段仅相接允许 | 边界 |
| 交错场景 | 确认/越界/空洞在一个请求内交替出现并按原始坐标返回；多层双向偏移；空洞切断父级身份；相邻同类片段合并 | 交错 |
| 只读保证 | `TestVerifierDoesNotMutateInputs` 深拷贝快照对比；乱序输入与等价有序输入结果一致 | 只读 |
| 规模约束 | `TestBillionSpanIsSegmentSizedWork`：4×10⁹ 跨度、3 层 8 段合成配置，断言子区间数量上限（≤12）与耗时上限（200ms），并用独立的逐编号参考实现对 200 个采样点交叉核验三类归属 | 性能+交叉核验 |

`TestBillionSpanIsSegmentSizedWork` 中的参考实现 `translateOneID` 与被测代码
独立（逐段线性查找、逐编号平移），用于交叉验证区间算法的分类结果。
