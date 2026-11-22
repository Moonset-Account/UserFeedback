# temporaljoin — 键控时态流连接算子（Go 1.20.2，仅标准库）

把左侧事件与右侧**同键、在该事件时刻已生效的最新版本**连接的流算子。
所有时间均为 `int64` 逻辑事件时间，不依赖任何外部依赖，仅使用 Go 标准库。

## 一、语义约定

| 规则 | 实现方式 |
| --- | --- |
| 左事件连接哪个右版本 | 同键、`EffectiveTime <= 左事件时间` 的**活跃**版本中，生效时间最大者；生效时间相同取 `Revision` 最大；再相同取后到达者（内部到达序号）。已撤回版本不参与匹配。 |
| 左事件先到先发结果 | `PushLeft` 立即发出一条 `ResultAdd`，无活跃版本时配对为空（`Pairing.Right == nil`）。 |
| 右侧变化影响既有匹配 | `PushRight` 对同键所有未封口左事件重算：配对变化时先发 `ResultRetract`（按左事件 ID 撤回旧配对，空配对也会被撤回），再发替代 `ResultAdd`。 |
| 右侧迟到插入/撤回 | 迟到判定以**该版本的生效时间**对右侧水位严格比较（`eff < rightWatermark`）；撤回时若版本已知，用其存储的生效时间判定。迟到记录（新增与撤回同规则）进入迟到侧输出，不改任何已封口或未封口结果。 |
| 左侧迟到 | 左事件时间 `< leftWatermark` 同样进入迟到侧输出，不产生 join 结果。 |
| 水位边界 | 时间 **等于**水位不算迟到、不封口（严格小于语义），由 `TestWatermarkBoundaryIsStrict` 锁定。 |
| 封口条件 | 仅当**两个水位都严格越过**某左事件时间（`t < leftWm && t < rightWm`）时封口；单路水位越过不封口。封口结果不可变，迟到记录不能再改它。 |
| 同生效时间修订 | 同生效时间高修订号胜出；低修订号后到不改变结果；撤回胜者后自动回退到下一活跃版本。 |
| 历史删除（GC） | 封口周期触发，规则见下节；`Finish` 后所有右历史均可删除。 |

### GC 可删除的三类右版本

1. `retracted`：已撤回的非活跃版本，永不可能再匹配（同版本 ID 准时重新 upsert 会作为新条目重建）。
2. `dominated`：被同键另一个（生效时间, 修订号）严格更晚的活跃版本压制，且两个水位都已越过它的生效时间、且不存在还能选中它的未封口左事件。未来准时修订的生效时间必然 `>= 当前水位 > 压制者生效时间 > 它的生效时间`，因此它不可能再相关。
3. `no-pending-left`：仅在 `Finish` 的终态水位下，某键已无未封口左事件时，删除该键全部残留版本（开放流中不启用，避免左事件只是尚未到达）。

GC 按 key、version ID 排序执行，输出顺序确定，不受 Go map 随机遍历影响。

## 二、公开 API（包 `temporaljoin`）

```go
// 构造
func NewJoiner() *Joiner

// 输入
func (j *Joiner) PushLeft(ev LeftEvent)
func (j *Joiner) PushRight(rec RightRecord)            // RightUpsert / RightRetract
func (j *Joiner) AdvanceLeftWatermark(wm int64)        // 单调推进，触发封口+GC
func (j *Joiner) AdvanceRightWatermark(wm int64)
func (j *Joiner) Finish()                              // 两路水位推到 MaxInt64，全封口+全GC

// 输出（均返回深拷贝）
func (j *Joiner) Results() []Result                    // retract/add 结果流
func (j *Joiner) Sealed() []Sealed                     // 已封口配对（封口顺序）
func (j *Joiner) Late() []LateRecord                   // 迟到侧输出（Source: "left"/"right"）
func (j *Joiner) GCLog() []GCEntry                     // 历史删除记录（含原因）

// 状态观测（测试/运维用）
func (j *Joiner) LeftWatermark() int64
func (j *Joiner) RightWatermark() int64
func (j *Joiner) PendingLeftIDs() []string
func (j *Joiner) RetainedRightVersions(key string) []string

// 故障恢复：JSON 检查点（深拷贝、可序列化）
func (j *Joiner) Snapshot() ([]byte, error)
func (j *Joiner) Restore(data []byte) error
```

关键数据类型：

- `LeftEvent{ID, Key string; Time int64; Payload string}`
- `RightRecord{VersionID, Key string; EffectiveTime, Revision int64; Op RightOp; Value string}`
- `Result{Kind ResultKind; Pair Pairing}`，`Kind` 为 `ResultAdd` / `ResultRetract`
- `Pairing{LeftID, Key string; LeftT int64; Right *RightRecord}`，`Right == nil` 表示空配对
- 所有公共方法经内部 `sync.Mutex` 串行化，可并发调用

## 三、复现步骤

```bash
go version            # 要求 go1.20.2（本工作区实测 go1.20.2 darwin/arm64）
go test ./...         # 全部功能测试
go test -race ./...   # 竞态检测
go test -count=5 ./...# 重复执行验证确定性（GC/封口顺序）
gofmt -l .            # 无输出表示格式合规
```

## 四、验证范围（均为真实算子输出上的断言，无预置演示输出）

| 测试 | 覆盖的题面场景 |
| --- | --- |
| `TestMainScenarioCoverInsertRetractSealAndGC` | **题面指定主序列**：同一右版本 R1 覆盖 L1/L2/L3 → 迟到但合法地插入中间时刻 R2（只改 t≥14 的事件，逐对 retract+add）→ 按版本 ID 撤回 R1（L1 变空配对）→ 单路水位不封口 → 双水位越过才封口 → 准时撤回/重插、迟到新增/迟到撤回全部单列且不改封口结果 → 封口后可删除的历史（R1 `retracted`、R2 `no-pending-left`）逐步核对 |
| `TestEmptyPairingWhenNoActiveVersion` | 边界：无版本时空配对；未来生效版本不回填；新有效版本撤回空配对并替代 |
| `TestWatermarkBoundaryIsStrict` | 边界：事件时间/生效时间**等于**水位不迟到、不封口；越过一格才封口 |
| `TestSameEffectiveTimeRevisionWinsAndLateSideOutput` | 同生效时间按修订号选最新；低修订号后到无效；撤回胜者后回退；封口后迟到高修订与迟到撤回均不能改封口结果 |
| `TestGarbageCollectionDominatedVersions` | 被压制旧版本在双水位越过且无待处理左事件可选中时中途 GC；仍需服务后续左事件的版本保留；被 GC 版本的迟到重插进迟到侧 |
| `TestSnapshotRestoreRecovery` | 故障恢复：检查点 → 新算子恢复 → 续播，结果流/封口/迟到侧/GC 日志与未故障实例逐字节一致；并验证恢复出的右状态能驱动真实重算（R2→R3 切换） |
| `TestConcurrentInterleaving` | 并发交错：24 个键在独立 goroutine 推送、后台 goroutine 持续快照，`-race` 下无竞态；水位确定性推进后逐键与串行参考比对结果、封口与迟到计数 |
| `TestReupsertRetractedVersion` | 撤回后同版本 ID 准时重插重新激活并恢复配对；`Finish` 后无残留 |

实测命令与结果：

- `go test -v ./...`：8 个测试全部 PASS
- `go test -race -count=3 ./...`：PASS（无数据竞争）
- `go test -count=5 ./...`：PASS（输出顺序确定）
- `gofmt -l .`：无输出

## 五、关键设计取舍

1. **撤回的迟到判定**：撤回记录携带的生效时间为可选；版本已知时以存储的生效时间为准，符合“撤回与新增同样按该版本生效时间判定迟到”。
2. **封口即清理状态**：封口的左事件从工作态移除并进入只读 `Sealed`；GC 只在封口周期和 `Finish` 时运行，保证删除决策始终基于最新水位。
3. **终态 GC 与开放流隔离**：`no-pending-left` 仅在双水位达到 `MaxInt64`（`Finish`）时启用，避免开放流中误删未来左事件仍需要的版本。
4. **并发模型**：粗粒度互斥锁保证所有外部可见状态一致；确定性的排序（封口按事件时间+到达序号、GC 按 key+版本 ID、重算按左事件到达顺序）使并发与串行实例可逐字节比对。

## 六、文件清单

```
go.mod           模块 temporaljoin，go 1.20，零依赖
joiner.go        算子实现（输入/输出/水位/封口/GC/快照恢复）
joiner_test.go   8 个测试：主序列、边界、迟到、GC、恢复、并发交错
README.md        本文件
```
