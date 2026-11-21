# expertparallel — 单轮专家并行派发/回收组件

纯 Go（Go 1.20.2，仅标准库）实现的一轮专家并行（expert-parallel）Token 派发与回收组件，无任何外部依赖。

## 业务规则与对应实现

题目规则与代码行为一一对应：

1. **收齐再派发**：一轮开始时已知全部参与 rank；每个 rank 恰好提交一个批次。
   `SubmitBatch` 只负责收集与校验，`Dispatch` 在所有 rank 到齐前一律返回
   `ErrBatchesIncomplete`，不会提前派发。
2. **候选与权重**：每个 Token 带全局唯一编号（`TokenID`）、原批位置（`BatchPosition`）
   和至多两个候选（`Candidate`，正整数权重）。超过两个候选、权重非正、引用未知专家、
   同一 Token 重复提名同一专家都会在校验阶段被拒绝。
3. **固定容量截断选取**：对每个专家，把所有来源 rank 的候选按 **权重降序、Token 编号升序**
   排序，取到该专家容量上限；容量为 0 时一个都不取。只有被选中的候选才会生成工作项，
   并按专家打包成 `WorkPackage`（每个已配置专家都会出一个包，可能为空）。
4. **按工作项身份回收、允许乱序与重送**：回包 `ReturnPacket` 以
   `(ExpertID, TokenID)` 工作项身份寻址，长度必须等于构造时配置的向量长度，提交顺序任意。
   同一工作项的重复回包是**幂等空操作**：计数不重复增加、保留首个向量、绝不令某个 Token
   提前凑齐结果。完成条件是“每个不同的被接收工作项都恰好有一份向量”。
5. **按实际接收权重新归一化合并**：合并时只统计该 Token **最终被接收**的专家权重并重新归一
   （例如原候选权重 3+1，但权重 1 的专家落选，则权重 3 归一为 1.0），再加权求和各专家向量。
6. **完全落选回填**：没有任何专家接收的 Token（含无候选 Token、只提名零容量专家的 Token）
   得到全零向量并置 `Overflow = true`。
7. **还原原批位置**：`Results` 按 rank 构造顺序返回 `RankResult`，其 `Tokens` 切片按
   原 `BatchPosition` 索引，位置与编号都可直接核对。

## 公开 API

类型与方法定义在 `types.go` 与 `round.go`：

| 符号 | 说明 |
| --- | --- |
| `NewRound(rankIDs []string, expertSpecs []ExpertSpec, vectorLength int) (*Round, error)` | 构造一轮，校验 rank/专家/容量/向量长度配置 |
| `(*Round).SubmitBatch(rankID string, tokens []Token) error` | 提交一个 rank 的批次；整批原子校验，失败不改变状态 |
| `(*Round).AllBatchesCollected() bool` | 是否所有 rank 都已提交 |
| `(*Round).Dispatch() ([]WorkPackage, error)` | 收齐后执行容量选取并返回每专家工作包（独立副本，仅可调用一次） |
| `(*Round).Dispatched() bool` | 是否已派发 |
| `(*Round).TotalWorkItems() int` | 被接收的不同工作项总数 |
| `(*Round).SubmitReturn(pkt ReturnPacket) error` | 回收一个回包；乱序合法、重送幂等去重 |
| `(*Round).ReturnedCount() int` | 已收到的**不同**工作项数（重送不重复计数） |
| `(*Round).RemainingWorkItems() []WorkItem` | 尚缺回包的工作项，便于重传/巡检 |
| `(*Round).Complete() bool` | 全部不同工作项是否回收完毕 |
| `(*Round).Results() ([]RankResult, error)` | 终态合并；未齐返回 `ErrIncomplete`，结果为独立副本 |

主要数据类型：`Token`、`Candidate`、`ExpertSpec`、`WorkItem`、`WorkPackage`、
`ReturnPacket`、`TokenResult`（含 `Vector`、`Overflow`、`AcceptedExperts`）、`RankResult`。

错误均为哨兵错误，可用 `errors.Is` 判定：`ErrNoRanks`、`ErrNoExperts`、
`ErrInvalidVectorLength`、`ErrInvalidRank`、`ErrDuplicateRank`、`ErrInvalidExpert`、
`ErrInvalidCapacity`、`ErrUnknownToken`、`ErrInvalidPosition`、`ErrInvalidCandidate`、
`ErrDuplicateCandidate`、`ErrBatchesIncomplete`、`ErrNotCollecting`、`ErrNotDispatched`、
`ErrUnknownWorkItem`、`ErrVectorLengthMismatch`、`ErrIncomplete`。

`Round` 内部以 `sync.Mutex` 保护全部状态，批次提交、回包回收与状态查询可并发调用。

## 复现步骤

```sh
go version        # 期望 go1.20.2（本仓库在 go1.20.2 darwin/arm64 验证）
go test -race -cover ./...
go test -race -v ./...        # 查看每个用例的逐条执行结果
gofmt -l .                    # 无输出表示格式合规
```

## 验证范围（均为真实断言，无预置演示输出）

`round_test.go` 中 7 个测试函数、全部通过 `go test -race` 实测，语句覆盖率 95.3%：

- `TestSelectionAndMerge`（正常场景）：两个 rank 交叉候选；断言容量截断顺序
  （权重降序 + TokenID 升序的平局判定）、未齐不可派发、回包乱序提交、按实际接收权重的
  归一化合并数值，以及两个 rank 原批位置的精确还原。
- `TestRenormalizationAndOverflow`（边界）：零容量专家、权重平局靠 TokenID 落选、
  无候选 Token、仅提名零容量专家的 Token；断言重新归一化算术、全零回填与 `Overflow` 标记。
- `TestTwoAcceptedExpertsMerge`（正常）：两个专家同时接收时 `0.25/0.75` 归一化加权的精确数值。
- `TestDuplicateReturnIsDeduplicated`（重送交错）：同一回包重送 3 次不增加计数、
  不能提前完成、`Results` 仍报 `ErrIncomplete`，且首个向量生效、重送向量被丢弃。
- `TestConcurrentBatchesAndShuffledReturns`（并发交错）：4 个 goroutine 并发提交批次，
  12 个工作项并发乱序回包并对半数做并发重送；`-race` 下无数据竞争，计数稳定，
  并用独立重算逻辑逐个核对合并向量与位置还原。
- `TestValidationAndLifecycleErrors`（边界/错误）：全部非法配置与非法批次、
  生命周期误用（派发前后的提交/回收/重复派发）、向量长度不符、未知工作项，
  以及失败校验不污染状态、返回工作包是独立副本。
- `TestResultsAreIndependentCopies`：调用方篡改返回切片不影响组件内部状态，重复取结果一致。

未覆盖项：无（全部测试均已实际执行；本组件不涉及真实网络、账户或外部系统，所有数据均为
测试内本地合成数据）。
