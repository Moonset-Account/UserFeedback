# txstore — 带保存点的内存事务层

一个纯标准库的 Go 包，实现有行 ID、可为空的联合唯一键、逐语句/延迟唯一性检查、
命名保存点、以及基于最新已提交状态校验的乐观提交。

- 模块：`example.com/txstore`
- Go：`go 1.20`（实测工具链 `go1.20.2 darwin/arm64`）
- 依赖：仅标准库，无第三方依赖，无 `go.sum`

## 公开 API

### 数据模型

```go
type Value struct { Null bool; Text string }
func Str(text string) Value
func Null() Value

type Key []Value               // 有序联合键；任一分量为 NULL 即不参与唯一性检查
func (k Key) HasNull() bool
func (k Key) Equal(other Key) bool
func (k Key) String() string   // 规范形式，如 ("tenant",NULL) / ("a","b")

type Row struct { ID string; Key Key; Payload string }
```

`Key.String()` 对非 NULL 文本做 Go 引号转义，NULL 渲染为字面量 `NULL`，因此该字符串是
可排序、可比较的规范键表示，测试用它做键集合比较。`Key.Equal` 要求长度与逐分量相等，
不同元数（arity）的键互不相等。

### 存储

```go
func Open(rows []Row) (*Store, error) // 校验 ID 非空且唯一、初始键无冲突
func (s *Store) Version() uint64
func (s *Store) Get(id string) (Row, bool)
func (s *Store) CommittedRows() []Row
func (s *Store) CommittedKeys() []Key  // 已排序的已提交键集合
func (s *Store) Begin() *Txn
```

`Store` 所有导出方法并发安全（内部互斥锁）。`Begin` 复制提交态引用作为快照；提交只安装
新的不可变行对象，从不原地修改，因此快照永远不会观察到之后的提交。

### 事务

```go
type Mode int
const ( ModeStatement Mode = iota; ModeDeferred )
func (m Mode) String() string

func (t *Txn) BaseVersion() uint64
func (t *Txn) Mode() Mode
func (t *Txn) Get(id string) (Row, bool)      // 读自己的写
func (t *Txn) Rows() []Row
func (t *Txn) Keys() []Key                    // 排序后的读视图键集合
func (t *Txn) Insert(id string, key Key, payload string) error
func (t *Txn) Update(id string, key Key, payload string) error
func (t *Txn) Delete(id string) error
func (t *Txn) SetMode(mode Mode) error
func (t *Txn) Savepoint(name string) error
func (t *Txn) RollbackTo(name string) error
func (t *Txn) Rollback()
func (t *Txn) Commit() (CommitResult, error)
func (t *Txn) Result() CommitResult

type CommitResult struct { Version uint64; Rows int }
```

### 错误（用 `errors.Is` 判断）

`ErrUniqueViolation`、`ErrWriteConflict`、`ErrRowExists`、`ErrRowNotFound`、
`ErrDuplicateID`、`ErrSavepointNotFound`、`ErrSavepointExists`、
`ErrInvalidSavepointName`、`ErrInvalidMode`、`ErrTxnClosed`。

## 语义要点

1. **空分量免检**：`Key.HasNull()` 为真的行不进入唯一性冲突检查；多行可共享同一含
   NULL 的键。非 NULL 键按元数 + 逐分量做唯一性判定。
2. **读自己的写**：`Get/Rows/Keys` 先把事务的工作集覆盖到快照上，再返回。
3. **逐语句模式（默认）**：每个 `Insert/Update/Delete` 在当前读视图上做唯一性检查；
   失败时该语句整体丢弃，不改数据、不改模式。
4. **延迟模式**：语句只做基础存在性检查，唯一性推迟到 `Commit`。因此可先把两行写成同一
   键、再改写成两个不同的键（键交换）。
5. **切回逐语句**：`SetMode(ModeStatement)` 先校验当前读视图。若冲突则返回
   `ErrUniqueViolation`，模式与当时数据都保持不变。
6. **保存点**：`Savepoint` 记录工作集深拷贝与当前模式；`RollbackTo` 同时回退键修改和模式，
   并使更晚创建的保存点失效；被回退到的保存点保持有效可重复回退。
7. **提交校验**（持锁、先校验后写入，任何失败都不落盘、无部分新键）：
   - 事务写过的每个 ID，其最新已提交行必须与事务起始时的 base 行一致；被并发改写的行
     返回 `ErrWriteConflict`。
   - 用「最新已提交的未改写行 + 本事务结果」组成最终键集合做唯一性检查；并发插入同一新键
     返回 `ErrUniqueViolation`。
   - 提交失败时事务保持打开，读视图不变，可检查或回滚。

## 关键验证序列：两笔事务各自完成交换、并发插入同一新键

见 `TestExchangeThenConcurrentSameKeyInsert`（`store_test.go`）。初始 4 行：
`a1,k=a`、`a2,k=b`、`b1,k=c`、`b2,k=d`。

1. `tx1`、`tx2` 均切到延迟模式。
2. `tx1` 交换 `a1/a2` 的键（`a↔b`），并插入 `new1, k=new`。
3. `tx2` 交换 `b1/b2` 的键（`c↔d`），并插入 `new2, k=new`。
4. **读视图核对**：提交前 `tx1.Keys()` 与 `tx2.Keys()` 都是
   `[a,b,c,d,new]`（各自看到完整快照 + 自己的交换 + 自己的新键；看不到对方未提交的写）。
5. `tx1` 先提交（Version=1，Rows=3），已提交键集合为 `[a,b,c,d,new]`。
6. `tx2` 提交：它只改了 `b1/b2`、新增 `new2`，行级冲突检查通过；但最终键集合里 `new`
   已被 `tx1` 占用，返回 `ErrUniqueViolation`，整笔不落盘。
7. **最终键集合核对**：提交态仍为 `[a,b,c,d,new]`，`b1/b2` 仍是 `c/d`（tx2 的交换完整
   缺失、无部分新键），`new2` 不存在；`tx2` 读视图保持 `[a,b,c,d,new]`。
8. 追加断言：`tx2` 删除 `new2` 后重试提交成功，证明它自己的交换本身是成立的。

## 复现步骤

```sh
cd <本题工作区>
go version          # go1.20.2（go.mod 声明 go 1.20）
go vet ./...        # 静态检查
go test ./... -count=1 -v
go test ./... -count=1 -race
go test ./... -count=20
```

## 验证范围（均为真实执行结果）

| 命令 | 结果 |
| --- | --- |
| `go test ./... -count=1 -v` | 15 个测试全部 PASS，`ok example.com/txstore 0.470s` |
| `go test ./... -count=1 -race` | `ok example.com/txstore 0.602s` |
| `go test ./... -count=20` | `ok example.com/txstore 0.494s`（20 轮无抖动） |
| `gofmt -l .` | 无输出（全部已格式化） |

测试覆盖的正常 / 边界 / 交错场景：

- `TestKeyNullableAndAritySemantics`：NULL 语义、元数不同的键不相等、规范字符串。
- `TestOpenValidatesInitialState`：重复 ID、初始键冲突、空 ID、含 NULL 键免检。
- `TestTransactionReadsItsOwnWrites`：事务内插入/更新/删除可见，提交前对外不可见。
- `TestStatementModeRejectsDuplicateAtomically`：逐语句重复键被拒且不改状态；NULL 键放行。
- `TestDeferredModeSwapOfTwoKeys`：延迟模式下两键交换成功；未解决重复在提交时被拒。
- `TestSetModeToStatementValidatesAndPreservesStateOnFailure`：切回逐语句校验失败时模式
  与数据保留，解决冲突后切换成功并恢复逐语句检查；非法模式报 `ErrInvalidMode`。
- `TestSavepointRestoresKeysAndModeAndInvalidatesLaterOnes`：保存点同时回退键与模式、
  失效后续保存点、可重复回退、空名/重名报错。
- `TestCommitChecksUntouchedRowsAndSameRowConflicts`：未改写行上的并发新键冲突；同一行
  并发改写返回 `ErrWriteConflict` 且不落任何改动；失败后可重试/回滚/关闭检查。
- `TestExchangeThenConcurrentSameKeyInsert`：题面要求的交换 + 并发同键插入交错（见上）。
- `TestConcurrentSameRowWritersOneWins`：25 轮并发的同一行改写，恰好一胜一 `ErrWriteConflict`。
- `TestStatementAndSavepointBoundaries`：ID 已存在/不存在、重复删除、删后重插、已关闭事务。
- `TestCallerSlicesCannotMutateStoreState`：调用方切片/返回键的改动不会污染存储。
- `TestRollbackDiscardsEverythingAndKeepsCommittedStable`：整笔回滚不改变提交态与版本，
  回滚幂等，关闭后操作报 `ErrTxnClosed`。
- `TestDeleteThenReuseKeyInterleaving`：释放键与抢占键的交错；先拒绝对已提交占用者的抢占，
  待删除提交后重试成功。
- `TestSnapshotIsolationFromLaterCommits`：长读视图不受之后提交影响。

未执行：无。所有列出的测试均已真实运行通过。

## 设计取舍

- 唯一性以「非 NULL 联合键字符串」判等，因此键比较开销为 O(分量数)；无索引结构，适合
  合成数据规模。
- 行级冲突检测用「提交行对象指针是否变化」判定，因为提交总是安装新对象；O(1) 每行。
- `CommitResult.Rows` 统计本条事务写入的 ID 数（含删除）。
- 事务本身非并发安全，约定一笔事务一个 goroutine；`Store` 可被多笔事务并发使用。
