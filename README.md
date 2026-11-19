# 截面重建器（Cross-Section Reconstructor）

多个服务各自封存了按本地序号排列的事件前缀。本模块在给定每个服务的最大
可用序号（上限）以及带唯一编号的发送/接收消息后，重建一个**同时成立**的
最大一致截面（consistent cut），并列出截面上已发未收（in-flight）的消息。

## 实际入口

- 模块：`crosssection`（`go.mod`，Go 1.20）
- 实现：`crosssection.go`
  - 入口函数：`func Reconstruct(services []ServiceLog) (*Result, error)`
  - 输入类型：`ServiceLog`（服务名、按 `Seq` 严格递增的 `[]Event`、上限
    `MaxSeq`）、`Event`（序号、类型 `Local/Send/Receive`、消息 ID）
  - 输出类型：`Result`
    - `Cut map[string]int`：每个服务保留到的最大事件序号，`0` 表示全部裁掉
    - `InFlight []string`：发送在截内、接收在截外（或根本没有接收）的消息 ID
    - `MissingSource []MissingReceive`：找不到发送证据的接收，含所在服务与序号
- 测试：`crosssection_test.go`，全部使用标准库 `testing`，断言真实生效；
  辅助函数 `validateCut` 会脱离实现的记账数据，独立复核全部交叉约束。

## 语义与算法

截面用每服务"保留到的最大序号"表示，按序号截断天然满足**前缀约束**。

1. 初始截面取 `min(MaxSeq, 该服务最后一个事件序号)`，负数上限按 0 处理。
2. 建立消息索引：每条消息 ID 至多一个发送，可有多个接收。
3. **来源缺失**：接收在任何服务中都找不到对应发送 → 记入 `MissingSource`，
   并把该服务强制裁到接收之前（`Seq-1`；接收在序号 1 时裁到 0）。
4. **消息约束定点迭代**：保留接收就必须保留对应发送；若发送落在当前截外，
   把接收方裁到接收之前。所有调整只会让截面向后退，因此迭代必然终止；从
   最高上限开始单调收敛，得到逐服务尽量靠后的**唯一最大**一致前缀。一个
   服务退回可能迫使其他服务再次退回（级联），由多轮外循环处理。
5. 汇总 in-flight：发送在截内，而所有接收都在截外或不存在。

`Reconstruct` 仅在输入畸形时返回错误（重名服务、序号非严格递增、重复发送、
收发事件缺少消息 ID、空服务名）；截面被迫退到 0 是合法结果而非错误。

## 验证范围

`go test ./...`（另经 `-race -cover` 验证，语句覆盖率 96.6%，未覆盖部分为
不可达的防御性分支）。用例按题面交叉约束组织：

| 用例 | 覆盖的约束与场景 | 期望结论 |
| --- | --- | --- |
| `TestAcceptableMaximalCut` | 可接受结果：各上限可同时成立；含已发未收消息 | 全部服务保持在上限，in-flight = `[m2]` |
| `TestBlockedCutCascadesBackward` | 应被阻止的结果：发送被上限裁掉，接收方退回，再级联第三个服务 | `A=1, B=0, C=1`；m1 有发送但超上限，不算来源缺失 |
| `TestCascadeRequiresMultipleIterations` | 交错/边界：服务检查顺序使级联需多轮外循环才收敛 | 同上固定点，证明单轮扫描不够 |
| `TestMissingSourceForcesCut` | 来源缺失：无发送证据的接收被标记并裁到之前，且继续级联 | `A=1, B=0`，`ghost` 进入 MissingSource |
| `TestMissingSourceAtFirstSeqCutsToZero` | 边界：孤儿接收位于序号 1 | 截面裁到 0 |
| `TestBoundaryReceiveAndSendAtCap` | 边界：发送与接收恰好都卡在上限 | 截面合法，无 in-flight |
| `TestCapClampingAndEmptyPrefixes` | 边界：上限超出实际事件、空服务、上限为 0 | 上限钳制；空/零服务截到 0 |
| `TestInFlightWhenReceiveCutButSendRetained` | 交错：发送保留而接收被裁；同一夹具下调上限改变结论 | 先无 in-flight，后 `fly` 变为 in-flight |
| `TestMalformedInputsRejected` | 五类畸形输入必须拒绝 | 均返回非 nil 错误 |
| `TestEmptyInput` | 空输入 | 返回全空结果而非错误 |

`validateCut` 对每个结果独立断言：截面不超上限与实际事件范围、截内接收的
发送一定在截内、来源缺失的接收不被保留，并独立重算 in-flight 与缺失清单。

## 本地核验命令

```sh
gofmt -l .          # 已验证：无输出
go test ./...       # 已验证：全部通过
go test -race -cover ./...   # 已验证：通过，覆盖率 96.6%
```

注：本环境中 `go vet ./...` 的执行未获沙箱批准，故未运行；已通过
`gofmt`、编译与上述测试完成核验。全程仅使用本地合成夹具，无网络访问。
