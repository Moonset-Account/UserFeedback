# compactlog — 分段键值日志的按时刻清理与按偏移读取

消息代理的分区日志按段（segment）保存键值更新；删除也是一条带偏移和写入时刻的消息（tombstone）。本模块实现两个接口：

1. **按给定时刻清理（`Clean`）**：只重写调用方选中的连续封闭段，活动段及其他封闭段原样保留。
2. **按偏移读取（`ReadFrom`）**：从已清理的空洞续读时，定位到下一条现存消息或日志末尾。

仅使用 Go 1.20.2 标准库，无第三方依赖。

## 公开 API

```go
type Message struct { Offset int64; Key string; Value []byte; Tombstone bool; WrittenAt time.Time }
type Segment struct { ID int; Messages []Message; Sealed bool }

func NewLog() *Log
func (l *Log) Append(key string, value []byte, at time.Time) int64 // 写入更新，返回永久偏移
func (l *Log) Delete(key string, at time.Time) int64               // 写入删除标记，返回永久偏移
func (l *Log) Seal() int                                           // 封闭活动段并开启新活动段
func (l *Log) Segments() []Segment                                 // 段列表快照（只读）
func (l *Log) Clean(now time.Time, retention time.Duration, fromID, toID int) error
func (l *Log) ReadFrom(offset int64) (Message, bool)
```

### 清理语义（`Clean`）

- 仅重写 ID 在 `[fromID, toID]` 内的连续**封闭**段；包含活动段或区间非法时返回错误（`ErrUnsealedSegment` / `ErrEmptyRange`），日志不变。
- 选段内，某键存在更晚记录时移掉旧记录；**更晚记录可以位于选段外**。
- 全日志中某键的最后记录若为删除标记，仅当 **保留期已满**（`now - WrittenAt >= retention`）**且本轮清理后不再有更早的同键记录**（即所有更早记录都在选段内、本轮被移掉）时移掉；其他情况保留。
- 偏移永久保留原编号，存活消息的偏移不变。

### 读取语义（`ReadFrom`）

返回偏移 `>= offset` 的第一条现存消息；无则返回 `false`（日志末尾）。清理留下的空洞被自动跳过。

## 复现步骤

```sh
go test ./...        # 运行全部功能测试
go test ./... -v     # 查看各场景明细
gofmt -l .           # 格式检查（无输出即通过）
```

环境：go1.20.2（darwin/arm64 实测）。

## 验证范围（`log_test.go`，全部真实断言，均已执行通过）

| 测试 | 场景类型 | 验证点 |
|---|---|---|
| `TestCleanDropsStaleValues` | 正常 | 选段内旧值因选段外的更晚记录被移掉；未选封闭段与活动段原样保留 |
| `TestCleanRejectsActiveAndBadRanges` | 边界 | 选中活动段 / 倒置区间 / 越界均报错且日志不变 |
| `TestTombstoneRetentionNotElapsed` | 边界 | 保留期未满的删除标记即使无更早记录也保留 |
| `TestTwoRoundTombstoneCleanup` | 交错 | 先单独清理含删除标记的末段（标记保留，因选段外有更早同键记录），再把它与含旧值的段一起清理（旧值与标记都被移掉）；两轮保留结果不同 |
| `TestReadFromGapAndEnd` | 正常 | 清理后偏移保持原编号；从空洞续读定位到下一条现存消息；越过末尾返回日志末尾 |
| `TestReadFromEmptyLog` | 边界 | 空日志读取返回末尾；清理空封闭段为无操作 |
| `TestTombstoneLatestOutsideSelection` | 交错 | 键的最后记录（删除标记）在选段外时，选段内旧记录被移掉、标记原样保留 |

未执行的性能/并发测试：无（本题为单线程语义验证，未覆盖并发场景）。
