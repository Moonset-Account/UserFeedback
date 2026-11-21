# twopc — 两阶段提交（协调器 + 参与者）

Go 1.20.2，仅使用标准库，无第三方依赖。两个账户余额分别存放在两个本地分片
（`Participant`）上，一笔事务可同时给两边加减整数；`Coordinator` 通过两阶段
提交保证两边要么都应用、要么都不应用。

所有状态迁移先持久化（JSON 行追加 + fsync）再应答，因此测试可以在任何一次
持久记录追加之后"停掉"任一方（丢弃对象），再从各自的日志文件重开（重放恢复）。
全程为进程内调用与本地临时文件，不访问任何网络目标。

## 公开 API

### 日志（`wal.go`）
- `OpenLog(path string) (*Log, []Record, error)` — 打开/创建追加式日志并返回已持久化的记录。
- `(*Log).Append(r Record) error` — 追加一条记录并 fsync 后返回。
- `(*Log).Close() error`

### 参与者（`participant.go`）
- `OpenParticipant(id, logPath string, startingBalance int) (*Participant, error)` —
  重放日志恢复分片；日志为空时写入 `init` 记录（初始余额）。
- `(*Participant).Prepare(txID string, delta int) (bool, error)` —
  **先持久化待应用的修改**再投 yes；透支则否决（不写日志）。重送的 Prepare 幂等。
- `(*Participant).Commit(txID string) error` — 持久化 commit 记录并恰好应用一次；重送为 no-op。
- `(*Participant).Abort(txID string) error` — 持久化 abort 记录并丢弃待定修改；重送为 no-op。
- `(*Participant).Balance() int` — 已提交余额；**准备期间按旧值读**（待定修改不可见）。
- `(*Participant).Pending(txID) (int, bool)` / `(*Participant).State(txID) TxState` —
  查询待定修改与事务状态（`unknown/prepared/committed/aborted`）。

### 协调器（`coordinator.go`）
- `OpenCoordinator(logPath string, parts map[string]ParticipantRPC) (*Coordinator, error)` —
  重放日志恢复；**重开时仍无决定的事务，恢复期间即持久化撤销（abort）决定**。
- `(*Coordinator).Begin(txID string, deltas map[string]int) error` — 持久化 begin 记录。
- `(*Coordinator).Decide(txID string) error` — 准备阶段：**收齐全部 yes 才持久化 commit
  决定**，任一否决或不可达则持久化 abort；决定记录先于任何决定消息落盘。
- `(*Coordinator).Submit(txID, deltas) error` — Begin + Decide + 一次投递尝试。
- `(*Coordinator).Deliver(txID string) bool` — 向未应答的分片（重）投送决定，幂等，
  可反复调用；全部应答后返回 true。
- `(*Coordinator).DecisionOf(txID) (string, bool)` / `(*Coordinator).Done(txID) bool` —
  终态可查询（重开后依然有效）。

### 故障注入链路（`link.go`）
- `Link{P, Down, DropNextReply}` 实现 `ParticipantRPC`：`Down` 模拟不可达；
  `DropNextReply` 模拟"已持久化/已应用但回复丢失"。

## 复现步骤

```sh
go test ./... -v        # 功能测试
go test -race -count=1 ./...   # 竞态检测（已执行，通过）
```

环境：go1.20.2 darwin/arm64。`go vet ./...` 因执行授权限制未运行，特此标注。

## 验证范围（`twopc_test.go`，全部真实断言、真实日志重放）

| 测试 | 场景 | 关键断言 |
|---|---|---|
| `TestCommitAppliesBothShards` | 正常：一笔事务两边加减 | 70/130，决定为 commit，Done |
| `TestVetoLeadsToAbort` | 边界：透支否决 | 决定为 abort，余额不变，双方 aborted |
| `TestReadsSeeOldValueDuringPrepare` | 边界：准备期间读 | 准备后读旧值 100，commit 后 60 |
| `TestRestartWithoutDecisionPersistsAbort` | 交错：协调器 begin 后崩溃 | 重开即持久化 abort，参与者释放待定 |
| `TestPrepareReplyLostAndParticipantRestart` | 交错：准备回复丢失 + 参与者重开 | 协调器 abort；参与者重开后仍 prepared、读旧值（问不到协调器时保留待定）；链路恢复后 abort 落地 |
| `TestDuplicateDeliveryAppliesOnce` | 边界：消息重送 | 重复 commit/prepare 后余额仍 70/130 |
| `TestCommitReplyLostAndSecondShardRecoversLater` | 题面主线交错场景 | 第一片应用后丢回复、协调器中途重开（commit 决定仍在）、第二片稍后从日志恢复；最终两边各应用一次（150/50），终态可再查询 |

所有日志与结果以上述真实测试输出为准；未执行的项目（`go vet`）已明确标注。
