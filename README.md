# sharedscan — 三值逻辑的相关子查询共享内侧扫描算子

Go 1.20.2 实现，零第三方依赖。一个 `Operator` 接收同一内侧扫描产生的分块行，
将每一行扇出给一批外行请求；每个外行/子查询维护独立的逐行三值状态和提前终止标记。

它覆盖：

- `IN`
- `NOT IN`，按 `NOT (IN 的三值结果)` 计算
- `EXISTS`
- 外行传入的复合左值 `Request.Left`
- 用于相关条件的复合参数 `Request.Param`
- 显式比较规则 `Rule`
  - `SQLRule`：标准 SQL，`NULL` 比较得到 `UNKNOWN`
  - `NullAwareRule`：`IS NOT DISTINCT FROM` 语义，两个 `NULL` 相等
  - `CaseInsensitiveRule`：文本大小写不敏感，空值仍为 SQL 三值
  - 可传入自定义 `Rule.Equal`
- 复合行逐字段比较，字段结果用三值 `AND` 合并；`FALSE` 可支配 `UNKNOWN`
- 按候选集合标签或依赖键分块
- 单个外行找到决定性匹配后停止评估，但共享扫描继续为其他未决外行提供剩余行
- `TRUE / FALSE / UNKNOWN` 的可观察结果、计数和 witness

## 公开 API

核心类型和函数：

```go
type Truth uint8
const (
    TruthUnknown Truth = iota
    TruthFalse
    TruthTrue
)
func Not(Truth) Truth
func And(...Truth) Truth
func Or(...Truth) Truth

type Datum struct { Null bool; Value any }
type Row []Datum
func NullDatum() Datum
func Int(int64) Datum
func StringDatum(string) Datum
func RowOf(...any) Row                 // nil 变成 NULL
func (Row) NullPositions() []int
func (Row) NullBitmap() string
func (Row) PhysicalKey() string

type Rule struct {
    ID         string
    NullsEqual bool
    Equal      func(a, b any) (bool, error)
}
var SQLRule, NullAwareRule, CaseInsensitiveRule Rule
func (Rule) Compare(Datum, Datum) (Truth, error)
func CompareRows(Row, Row, Rule) (Truth, error)

type Expr uint8
const (
    ExprExists Expr = iota + 1
    ExprIn
    ExprNotIn
)

type Request struct {
    ID           string
    Expr         Expr
    Left         Row   // IN / NOT IN 的复合左值
    Param        Row   // 相关条件的复合外行参数
    Rule         Rule
    CandidateSet string
}
func (Request) Fingerprint() (string, error)

type InnerRow struct {
    Key     Row   // 与 Request.Param 做相关匹配
    Values  Row   // 与 Request.Left 做 IN/NOT IN 比较
    Payload any   // 可选的原始执行器元组
}

type Chunk struct {
    Set        string
    Dependency Row
    Rows       []InnerRow
}
func DependencySetID(Row) string
func ChunkByDependency([]InnerRow) []Chunk

type Operator
func NewOperator([]Request, ...Option) (*Operator, error)
func WithCorrelator(CorrelationFunc) Option
func MatchParameter(Request, InnerRow) (bool, error)
func (*Operator) Consume(...InnerRow) error
func (*Operator) ConsumeChunk(Chunk) error
func (*Operator) Progress() []Progress
func (*Operator) PendingCount() int
func (*Operator) AllDecisive() bool
func (*Operator) Finish() ([]Result, error)
func (*Operator) Results() []Result
func (*Operator) Stats() Stats

type Scanner interface { NextChunk(context.Context) (Chunk, error) }
type ScannerFunc func(context.Context) (Chunk, error)
func Evaluate(context.Context, []Request, Scanner, ...Option) ([]Result, Stats, error)
```

完整可执行用法见 `example_test.go`；行为和边界断言见 `operator_test.go`。

## 三值语义

对 `IN`，算子逐行计算复合行相等值并做三值 `OR`：

1. 任意一行比较为 `TRUE`：`IN = TRUE`，`NOT IN = FALSE`；该外行可提前终止。
2. 没有 `TRUE`，但至少一次比较为 `UNKNOWN`：`IN = UNKNOWN`，`NOT IN = UNKNOWN`。
3. 集合为空，或所有行均明确为 `FALSE`：`IN = FALSE`，`NOT IN = TRUE`。

因此，左值为 `NULL` 且内表为空时，`IN` 仍是 `FALSE`、`NOT IN` 仍是 `TRUE`；
只有非空集合中的比较产生 `UNKNOWN` 且没有真匹配时，结果才是 `UNKNOWN`。

`EXISTS` 只看相关谓词过滤后是否有候选行。候选行的投影列含 `NULL` 不会使
`EXISTS` 变成 `UNKNOWN`。相关谓词本身遵循 SQL `WHERE`：只有 `TRUE` 通过，
`FALSE` 和 `UNKNOWN` 都拒绝该行。`MatchParameter` 在 `SQLRule` 下会把
`NULL = NULL` 计算为 `UNKNOWN` 并拒绝候选；若需要物理上的 NULL-safe 相关，
显式使用 `NullAwareRule` 或自定义 `CorrelationFunc`。

## 空左值、空内表和内表 NULL 并存的结果

以下矩阵由 `Example` 实际运行并校验，不是手工推断：

```text
left=NULL; inner=empty; rule=SQL: IN=FALSE NOT_IN=TRUE EXISTS=FALSE
left=1; inner=empty; rule=SQL: IN=FALSE NOT_IN=TRUE EXISTS=FALSE
left=NULL; inner=(1,2); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
left=NULL; inner=(1,NULL); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
left=1; inner=(NULL,2); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
left=1; inner=(NULL,1); rule=SQL: IN=TRUE NOT_IN=FALSE EXISTS=TRUE
left=3; inner=(NULL,1); rule=SQL: IN=UNKNOWN NOT_IN=UNKNOWN EXISTS=TRUE
left=NULL; inner=(NULL); rule=NULL_AWARE: IN=TRUE NOT_IN=FALSE EXISTS=TRUE
```

## 共享扫描、依赖分块和提前终止

所有 `Request` 放在同一个 `Operator` 中。每次 `ConsumeChunk` 读取的原始内侧行只
扫描一次，然后扇出给尚未找到决定性证据的请求。

- `Request.CandidateSet` 与 `Chunk.Set` 相同的请求才接收该命名块；
  `CandidateSet == ""` 的请求接收所有块。
- `ChunkByDependency` 使用包含列数、类型和 NULL 位置的 `Row.PhysicalKey()`
  做物理分组。物理分组把相同 NULL 位置放在一起，但不改变 SQL 比较规则。
- 两个外行即使收到同一个 `Chunk`，仍各自拥有 tracker。规则 ID、左值 NULL 位置、
  相关参数 NULL 位置不同，都会体现在 `Request.Fingerprint()` 中，不能共用判定状态。
- 某个 `IN` 找到 `TRUE` 行或某个 `NOT IN` 找到使 `IN=TRUE` 的行后，该请求之后
  不再计数或比较；其他请求继续处理后续 chunk。
- `Evaluate` 只在所有请求都找到扫描期决定性证据时提前停止拉取；否则读到
  `io.EOF`，保证未决请求获得判断 `FALSE/UNKNOWN` 所需的全部证据。

注意：在保持行顺序的前提下，把数据拆成任意 chunk 不改变 `Result`（包括比较次数）。
`ChunkByDependency` 可能重排交错的依赖键；最终三值结果不变，但提前终止前的比较次数、
correlation 次数和 witness 等扫描顺序相关信息可能变化。

## 运行方法

要求 Go 1.20.x；开发环境实际使用 `go version go1.20.2 darwin/arm64`。

```sh
go test ./...
```

查看全部子测试和可运行示例：

```sh
go test -v -count=1 ./...
```

只运行矩阵示例：

```sh
go test -run '^Example$' -v ./...
```

格式检查：

```sh
gofmt -l .
```

## 已验证范围

当前测试全部使用本地合成数据，不联网、不读取凭据、不访问真实数据库，也不安装依赖。
已由实际 `go test -v -count=1 ./...` 执行并通过的断言覆盖：

- 三值 `NOT/AND/OR` 真值表，包括 `FALSE AND UNKNOWN = FALSE` 和
  `TRUE OR UNKNOWN = TRUE`。
- 复合行比较、不同 NULL 位置、SQL/NULL-safe/大小写不敏感规则。
- `IN` 与 `NOT IN` 在空内表、全 FALSE、仅 UNKNOWN、NULL 与普通值并存、
  NULL 后出现真匹配等情况下的结果。
- 左值为 NULL、内表为空、内表含 NULL 的展示矩阵；`Example` 输出被 Go 测试框架逐字校验。
- `EXISTS` 空集为 `FALSE`，含 NULL 列的候选行仍为 `TRUE` 并立即终止。
- 相关参数含 NULL 时，SQL 规则拒绝 `UNKNOWN` 相关行，NULL-safe 规则接受。
- 不同规则、左值 NULL 位置、参数 NULL 位置产生不同 fingerprint；同一 candidate chunk
  上，参数 NULL 位于第二字段可 NULL-safe 匹配为 `TRUE`，位于第一字段则为 `FALSE`。
- 两个外行共享同一 candidate chunk，但规则或 NULL 位置不同导致不同结果和候选计数。
- 一个外行在第一条匹配后提前终止，另一个外行继续从后续 NULL 和匹配行获得证据。
- `NOT IN` 被真匹配确定为 `FALSE` 后跳过后续 NULL；另一外行仍看到该 NULL 并得到
  `UNKNOWN`。
- 按逐行 chunk 拆分与整块输入结果完全一致；按依赖键分块后最终三值结果一致。
- `Evaluate` 对共享扫描只拉取一个 `Scanner`，并在所有外行均决定性后才停止；
  测试用第四个不应被读取的 chunk 验证全局提前停止边界。
- 非法表达式、重复 ID、缺少规则 ID、行 arity 不匹配、sticky processing error 和
  `ErrFinished` 等错误路径。

未声称已验证：真实优化器/执行器接入、并行 goroutine 调用、磁盘 spill、代价选择、
分布式扫描或真实 SQL 解析器。当前 `Operator` 设计为由一个扫描 goroutine顺序调用；
嵌入多 goroutine 执行器时应由外层同步或为每个扫描分区创建独立 operator。
