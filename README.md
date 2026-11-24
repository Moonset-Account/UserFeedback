# migrationplanner

发布控制器的协议迁移规划模块（Go 1.20.2，仅标准库）。它在一个静态版本表上搜索
**满足全部约束的最短前进方案**；当目标不可达时，返回可达前沿上被阻断的动作、冲突
调用对、等待中的排空事件或超出回退步数的位置。

## 解决的问题

服务之间的请求/响应编码可以独立演进，两个版本各自单测通过，混跑仍可能断链。本模块
把一次发布拆成「一次只改一项配置或替换一个副本」的原子动作，并在每一步强制执行：

1. **全网格可通**：每条调用边上，所有仍接流量的调用者 × 所有候选接收者逐对兼容
   （请求方向与返回方向分别声明、分别判定）。
2. **旧会话钉住编解码器**：开始前已接单的会话保留其选定的请求/响应编解码对，直到
   输入指定的排空事件被观察到并执行显式排空动作；排空不可逆，回退序列里永不出现排空。
3. **新旧副本并存步数上限**：每个服务声明混合版本布局允许存在的步数。
4. **回退预算**：为每个成功前缀预留一条只含配置/副本动作、按服务计步且在预算内的
   回退序列，并且回退过程中每一步都保持全网格连通。
5. **在途有界双向流等价**：版本对可挂载 FSM（分块请求、半关闭、可重送尾帧）。静态
   版本表允许升级但在途协议不等价时，给出可回放的最短分歧对话，规划器据此绕行或等待。

## 公开 API（包 `migrationplanner`）

### 版本表与布局

- `Spec`：`Services map[string]*Service` + `Edges []*Edge`。
- `Service`：`Versions`、`MaxReplicas`、`CoexistSteps`、`RollbackBudget`、`Target`。
- `Version{Name, Profiles []Profile}`；`Profile{Request, Response Codec}` 是一个副本可
  同时启用的收发编码组合。
- `Edge`：按调用边方向声明 `RequestOK` 与 `ResponseOK`，并可用 `Streams map[VPair]*StreamProtocol`
  给有序版本对挂载流 FSM。
- `Replica{ID, Service, Version, Profile}` 与 `Session`（钉住的旧会话，含 `DrainEvent`）。

### 规划器

- `NewPlanner(spec *Spec, initial map[string]*Replica, sessions []*Session, cfg PlannerConfig) (*Planner, error)`
  校验版本表、初始布局与流 FSM 一致性。
- `(*Planner).Plan() (*Plan, *Frontier)`：
  - 成功返回 `*Plan`，每个 `Step` 带一个 `RollbackReserve`（该前缀的回退动作序列）。
  - 失败返回 `*Frontier`：`Blocked []Blocked`（含 `Reason`、冲突 `Caller/Callee/Edge`、
    可选 `Diverge *Divergence`）、`WaitingEvents`、可达状态采样。
- 阻断原因常量：`ReasonCodec`、`ReasonPinBroken`、`ReasonPinRemoved`、`ReasonCapacity`、
  `ReasonCoexistLimit`、`ReasonRollbackUnreachable`、`ReasonStreamDivergence`、
  `ReasonDrainNotObserved`、`ReasonNoCandidate` 等。
- 原子动作 `Action` 的种类：`ActReplace`、`ActAdd`、`ActRemove`、`ActProfile`、`ActDrain`。

### 有界双向流与过渡期代理

- `StreamProtocol{States, Start, Transitions, Terminal}` + `Build()`：确定性 FSM，
  `Frame{Dir, Kind, Seq, Effect}`，`Kind` 为 `KindData/KindHalfClose/KindTail`。
- `(*StreamProtocol).Compare(other) *Divergence`：带标签的强互模拟检查；首个不一致返回
  `Divergence{Replay []Frame, At Frame, Reason}`，`Replay` 是可在旧 FSM 上逐帧回放的
  最短公共前缀。
- `NewProxy(oldP, newP, oldState) (*Proxy, error)`：只依据已确认帧，为断线旧会话合成
  继续/回退状态映射（在新 FSM 中寻找覆盖旧状态全部允许后续帧的可达状态）。
- `(*Proxy).Accept(f) (effect, replayed, err)`：保住已产生业务结果；已确认尾帧重投时
  返回 `replayed=true` 且不重复处理（精确一次）。
- `MappedState()` / `RollbackState()` / `AppliedEffects()` / `ConfirmedFrames()`。

## 关键样例：加一版只读兼容的过渡副本

`session_test.go` 中的 `transitionalSpec` 构造服务 B 的过渡版本 `vt`，它同时支持旧的
`json/json` 与桥接组合 `proto/json`（能读旧 json 请求、仍以 json 应答）。

- `TestWithoutTransitionalReplicaMigrationIsBlocked`：只有 v1/v2 时，严格方向兼容关系
  下任何单副本替换都会让某个调用者/接收者对断链，前沿给出冲突对。
- `TestTransitionalReplicaEnablesForwardAndRollback`：引入 `vt` 后，最短方案经
  `v1 -> vt -> v2` 前进，每个中间网格全通，每个前缀都带在预算内、不含排空的回退。
- `TestPlusOneTransitionalReplicaWhileDrainPending`：排空未到、钉住端点不可替换时，
  +1 过渡副本仍可上线并保持连通（有界前进），但完整目标需等待排空，前沿明确报告
  `WaitingEvents`。

## 复现步骤

```bash
go version          # 期望 go1.20.2
go test ./...       # 全部功能测试，含真实断言
go test -race ./... # 竞态检测
go test -cover ./...# 语句覆盖率
gofmt -l .          # 应无输出
```

不依赖任何第三方模块，`go.mod` 仅声明 `module migrationplanner` 与 `go 1.20`。

## 验证范围（以真实测试为准）

测试文件与覆盖的验收条件：

- `planner_test.go`
  - 最短前进方案长度与单原子动作语义（`TestShortestPlanTwoServices`）。
  - 全网格不兼容时前沿给出冲突调用者/接收者对（`TestCodecIncompatibilityBlocks`）。
  - 每个成功前缀的回退可实际重放、回到起始布局且全程连通、按服务不超预算、不含排空
    （`TestEveryPrefixHasRollback`）。
  - 回退预算为 0 时阻断（`TestRollbackBudgetExceeded`）。
  - 容量边界与静态表校验（`TestCapacityBoundaryBlocksAdd`、两个 Validation 用例）。
- `session_test.go`
  - 旧会话等待指定排空事件、观察到排空后显式释放（两个 Pinned 用例）。
  - 过渡副本对前进与回退的必要性、+1 副本在排空未到时的有界前进与等待。
  - 并存步数 0 阻断、预算内放行（两个 Coexistence 用例）。
  - 排空与迁移交错时每个中间网格仍全通（`TestInterleavedDrainAndMigrationMesh`）。
- `stream_test.go`
  - 等价 FSM（内部状态不同、标签行为相同）无分歧。
  - 缺半关闭帧的 FSM 返回可回放最短分歧对话。
  - 代理保全允许后续帧序列与业务结果、尾帧精确一次、无覆盖状态时报错。
- `integration_test.go`
  - 静态表可升级但在途流不等价时阻断并携带可回放分歧；排空后同表升级成功（绕行/等待）。
  - 等价在途流不阻断升级。

## 明确的建模边界

- 排空事件通过 `PlannerConfig.ObservedEvents` 声明「已观察到」，但会话钉住在计划内由
  显式 `ActDrain` 释放；未观察到的排空动作会在前沿标记为 `ReasonDrainNotObserved`。
- 回退只比较副本/版本/配置组合的布局键，不复活已排空会话——排空被视为不可逆。
- 搜索受 `PlannerConfig.MaxVisited`（前向）与内部回退探索上限约束，避免状态空间爆炸；
  回退可行性带正/负记忆化与剩余预算下界剪枝。
