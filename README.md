# 盲文点显器设备输出协调器（braillecoord）

用 Go 1.20 实现的保密工作站盲文点显器输出协调器。协调器把一行盲文拆成
若干**异步写命令**，每条命令带单调递增的写序号 `WriteSeq` 与发出时的
连接世代 `Generation`；设备可以乱序完成命令，协调器在回执中核对物理格位、
连接世代与写序号，并负责读者切换时的格位清场安全。

## 安全模型（清场判据）

把一个物理格位从上一读者分给新读者，必须**同时**满足：

1. 该格位在**当前连接世代**没有任何未确认写命令——即上一读者最后一次
   可能带密信息的写入已经收尾；
2. 当前连接世代存在一次序号晚于最后敏感写入的**清零命令**（写 `Pattern(0)`，
   所有触点落下），且该清零已随条件 1 一并收尾确认；
3. 所有推进状态的回执必须属于当前连接世代。**旧连接迟到的成功回执一律
   丢弃，不能推进新连接清场。**

满足条件的格位进入 `CellReleasable`，经 `FinishSwitch` 或 `Allocate` 才
真正分给新读者。清零回执失败（`Receipt.OK == false`）不视为清场完成，
需要重新发清零。

### 乱序下的物理刷新

设备可能先完成清零、后完成更早的敏感写入。此时协调器的**开放决策**仍然
安全（敏感写入与清零都已收尾且清零序号更晚），但内存设备按完成顺序落值，
物理格位可能暂时保留旧点位。`ClearRange` 允许对已 `CellReleasable` 的
格位再补一次清零来刷新物理格位，测试 `TestHalfLineClearanceOutOfOrder`
覆盖了这一交错。

### 设备重连

`Reconnect` 使连接世代加一：

- 旧世代在途命令全部作废，其迟到回执在 `OnReceipt` 被世代核对丢弃；
- 所有格位在新世代变为未核验，已判定可释放的格位回退为重新清场；
- 切换冻结中的格位在新世代重发清零；活动模式下只对**当前读者所有**的
  格位重发其最后内容，未清场的冻结格位与空闲格位只发清零——**绝不向新
  连接重发旧密信息**（见 `TestReconnectFrozenCellGetsZeroNotSecret`）。

## 公开 API（包 `braillecoord/display`）

### 核心类型

| 类型 | 说明 |
| --- | --- |
| `Pattern uint8` | 单格盲文点位值，`0` 表示清零 |
| `ReaderID string` | 读者标识 |
| `Generation uint64` | 设备连接世代，重连加一 |
| `WriteSeq uint64` | 协调器范围内单调递增的写序号，从 1 开始 |
| `CellState` | `CellFree` / `CellActive` / `CellClearing` / `CellReleasable` |
| `WriteCommand` | 发给设备的异步命令（格位、世代、序号、点位、是否可能带密） |
| `Receipt` | 设备回执（物理格位、世代、写序号、点位、成功与否） |
| `Device` / `ReceiptSink` | 设备抽象与回执接收接口 |
| `CellStatus` | 格位只读状态快照 |

### Coordinator

- `New(dev Device, physicalCells int) (*Coordinator, error)`
- `WriteCell(cell int, value Pattern, sensitive bool) (WriteSeq, error)`：
  局部改写一个物理格位；切换冻结期返回 `ErrFrozen`
- `SwitchReader() error`：冻结上一读者，活动格位转 `CellClearing`，
  从未使用的空白格位直接可释放
- `ClearRange(from, to int) ([]WriteSeq, error)` / `ClearAll()`：
  对指定半开区间（如左半行 `[0, n/2)`）发清零
- `ReleasableCells() []int`：当前可安全开放的物理格位
- `Allocate(cell int, newReader ReaderID) error`：显式分配单个已清场格位
- `FinishSwitch(newReader ReaderID) ([]int, error)`：开放全部已清场格位，
  未清场格位继续冻结
- `Reconnect() (Generation, []WriteSeq, error)`：设备重连后的世代推进与再同步
- `OnReceipt(Receipt)`：实现 `ReceiptSink`，统一处理乱序/迟到回执
- `Status() []CellStatus`、`Outstanding() int`、`Generation() Generation`、
  `Switching() bool`、`CurrentReader() ReaderID`

### 逻辑行平移：LogicalLine

- `NewLogicalLine(c *Coordinator) *LogicalLine`
- `LoadLine(values []Pattern, sensitive []bool) error`：加载逻辑行，
  短于物理行时多余物理格位清零
- `Pan(newOffset int) error`：窗口平移，只向点位实际变化的物理格位发命令
- `RewriteLogical(index int, value Pattern, sensitive bool) error`：
  局部改写；窗口外只更新逻辑行，平移回来时再落设备

### 内存设备：MemoryDevice

纯内存、可乱序确认的演示/测试设备：

- `NewMemoryDevice(cells int) *MemoryDevice`
- `Complete(seq WriteSeq, ok bool) bool`：完成单条命令（任意顺序）
- `CompleteAll(ok bool) int`：按提交顺序完成
- `CompleteRandom(rng, ok bool) int`：伪随机乱序完成
- `FailNext()` / `FailSeq(seq)`：模拟同步拒单
- `Cell(cell int) Pattern`、`Pending()`、`PendingAt(cell int)`

## 运行方法

```sh
go test ./...            # 全部测试与 Example
go test -race ./...      # 竞态检测
go test -v -count=1 ./...
```

无外部依赖，仅使用标准库；不需要联网。

## 验证范围

全部判断均由 `display/*_test.go` 中的实测断言支撑，共 19 个测试用例与
1 个可执行 `Example`：

- **正常场景**：写序号单调、回执三字段核对、乱序确认、重复回执忽略、
  内存设备物理落值；
- **清场安全**：敏感写入未收尾时即使清零已确认也不开放；清零确认缺失时
  不开放；失败回执必须重发清零；两种回执到达顺序分别验证；
- **半行清零与交错**（`TestHalfLineClearanceOutOfOrder` 与
  `ExampleHalfLineClearance`）：8 格设备只清左半行，验证可开放的物理位置
  恰为 `[0 1 2 3]`，右半行继续冻结并保留旧内容；乱序导致的物理残留由
  补一次清零修复；
- **连接世代**：旧世代成功回执不推进新世代清场；重连回退可释放格位；
  活动模式重连只重发当前读者内容；部分切换后重连绝不重发旧密；
- **逻辑行**：平移、越界边界、短行清零、未变格位跳过、窗口内外局部改写；
- **并发与压力**：并发写入 + 切换 + 随机乱序回执（`-race`），多轮
  重连/随机确认压力测试；
- **输入校验**：空设备、零格位、格位越界、无读者、重复读者、冻结期写入、
  非切换态清场/结束切换、设备拒单后的状态回收。

## 已知限制

- `MemoryDevice` 是测试替身：其物理落值按回执完成顺序更新，用于真实再现
  乱序语义；真实硬件若保证同格位按提交顺序生效，则不会出现需要补清零的
  物理残留。
- 协调器在单进程内用互斥锁保证线程安全；跨进程/掉电持久化不在本题范围内。
- `go vet` 在当前沙箱需额外授权未能执行；已通过 `go build`、`gofmt` 与
  `go test -race`（重复 20 轮）验证。
