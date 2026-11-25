# blockfilter

对象块索引使用的**紧凑计数过滤器**：以调用方提供的哈希位置为输入，对“肯定不存在”的块做快速跳过。

过滤器不保存每个块的键表，只保存稀疏的槽计数；同一块的多个哈希可以落到同一个槽。变更按**批次**提交，整批校验通过后作为一个新的**不可变代**发布，旧读者持有的快照继续有效。

## 设计要点

- **四位主槽**：每个主槽占一个 4 bit 半字节（两个槽打包在一个字节里），计数范围 `0..15`。零表示该位置肯定没有活跃引用，因此任何位置为零即可判定块“肯定不存在”。
- **稀疏溢出区**：计数达到 15 后，每一次额外增持只把“超出 15 的部分”记到 `map[uint32]uint32`，仅对饱和槽存在条目。释放时先消耗溢出量，溢出归零即**删除**条目，不会留下冗余的零值条目。
- **按出现计数**：`Change.Positions` 中每个出现计一次。同一块有重复哈希落到同一槽时，重复增持与逐次释放严格对称。
- **整批原子发布**：`Apply` 在私有副本上按顺序应用整批变更，任何一条校验失败则整批拒绝、不发布、不改变当前代。成功则代 +1 并原子替换指针。
- **快照固定**：`Filter.Snapshot()` 返回的 `*Snapshot` 不可变，发布新代不影响它，旧读者可以固定快照继续判断。
- **无完整键表**：状态只有打包计数数组和稀疏溢出 map，不为每个已插入块保存完整键列表。

## 公开 API

```go
package blockfilter

const MaxSlots uint32 = 1 << 28

// 错误值（可用 errors.Is 匹配）
var ErrNoSlots, ErrTooManySlots, ErrEmptyBatch, ErrNoPositions,
    ErrPositionOutOfRange, ErrUnknownKind, ErrRemoveNotHeld, ErrCounterOverflow error

type Kind uint8
const (
    KindAdd Kind = iota + 1 // 每个位置 +1
    KindRemove              // 每个位置 -1
)
func (k Kind) String() string

type Change struct {
    Kind      Kind
    Positions []uint32 // 调用方提供的哈希位置（主槽下标）
}

// 构造
func New(slots uint32) (*Filter, error) // slots 必须在 (0, MaxSlots]

type Filter
func (f *Filter) Apply(batch []Change) (*Snapshot, error) // 整批原子发布；成功时代 +1
func (f *Filter) Snapshot() *Snapshot                    // 固定当前代
func (f *Filter) Generation() uint64
func (f *Filter) Slots() uint32

type Snapshot // 不可变、并发安全
func (s *Snapshot) Generation() uint64
func (s *Snapshot) Slots() uint32
func (s *Snapshot) MaybePresent(pos uint32) bool              // 该位置计数非零
func (s *Snapshot) BlockMaybePresent(positions []uint32) bool // 所有位置均非零
func (s *Snapshot) BlockDefinitelyAbsent(positions []uint32) bool // 任一位置为零
func (s *Snapshot) Counter(pos uint32) (count uint32, ok bool)         // 含溢出量
func (s *Snapshot) PrimaryCounter(pos uint32) (count uint32, ok bool)  // 仅四位部分 0..15
func (s *Snapshot) OverflowExcess(pos uint32) (excess uint32, ok bool) // 超出 15 的部分
func (s *Snapshot) OverflowEntries() int                               // 当前溢出条目数
```

语义约定：

- `BlockDefinitelyAbsent` 在任一位置计数为零时返回 `true`；空位置列表返回 `false`（无证据）。
- 越界位置没有计数器，视作零：`MaybePresent` 返回 `false`，`Counter` 的 `ok` 为 `false`。
- `ErrRemoveNotHeld`：释放次数多于槽当前持有量，整批拒绝，避免把仍持有的块降成“肯定不存在”。
- `ErrCounterOverflow`：溢出量会超过 `uint32` 表示范围（仅构造极端状态触发）。

## 运行方法

需要 Go 1.20.2。

```sh
go test ./...              # 运行全部测试（使用本地合成数据，无网络、无外部依赖）
go test ./... -race        # 竞态检测
go test ./... -v           # 逐条查看覆盖场景
```

## 验证范围（均由 `blockfilter_test.go` 实测覆盖）

| 场景 | 测试 |
| --- | --- |
| 构造校验（0 / 超 `MaxSlots`）、初始代与初始溢出 | `TestNewValidation` |
| 增持/释放基本语义、计数器与“肯定不存在” | `TestAddAndRemoveBasic` |
| 同一块重复哈希到同一槽 + 重复增持与逐次释放匹配 | `TestDuplicatePositionsAndRepeatedAdds` |
| 任一零位置即证明不存在 | `TestAnyZeroPositionProvesAbsence` |
| 空位置列表（无证据） | `TestEmptyPositions` |
| 越界位置按零处理 | `TestOutOfRangePosition` |
| 整批校验失败原子拒绝（空批/无位置/未知类型/越界/未持有） | `TestBatchValidationIsAtomic` |
| 过度释放被拒绝且不改变状态 | `TestRemoveOverReleaseRejected` |
| 旧快照固定、新代不受影响 | `TestSnapshotPinKeepsOldGeneration` |
| **高度碰撞链推过 15 再逐步降回；每步仍持有的块都非零；最终释放后溢出条目消失** | `TestCollisionChainOverflowsAndDrains` |
| 多饱和槽的稀疏性、局部回落后条目数正确 | `TestOverflowDrainStaysSparse` |
| 计数达 `uint32` 上界时守卫 | `TestCounterOverflowGuarded` |
| 大量互不碰撞块不产生溢出条目 | `TestManyDistinctBlocksStaySparse` |
| 15 与 16 次引用的溢出边界及回落 | `TestExactSaturationBoundary` |
| 同一批内先加后减按顺序生效、代 +1 | `TestApplyBatchAppliesInOrder` |
| 随机增减交错（高频碰撞）与测试内参考模型逐代核对计数、主槽、溢出条目 | `TestRandomizedModelMatch` |
| 并发 `Apply` 代互不丢失，并发读者固定快照不变 | `TestConcurrentAppliesPublishDistinctGenerations` |
| 快照副本互不影响 | `TestCloneIsIndependent` |
| `BlockMaybePresent` 要求所有位置非零 | `TestBlockMaybePresentRequiresAllPositions` |
| 批内计数溢出整批失败且不泄漏部分变更 | `TestBatchCounterOverflowDropsWholeBatch` |
| 访问器越界报告、空溢出 map 安全读取 | `TestSnapshotAccessorsReportRange` |
| 过滤器代/槽数访问器 | `TestFilterGenerationAndSlots` |
| `Kind.String` 名称 | `TestKindString` |

实测结果（2026-11-26，Go 1.20.2，darwin/arm64）：

- `go test ./... -count=1`：PASS
- `go test ./... -count=1 -race`：PASS
- `go test ./... -count=1 -coverprofile=...`：语句覆盖率 100.0%

未覆盖 / 已知限制：

- 本实现不使用真实哈希函数；`Positions` 由调用方提供，测试全部使用本地合成的槽坐标。
- 过滤器只做“肯定不存在”的负向判断；false positive 属预期行为，未做概率评估。
- 不做持久化与序列化。
