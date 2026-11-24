# blockcw — 持久化整组比对写模拟器

`blockcw` 是一个仅依赖 Go 标准库的块设备模拟器。它对一个卷中的多个块执行整组 compare-and-write：

1. 根据卷标识找到卷；
2. 检查请求代号是否已有持久化决定；
3. 一次性比较请求中的全部块；
4. 全部匹配时，在同一个持久状态事务中发布所有替换块和 `COMMITTED` 决定；
5. 任一块不匹配时，不修改任何块，持久化 `CONFLICT` 决定及当时观察到的不匹配块；
6. 相同请求代号、相同规范内容的重试拿回原决定；
7. 请求代号相同但内容不同，返回可用 `errors.Is(err, blockcw.ErrRequestIDReused)` 识别的明确错误。

模块声明为 `module blockcw`，`go.mod` 指定 Go 1.20；实际验证使用 `go version go1.20.2 darwin/arm64`。

## 公开 API

核心入口：

```go
import "blockcw"

store, err := blockcw.Open("./data/blockcw.json")
defer store.Close()

info, err := store.CreateVolume(ctx, "vol-1", blockcw.VolumeConfig{
    BlockSize:  4096,
    BlockCount: 16,
})

err = store.WriteBlock(ctx, "vol-1", 0, initialBlock)

result, err := store.CompareAndWrite(ctx, blockcw.Request{
    VolumeID:  "vol-1",
    RequestID: "client-request-123",
    Operations: []blockcw.Operation{
        {Index: 0, Expected: oldBlock0, Replacement: newBlock0},
        {Index: 3, Expected: oldBlock3, Replacement: newBlock3},
    },
})
```

主要类型和方法：

```go
func Open(path string) (*Store, error)
func (s *Store) Close() error
func (s *Store) Path() string

func (s *Store) CreateVolume(
    ctx context.Context,
    volumeID string,
    cfg VolumeConfig,
) (*VolumeInfo, error)

func (s *Store) WriteBlock(
    ctx context.Context,
    volumeID string,
    index int,
    data []byte,
) error

func (s *Store) WriteBlocks(
    ctx context.Context,
    volumeID string,
    writes []BlockWrite,
) error

func (s *Store) ReadBlock(
    ctx context.Context,
    volumeID string,
    index int,
) ([]byte, error)

func (s *Store) ReadBlocks(
    ctx context.Context,
    volumeID string,
    indices ...int,
) (map[int][]byte, error)

func (s *Store) ReadVolume(
    ctx context.Context,
    volumeID string,
) ([][]byte, error)

func (s *Store) CompareAndWrite(
    ctx context.Context,
    req Request,
) (*Result, error)

func Fingerprint(req Request) string
```

请求与结果：

```go
type Request struct {
    VolumeID   string
    RequestID  string
    Operations []Operation
}

type Operation struct {
    Index       int
    Expected    []byte
    Replacement []byte
}

type Result struct {
    VolumeID    string
    RequestID   string
    Decision    Decision // "COMMITTED" 或 "CONFLICT"
    Mismatches  []Mismatch
    Replayed    bool      // true 表示来自持久化的原决定
    CompletedAt time.Time
}

type Mismatch struct {
    Index    int
    Expected []byte
    Actual   []byte // 原决定发生时的实际块字节
}
```

公开哨兵错误：

- `ErrInvalidRequest`
- `ErrVolumeNotFound`
- `ErrVolumeExists`
- `ErrBlockOutOfRange`
- `ErrRequestIDReused`
- `ErrCorruptState`

代号重用错误的具体类型为 `*RequestReuseError`，包含已有和本次请求的指纹，支持 `errors.As`。

## 关键语义

### 整组比较、一次发布

所有 `Operation` 先完成范围、块长度和字节比较。只有全部 `Expected` 与当前块完全相等时，才在内存中应用替换，并把以下内容放入同一个 JSON 状态：

- 所有卷和块字节；
- 请求代号、请求指纹；
- 最终 `COMMITTED` / `CONFLICT` 决定；
- 冲突时的不匹配块及当时的 `Actual` 字节。

状态文件通过“同目录临时文件 → `fsync` → `rename` → 目录 `fsync`”原子替换。读者要么看到完整旧状态，要么看到完整新状态，不会通过本包的读取 API 看到半组更新。

### 幂等重试和超时

请求指纹使用带 8 字节长度前缀的 SHA-256，规范内容包括：

- `VolumeID`；
- `RequestID`；
- 按 `Index` 排序后的全部块号、`Expected`、`Replacement`。

因此操作顺序不同但块集合相同的重试视为同一请求。请求代号在一个状态文件内全局唯一，卷标识也属于请求内容；同一代号用于不同卷会按内容改变拒绝。

如果调用方在拿到响应前超时或崩溃：

- 若决定尚未持久化，释放锁后用相同请求重试会正常执行；
- 若决定已经持久化，相同请求会得到 `Replayed: true` 的原决定和原始完成时间；
- 冲突重试返回原决定时保存的 `Actual`，即使该块后来已被其他写入修改；
- 重放成功决定不会再次应用替换，因此首次成功之后的普通写入或新的 compare-and-write 不会被旧响应回滚。

上下文目前可中断锁等待；测试 `TestCompareAndWriteContextTimeoutLeavesStoreRetryable` 实测了持锁导致 `context.DeadlineExceeded` 后仍可重试。

### 并发和进程重启

`Open(path)` 使用 `path` 作为完整状态文件路径，并使用稳定的伴随文件 `path + ".lock"` 执行 Unix `flock`：

- 写入和 compare-and-write 使用排他锁；
- 读取整卷或多块时使用共享锁并加载同一份状态；
- 同一进程的多个 `Store` 句柄、多个 goroutine，以及共享同一本地文件路径的多个 Unix 进程都通过该锁串行化关键区。

卷标识只作为 JSON map 的键，不会被拼接成文件系统路径。

非 Unix 平台保留了编译回退：原子状态文件和重启恢复仍有效，但回退锁不协调并发进程；本工作区的实测平台是 macOS/darwin。

## 运行方法

无需联网或安装依赖，使用 Go 1.20.2：

```sh
go version
go test ./... -count=1
```

详细测试：

```sh
go test -v ./... -count=1
```

竞态检测：

```sh
go test -race ./... -count=1
```

## 本地合成验证范围

最近一次在本工作区实际执行：

```text
$ go version
go version go1.20.2 darwin/arm64

$ go test ./... -count=1
ok  	blockcw

$ go test -race ./... -count=1
ok  	blockcw

$ go test ./... -count=3
ok  	blockcw	4.379s
```

`go test -v ./... -count=1` 中以下测试全部 PASS：

- `TestCompareAndWriteCommitsAllBlocksAtomically`
  - 3 块乱序请求全部符合后提交；
  - 重试为 `Replayed`，完成时间与原决定一致。
- `TestCompareAndWriteReturnsEveryMismatchAndPreservesOriginalDecision`
  - 一次返回两个不匹配块；
  - 冲突后块不变；
  - 后续写入改变实际块后，重试仍拿回原冲突时的实际字节。
- `TestRequestIDReuseWithChangedContentIsRejected`
  - 替换内容、预期字节、卷标识、操作集合变化均返回 `ErrRequestIDReused` / `*RequestReuseError`；
  - 仅操作顺序变化按相同规范请求重放。
- `TestOtherWritesAndNewCompareAndWritesProceedAfterSuccess`
  - 成功后普通写入和基于新字节的新 CAS 可继续；
  - 重放旧成功请求不会回滚后续写入。
- `TestInvalidRequestsAreRejectedAndNotRecorded`
  - 空卷标识、空请求代号、空操作组、负块号、同块重复、长度不一致、块长度不符、块号越界、未知卷；
  - 断言无效请求不会留下请求决定记录。
- `TestReopenReplaysCommitAndConflictDecisions`
  - 关闭后重新 `Open` 同一文件，提交和冲突决定仍可重放，内容改变仍被拒绝。
- `TestConcurrentIdenticalRequestsShareOneDecision`
  - 24 个 goroutine、4 个 `Store` 句柄执行同一代号请求；
  - 恰好 1 个原始决定、23 个重放，所有调用均成功提交。
- `TestConcurrentDifferentGroupWritesNeverExposeHalfUpdate`
  - 24 个不同代号的整组写入与 8 个持续整卷读取者交错；
  - 恰好 1 个提交、23 个冲突；
  - 所有读取快照中的全部块始终属于同一个完整版本，最终所有块等于获胜写入。
- `TestCompareAndWriteContextTimeoutLeavesStoreRetryable`
  - 实际持有 `flock`，验证上下文超时和释放锁后的重试/幂等重放。
- `TestProcessRestart_DecisionsSurviveInANewOSProcess`
  - 测试实际重新执行当前测试二进制作为新的 OS 进程；
  - 子进程重新打开同一状态文件，验证提交/冲突原决定、保存的冲突字节、代号内容改变拒绝和新写入；
  - 父进程随后再次打开文件，断言子进程的新写入已持久化并可重放。
- `TestMultiProcess_InterleavedIdenticalAndUniqueRequests`
  - 实际并发启动 6 个执行相同代号请求的 OS 进程：恰好 1 个原始决定、5 个重放；
  - 实际并发启动 8 个执行不同整组写入的 OS 进程：恰好 1 个提交、7 个冲突，最终所有块等于获胜进程的内容。

## 未覆盖与限制（如实说明）

这些测试使用本地合成字节、临时目录和本机子进程，证明了进程重启、同机多进程和 goroutine 交错下的行为；没有连接或测试两个物理主机、真实共享块存储、网络文件系统的跨主机 `flock` 语义，也没有进行真实断电或磁盘硬件级 fsync 可靠性试验。

当前状态文件保存完整卷数据和全部请求记录，适合题面要求的模拟器与判定验证；没有实现请求记录压缩、TTL、垃圾回收或状态分片。若未来用于真正的双主机共享文件系统，还需确认该文件系统提供正确的 `flock`/fencing 和原子 rename/fsync 语义。
