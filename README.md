# 身份会话归属模块（sessionattribution）

应用收到目录停用通知后，用本模块决定哪些已建立的会话必须断开。模块仅依赖
Go 1.20 标准库，无外部依赖、无网络访问。

## 解决的问题与规则

1. **按自然人稳定标识归属，不按账号名归属。**
   匹配一律使用 `PersonID`。账号名（`AccountName`）只作展示信息；账号名回收
   给新员工后，旧员工的停用通知不会影响新员工的会话。
2. **共用终端的工作槽隔离。**
   一个会话内按切换操作人记录保存多个工作槽（`WorkSlot`）。停用某自然人时
   仅挂起其拥有的槽；其他操作人的活动槽保留，会话继续存活。
3. **最后一个活动槽消失才断开会话。**
   会话一旦进入 `SessionTerminated` 即终态：重复通知、更正通知或后续切换都
   不能使其重开。
4. **重复与更正通知更新可见证据。**
   - 重放同一事件 ID：`ObservationCount` 递增、`LastNoticedAt` 刷新；
   - 更正事件（`CorrectionOf`）：追加 `Correction` 记录并可更正停用时间；
   - 两者都只产生 `DecisionEvidenceUpdated`，绝不把已挂起槽位/已终止会话改回
     活动状态。更正先于原通知到达时，原通知随后被并入同一证据链。
5. **既成停用不可逃避。**
   已停用自然人新开会话或切换进入时，新槽在创建时即挂起；无活动槽则会话
   出生即终止。
6. **迟到/回溯通知可见。**
   停用时间早于槽位开始时间时，证据上标记 `Retroactive: true`。

## 公开 API

包名：`attribution`，模块名：`sessionattribution`。

### 构造

| API | 说明 |
| --- | --- |
| `NewEngine() *Engine` | 使用系统时钟创建引擎 |
| `NewEngineWithClock(clockFn func() time.Time) *Engine` | 注入时钟，便于确定性测试交错场景 |

### 会话与槽位

| API | 说明 |
| --- | --- |
| `OpenSession(id SessionID, p Principal) (*Session, []Decision, error)` | 为已认证登录主体建立会话；主体已停用时出生即终止 |
| `SwitchOperator(sessionID SessionID, rec SwitchRecord) (*Session, []Decision, error)` | 记录共用终端的操作人切换，保留旧槽并创建新槽 |
| `Session(id SessionID) (*Session, error)` | 取会话防御性副本，未知 ID 返回 `ErrUnknownSession` |
| `Sessions() []*Session` | 按创建顺序返回全部会话副本 |
| `IsDeactivated(p PersonID) bool` | 查询自然人当前是否处于停用状态 |
| `(*Session).CurrentSlot() *WorkSlot` | 前台槽；会话终止后为 `nil`（自动落到其他活动槽） |
| `(*Session).ActiveSlots() / SuspendedSlots() []*WorkSlot` | 按创建顺序列出活动/挂起槽 |

### 停用事件

| API | 说明 |
| --- | --- |
| `ApplyDeactivation(ev DeactivationEvent) ([]Decision, error)` | 应用目录停用通知，返回确定性有序的决策列表 |

决策按“会话创建顺序、槽位顺序”排列：先 `DecisionSlotSuspended`，紧随其引发的
`DecisionSessionTerminated`，最后是 `DecisionEvidenceUpdated`。

### 关键类型

- `Principal{PersonID, AccountName, IssuedAt}`：已验证登录主体，`PersonID` 为
  稳定锚点。
- `WorkSlot{ID, PersonID, AccountName, State, StartedAt, Evidence}`：操作人工作槽。
- `DeactivationEvent{ID, PersonID, At, NoticeAt, CorrectionOf}`：`At` 为停用生效
  时间，`NoticeAt` 为收到时间（可模拟乱序送达），`CorrectionOf` 非空表示更正。
- `DisconnectEvidence`：挂起槽位上的可见断开证据（事件 ID、时间、观测次数、
  更正链、回溯标记）。
- 哨兵错误：`ErrInvalidEvent`、`ErrUnknownSession`、`ErrEmptyPrincipal`、
  `ErrInconsistentSwitch`，用 `errors.Is`/`==` 比较。

## 运行方法

```bash
go test ./...          # 全部测试
go test -v ./...       # 逐用例输出
go test -race ./...    # 竞态检测
go test -cover ./...   # 覆盖率
gofmt -l .             # 格式检查（无输出即通过）
```

## 验证范围

全部用本地合成数据验证，未联网、未读取任何真实目录或凭据。测试文件
`engine_test.go` 使用可手动推进的固定时钟，共 15 个用例：

| 用例 | 覆盖点 |
| --- | --- |
| `TestOpenSessionAndBasicDeactivation` | 正常场景：单操作人会话停用即断开，证据落槽 |
| `TestSharedTerminalRetainsOtherSlots` | 共用终端：后台槽挂起、前台自动让给其他操作人；最后一槽才终止；切换记录留痕 |
| `TestAccountNameRecyclingDistinguishesPersons` | 账号名回收：稳定 ID 区分新旧员工，旧会话不重开、新会话不误伤 |
| `TestDuplicateNoticeUpdatesEvidenceWithoutReopen` | 重复通知幂等：计数/时间刷新，状态不回退；终止会话拒绝再切换；停用者新会话出生即终止 |
| `TestCorrectionNoticeUpdatesEvidence` | 更正通知：停用时间与更正链可见，状态不回退 |
| `TestCorrectionBeforeOriginalNotice` | 交错：更正先到先建立停用，迟到原通知并入证据链 |
| `TestBackdatedNoticeFlagsRetroactive` | 边界：回溯通知标记 `Retroactive`，事后通知不标记 |
| `TestSessionForDeactivatedPersonBornTerminated` | 边界：停用者新会话出生即终止并附证据 |
| `TestSwitchToDeactivatedOperatorSuspendedAtBirth` | 边界：切换给停用者，新槽出生挂起，前台回退给活动槽 |
| `TestInterleavedSessionsFanOutAndOrdering` | 交错：一条通知扇出到多个会话的匹配槽，无关会话不受影响，决策顺序确定 |
| `TestValidationAndLookupErrors` | 边界：非法事件、空主体、未知会话、不一致切换、重复建会话 |
| `TestReturnedSessionsAreCopies` | 防御性拷贝：外部修改不污染引擎状态 |
| `TestNoticeWithoutMatchingSessionStillRecorded` | 边界：无匹配槽时通知仍产生可见证据更新（重复/更正同样可见） |
| `TestSlotHelpersAndStateText` | 查询辅助方法与状态文本 |
| `TestRepeatedOriginalNoticeNoSlotsFallback` | 边界：同一人第二条原始通知且无槽位时不静默丢弃 |

实测结果（本机 `darwin/arm64`，`go1.20.2`）：

- `go test -count=1 ./...`：PASS
- `go test -race -count=1 ./...`：PASS
- `go test -cover ./...`：93.6% 语句覆盖
- `gofmt -l .`：无输出

未覆盖部分为不可达的防御性兜底分支（如证据链查找同时落空的回退路径）。

## 已知限制

- 引擎面向单 goroutine 使用；并发调用需调用方自行加锁。
- 会话与事件保存在内存 map 中，进程重启不持久化。
- 时间戳使用 UTC/注入时钟比较，不处理时区数据库本身的历史变更。
