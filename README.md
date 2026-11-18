# 本地许可判定模块（permit）

巡检终端在断线期间仍需允许当班人员登录。本模块在终端本地、纯离线条件下，
依据**已验签的许可快照**、**新旧撤销记录**和**可注入的设备时钟**，对
“某人员是否可登录某工区”做出判定。模块本身不做验签、不联网，只消费上游
已验证的数据。

## 实际入口

- 模块路径：`permit`（`go.mod` 中 `module permit`，Go 1.20，无第三方依赖）
- 核心实现：`permit.go`
  - `NewEngine(clock Clock) *Engine`：构造本地判定引擎；`clock=nil` 时回退到设备实时时钟。
  - `(*Engine).IngestSnapshot(Snapshot) error`：接收带编号的可信许可快照。
  - `(*Engine).IngestRevocation(Revocation) error`：接收撤销记录（新旧、乱序均可）。
  - `(*Engine).Login(person PersonID, area AreaID) Decision`：登录判定入口。
  - 辅助只读接口：`Watermark()`、`LatestSnapshotSeq()`、`RevocationSeq()`、`ObserveClock()`。
- 验收测试：`permit_test.go`（Go 内置 `testing`，全部为真实断言，无 mock 框架）。
- 验证命令：
  - `go test ./...`（验收）
  - `go test ./... -race -count=1`（竞态 + 禁用缓存，已通过）
  - `gofmt -l .`（无输出，格式合规）

## 数据模型

| 类型 | 关键字段 | 说明 |
| --- | --- | --- |
| `Snapshot` | `Seq`、`Grants[]` | 带单调编号的许可包；整体替换式携带授权 |
| `Grant` | `Person`、`Area`、`NotBefore`、`ExpiresAt` | 人员+工区的明确授权与有效窗口（起点含、终点不含） |
| `Revocation` | `Seq`、`Person`、`Area` | 撤销记录；`Area` 为空表示人员级撤销 |
| `Clock` | `Now() time.Time` | 可注入设备时钟 |

## 判定与安全规则

1. **断线续用至期限**：无新数据时，仅依据最新可信快照中的授权窗口判定，
   到达 `ExpiresAt` 即拒绝（`ReasonExpired`）；断线期间到达的撤销记录无需
   新快照即可立即阻断登录。
2. **时钟回拨不延长有效期**：引擎维护单调水位
   `watermark = max(所有观测时钟)`。所有窗口判定按水位进行——回拨既不能
   复活已过期授权，也不能把授权倒带回“未生效”状态。
3. **旧快照不改变已知撤销结果**：`Seq <= 已知最新 Seq` 的快照返回
   `ErrStaleSnapshot` 且不修改任何状态；相同编号同样拒绝。
4. **撤销累积、旧记录忽略**：撤销按“人员+工区”分键（空工区为人员级），
   同键只保留最大 `Seq`；乱序到达的更旧记录被静默忽略。
5. **只有明确重新授权才能恢复**：携带授权的快照 `Seq` 必须**严格大于**
   对应撤销 `Seq` 才视为恢复；两者同编号时保守地按撤销优先拒绝。新快照中
   未重新出现的人员/工区授权一律视为无授权（`ReasonNoGrant`）。
6. **输入校验**：非正编号、空人员、`ExpiresAt <= NotBefore` 等返回
   `ErrInvalidSnapshot` / `ErrInvalidRevocation`，且不污染引擎状态。
7. **防御性拷贝**：引擎内部保存快照切片副本，调用方摄入后篡改入参不影响判定。

## 验证范围（交叉约束映射）

测试同时覆盖“可接受结果 / 应被阻止的结果 / 影响两者的边界与交错场景”：

| 分类 | 测试用例 | 覆盖的交叉约束 |
| --- | --- | --- |
| 可接受 | `TestValidGrantAllowsLogin` | 有效快照 + 窗口内 → 允许 |
| 可接受 + 边界 | `TestDisconnectedTerminalWorksUntilDeadline` | 断线续用、期限内允许、终点时刻拒绝、断线期撤销即时生效 |
| 应阻止 | `TestNoSnapshotAndNoGrant` | 无快照拒绝；无明确授权（异工区/异人员）拒绝 |
| 应阻止 | `TestRevocationBlocksLogin` | 工区级撤销阻断登录 |
| 应阻止 | `TestPersonWideRevocationBlocksAllAreas` | 人员级撤销跨工区生效且不牵连他人 |
| 应阻止（交错） | `TestSameSequenceTieGoesToRevocation` | 快照/撤销同编号 → 撤销优先 |
| 交错 | `TestOnlyExplicitRegrantInNewerSnapshotRestores` | 撤销后，新快照不重新授权仍拒绝；严格更大编号中明确重新授权才恢复 |
| 交错 | `TestStaleSnapshotDoesNotChangeKnownState` | 更旧/同编号快照被拒且撤销结果不变 |
| 交错 | `TestOlderRevocationRecordsAreIgnored` | 撤销乱序到达；旧记录不覆盖新记录，重新授权后旧撤销不能再阻断 |
| 边界（回拨） | `TestClockRollbackCannotReviveExpiredGrant` | 过期后回拨时钟，水位不降、授权不复活 |
| 边界（回拨） | `TestClockRollbackDoesNotChangeWindowSemantics` | 回拨到 NotBefore 之前不改变“有效”结论 |
| 边界（窗口） | `TestNotYetValidAndExpirationBoundary` | 未生效拒绝；起点包含、终点排他 |
| 健壮性 | `TestInvalidInputs` | 非法输入返回哨兵错误且不污染状态；空人员请求拒绝 |
| 健壮性 | `TestDefensiveCopyOfSnapshot` | 外部篡改入参不影响引擎 |
| 健壮性 | `TestNilClockFallsBackToWallClock` | 未注入时钟时回退实时时钟且不 panic |

## 范围限制

- 本模块假定所有快照与撤销记录在进入前已完成验签；不包含任何密码学、
  网络通信或真实设备时钟校准逻辑。
- 单调水位只能抵抗“回拨延长有效期”，无法在没有可信时间源的情况下检测
  “时钟冻结在期限之前”——这属于上游可信时间/心跳通道的职责。
- `go vet ./...` 在本次环境的命令权限策略下未获批准运行；已通过
  `go test ./... -race -count=1` 与 `gofmt` 检查替代核验。
