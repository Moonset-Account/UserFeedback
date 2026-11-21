# alignedjoin — 双输入有符号增量累加与对齐检查点

一个流处理算子（Go 1.20.2，仅标准库）：两路输入（`Left` / `Right`）中同键的有符号
增量被累加；每处理一条记录，就输出该键的新值。两路输入均为从 0 开始的连续位置序列，
并按相同编号顺序收到屏障（barrier）。算子支持**对齐检查点**与确定性重放。

## 语义

1. **正常处理**：`Pull` 处理一条记录时，把 `Delta` 累加到 `Key` 上，返回包含新值的
   `EvOutput` 事件。两路输入共享同一份按键累加的状态。
2. **对齐屏障**：先消费到屏障的一路进入阻塞（`Blocked` 为真、`PendingBarrier` 为真），
   此时再拉取该路会得到 `EvStaged`——**不消费位置、不修改状态**，记录仍留在队首；
   另一路在对齐完成前可以继续更新状态。
3. **固化快照**：同编号屏障在两路都到达后，算子深拷贝当前累加状态并记录两路屏障位置，
   生成 `Snapshot`（随 `EvCheckpoint` 事件返回），随后解除两路阻塞。暂存记录的位置没有
   被消费，检查点后用**同一位置**重新拉取即可处理。
4. **单一在途检查点**：一次只允许一个未完成的检查点；重复屏障、编号不匹配、编号不递增
   都会在出队前被拒绝，队列与位置游标保持不变。
5. **重开重放**：`Restore(snapshot)` 还原快照后，调用方对每路从
   `Snapshot.ResumePos(side)`（屏障位 + 1）开始，按与原运行**相同的消费拉取顺序**重放，
   得到与原运行逐事件一致的输出后缀与最终状态。`EvStaged` 不是消费事件，不构成拉取顺序。

### 题面示例（左屏障后排 +7，右屏障前排 −7）

| 步骤 | 操作 | 结果 |
| --- | --- | --- |
| 1 | 左路 `k+1`，随后左路消费 `B1` | 状态 `k=1`，左路阻塞 |
| 2 | 左路压入 `k+7` 并拉取位置 2 | `EvStaged`，状态仍为 `k=1` |
| 3 | 右路消费 `k−7` | 状态更新为 `k=−6`（右路未阻塞，可更新） |
| 4 | 右路消费 `B1` | `EvCheckpoint`：快照含 `k=−6`（含 −7、不含 +7），屏障位 `{1,1}` |
| 5 | 左路重新拉取位置 2（`k+7`） | `EvOutput k=1` |

对应测试：`TestHeadlineInterleaving`。

## 公开 API（package `alignedjoin`）

| 标识符 | 说明 |
| --- | --- |
| `type Side int`，常量 `Left`/`Right` | 两路输入 |
| `type Item`，`Record(key, delta)` / `Barrier(id)` | 输入项：数据记录或屏障 |
| `func New() *Operator` | 创建空算子 |
| `func Restore(*Snapshot) *Operator` | 从快照重开算子 |
| `func (o *Operator) Push(sn Side, item Item) error` | 向一路输入追加一个输入项（缓冲，不立即处理） |
| `func (o *Operator) Pull(sn Side, pos int64) (Event, error)` | 消费该路 `pos` 位置的下一项；位置必须连续 |
| `type Event` / `EventKind` | `EvIdle`、`EvOutput`、`EvStaged`、`EvBarrier`、`EvCheckpoint` |
| `type Snapshot` | `BarrierID`、`BarrierPos [2]int64`、`Values map[string]int64`（深拷贝） |
| `func (s *Snapshot) ResumePos(sn Side) int64` | 重放起始位置（屏障位 + 1） |
| `Value(key)` / `PendingBarrier()` / `Blocked(sn)` / `LastBarrier()` | 状态与检查点观测 |
| 哨兵错误 | `ErrInvalidSide`、`ErrInvalidItem`、`ErrEmptyQueue`、`ErrNonContiguousPos`、`ErrBarrierMismatch`、`ErrCheckpointInFlight`、`ErrBarrierOrder`（均支持 `errors.Is`） |

约定：

- 单 goroutine 使用，由调用方通过 `Push`/`Pull` 显式驱动，交错顺序完全由调用方控制。
- 屏障编号必须为正且严格递增；两路同次检查点的编号必须相同。
- 每路位置从 0 开始连续；`Pull` 到空队列返回 `ErrEmptyQueue`（带 `EvIdle`），算子不变，
  调用方可转而去拉另一路。
- 检查点在屏障类协议错误发生时不出队、不前进位置，算子可继续使用。

## 复现步骤

```sh
go version          # 要求 go1.20.2（本机即 go1.20.2 darwin/arm64）
go test -v ./...    # 全部功能测试，含真实断言
go test -race -count=3 ./...
go test -cover ./...
```

无外部依赖，`go.mod` 不引入任何 require；go 指令写作 `go 1.20`（Go ≤ 1.20 的模块解析
不接受 `1.20.2` 这种带补丁号的写法，1.21 起才支持，实际编译运行仍由 go1.20.2 工具链完成）。

## 验证范围

测试全部为真实功能断言（非预置演示输出），共 9 个测试函数：

| 测试 | 场景分类 | 覆盖内容 |
| --- | --- | --- |
| `TestBasicAccumulation` | 正常 | 两路同键有符号增量累加、每条记录输出新值、交错拉取 |
| `TestHeadlineInterleaving` | 交错 + 边界 | 题面 +7/−7 场景；`EvStaged` 不消费位置；快照含 −7 不含 +7；快照后重拉 +7；快照重放输出一致 |
| `TestBoundaryAdjacentBarriers` | 边界 | 屏障位于位置 0、背靠背屏障、一路屏障前无记录、连续两个检查点 |
| `TestMultipleCheckpointsReplay` | 正常 + 重放 | 两个对齐检查点；从第一个快照重放，输出后缀与最终状态逐事件一致 |
| `TestRandomInterleavingReplay` | 交错 + 重放 | 30 个随机种子、随机拉取交错（含阻塞侧重试）；断言每次都产生 `EvStaged`，且按记录的消费拉取顺序重放时输出后缀与最终状态完全一致 |
| `TestZeroAndCrossingValues` | 边界 | 不存在键读零、累加回零、负值穿越零 |
| `TestSnapshotIsolation` | 边界 | 快照是独立深拷贝；外部篡改快照不影响活算子与后续检查点 |
| `TestProtocolErrors` | 错误路径 | 非法 side、畸形 Item、空队列、位置不连续（失败不前进游标）、屏障编号不匹配、检查点在途重复屏障、重开后旧编号屏障 |
| `TestResumePos` | API | 快照重放位置算术 |

当前结果：`go test -race -count=3` 通过，语句覆盖率 100.0%。

## 文件

- `aligned.go` — 算子实现
- `aligned_test.go` — 全部测试
- `go.mod` — 模块定义（无依赖）
