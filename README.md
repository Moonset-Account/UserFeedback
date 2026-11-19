# mantrap —— 双门互锁门禁通行模块

内外两扇门共用一个中间区（mantrap / airlock）。本模块实现“按凭证判定”的
通行控制：凭证只可从登记所在区发起有方向的过门，位置仅在过门回执被接受后
改变；一扇门放行期间另一扇门互锁；中间区容量为一。

- 语言/工具链：Go 1.20.2（`go.mod` 声明 `go 1.20`，仅用标准库）
- 模块路径：`mantrap`
- 无任何外部依赖，无网络访问

## 物理布局与方向

```
外区 Outer —— 外门 OuterDoor —— 中间区 Middle(容量=1) —— 内门 InnerDoor —— 内区 Inner
```

- `Inward`（向内）：Outer→Middle（外门）、Middle→Inner（内门）
- `Outward`（向外）：Middle→Outer（外门）、Inner→Middle（内门）

## 实际入口

实现文件：`controller.go`；验收测试：`controller_test.go`。

| 入口 | 作用 |
| --- | --- |
| `New()` / `NewWithCapacity(cap)` | 构造控制器；`New` 默认中间区容量为 1，外/内区不限容量 |
| `RegisterCard(card, zone)` | 凭证登记所在区，凭证只能从该区发起通行 |
| `Request(card, dir) (Snapshot, error)` | 发起一轮有方向过门；放行后在目标区**暂占名额**，并置互锁 |
| `NotifyDoorOpened(door)` | 门已打开事件：轮次进入 `pending`，凭证与名额保持待核对 |
| `NotifyDoorTimeout(door)` | 放行后超时未开门：**归还暂占名额**、解除互锁，轮次置 `timed_out` |
| `NotifyDoorClosed(door)` | 关门确认：物理门锁定，但未收到回执前轮次仍 `pending`，互锁不解除 |
| `SubmitReceipt(roundID, Receipt)` | 提交有方向过门回执；**位置只在此处改变**；相同回执重送幂等 |
| `AdminResolve(roundID, confirm)` | 管理员纠正有疑义通行：`false` 撤销并同时恢复位置与空余名额 |
| `CardZone` / `ZoneUsed` / `ZoneCapacity` / `DoorLocked` / `RoundSnapshot` | 状态查询 |

关键拒绝错误（`errors.Is` 判定）：`ErrWrongZone`、`ErrBadDirection`、
`ErrCardBusy`、`ErrDoorInterlocked`、`ErrZoneFull`、`ErrRoundState`、
`ErrStaleReceipt`、`ErrReceiptMismatch`，以及未知卡/门/区/轮次错误。

## 规则到实现的对应

1. **凭证只可从登记所在区发起**：`cardZone` 同时是登记位置与当前位置；
   `Request` 用当前位置 + 方向解析合法门与目标区，非法方向返回
   `ErrBadDirection`；位置只随有效回执（`completeRound`）或管理员纠正变化。
2. **互锁**：以“是否存在未完结轮次”（`activeRound`）为准，而不是仅看物理
   门态。因此关门确认后、传感器未确认的待核对期间，另一扇门依然互锁；
   容量约束优先于互锁判定，拒绝原因确定。
3. **暂占名额**：`Request` 成功即在目标区记一个 reservation；`zoneUsed`
   统计“实际在区凭证 + 暂占名额”。完成时 reservation 转实际占用（总量不
   变），超时/撤销时归还。
4. **超时未开门**：仅 `released` 阶段允许超时；名额归还、互锁解除。
5. **门已开未确认**：轮次 `pending`，凭证留源区、名额继续暂占，直到回执
   或管理员介入。
6. **回执语义**：三要素（卡/门/方向）必须与本轮一致，否则
   `ErrReceiptMismatch`；相同回执重送幂等；已关闭轮次（超时/撤销）的回执
   返回 `ErrStaleReceipt`，上一轮回执不能替新一轮过门。
7. **管理员纠正**：仅 `pending` 轮次可纠正；撤销后凭证留源区、名额归还、
   互锁解除，位置与空余名额一起恢复一致。

## 验证范围与运行方式

```
go test ./...            # 验收（全部为真实断言，无占位测试）
go test -race -count=1 ./...
```

测试场景（10 个测试函数，含子测试；`-race` 下连跑通过）：

| # | 场景 | 类型 |
| --- | --- | --- |
| 1 | 单卡 Outer→Middle→Inner 全旅程，逐步核对位置/名额/互锁 | 可接受结果 |
| 2 | 未登记凭证、与所在区不匹配方向被拒，且不留占用 | 被阻止 |
| 3 | 容量一中间区两卡竞争：第二张始终 `ErrZoneFull`，第一张离开后才放行 | 被阻止（题面指定） |
| 4 | 200 轮并发同时请求，每轮恰好一成一拒、名额守恒（`-race`） | 交错/边界 |
| 5 | 超时未开门归还名额；重复超时、超时后迟来回执被拒；门开后超时非法 | 边界 |
| 6 | 关门后待核对仍互锁（容量 2 独立验证）；管理员撤销后位置与名额一致恢复 | 边界 + 管理员纠正（题面指定） |
| 7 | 错门/错向/错卡回执拒绝；相同回执幂等重送；旧回执不能替新一轮 | 回执交错 |
| 8 | 关门后迟来的有效回执仍可完成；已完结轮次拒绝管理员再纠正 | 边界交错 |
| 9 | 容量 2 下名额充足但另一扇门在用：互锁独立于容量生效 | 交叉约束 |
| 10 | 同一卡上一轮未完结时 `ErrCardBusy`，完结后可发起下一轮 | 被阻止/边界 |

## 已知限制

- 区域与门为固定三区两门模型；时间戳/物理超时计时由调用方通过
  `NotifyDoorTimeout` 驱动，模块本身不持有计时器，便于确定性测试。
- 所有状态在内存中，进程重启不保留；所有方法带互斥锁，可并发调用。
