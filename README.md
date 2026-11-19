# 内存发送门禁（sendgate）

专网数据报服务的内存发送门禁：在确认对端返回地址（路径验证）之前，
限制响应流量最多为该路径上**实际收到的唯一数据报字节数的三倍**。

## 模块信息

- Go 版本：1.20.2（`go.mod` 声明 `go 1.20`，仅依赖标准库 `errors`/`testing`）
- 模块名：`sendgate`
- 产物（全部位于本工作区）：
  - `gate.go`：门禁实现
  - `gate_test.go`：测试传输层（`fakeTransport`）与验收用例
  - `go.mod`：模块定义

## 实际入口（公开 API）

| 入口 | 作用 |
| --- | --- |
| `New(headerSize int64) *Gate` | 构造门禁，`headerSize` 为每包固定包头字节数，拼包时整体计入且只计一次 |
| `OnReceive(conn, addr, datagramNumber, size) (counted bool)` | 入站数据报解析事件；同路径同编号只入账一次，首次返回 `true`，重复返回 `false` |
| `OnVerified(conn, addr) error` | 路径验证回执；验证后额度限制解除，未知路径返回 `ErrUnknownPath` |
| `TrySend(conn, addr, number, responseSizes) (bool, error)` | 原子申请发送：整包大小 = 包头 + 各响应之和；额度不足整包拒绝（一字节不预留），在途包号复用返回 `ErrPacketInflight` |
| `OnSendResult(conn, number, success) error` | 发送最终结果；按包号登记的**原路径**归属，明确失败才归还预留，成功只核销在途记录，未知包返回 `ErrPacketUnknown` |
| `Report() []PathReport` / `ReportPath(conn, addr) (PathReport, bool)` | 每条路径的剩余额度、已预留字节、唯一已收字节、验证状态及实际获准包号 |

`PathReport.Remaining` 在已验证路径上返回哨兵值 `UnlimitedRemaining`（-1）。

### 关键规则落地

1. **三倍额度**：未验证路径 `limit = 唯一已收字节 × 3`，`remaining = limit - 已预留字节`。
2. **解析去重**：每条路径独立维护数据报编号集合；连接换地址后新路径从零开始，同一编号在新路径仍可入账一次。
3. **拼包原子预留**：多个响应与固定包头一次性整体预留；发不下整包拒绝，不登记在途、不占额度、不记获准包号。
4. **仅明确失败归还**：成功确认不归还额度（字节确实已发出）；失败确认整包字节归还原路径。
5. **换地址归属隔离**：路径键为 `(连接, 地址)`，旧路径状态保留；迟到的发送确认按包号原路径归属，旧路径验证回执不作用于新路径。

## 验证范围与用例

测试传输层 `fakeTransport` 独立维护连接、当前返回地址、换地址、唯一数据报
编号集合与字节统计，并在每个解析事件上与门禁结果做交叉断言。

运行方式（本工作区内）：

```bash
go test ./... -v          # 全部验收用例
go test -race ./...       # 竞态检测（已通过）
go test -cover ./...      # 语句覆盖率（实测 92.6%）
```

| 用例 | 覆盖的交叉约束 | 可接受 / 被阻止 / 边界 |
| --- | --- | --- |
| `TestUnverifiedQuotaAcceptBlockAndBoundary` | 三倍额度、整包预留、失败归还、成功不归还、获准包号序列 | 可接受（350/450）、被阻止（110>100 且额度不变）、边界（恰好占满额度仍获准） |
| `TestDuplicateDatagramParsedOnce` | 同一数据报多次解析只入账一次，与传输层独立统计交叉核对 | 重复事件被阻止；两个唯一编号可接受 |
| `TestBundledReservationAtomic` | 多响应拼包连同包头整体预留、整包拒绝、零响应包边界、零包头配置 | 可接受（恰好占满）、被阻止（差 1 字节整包拒绝）、边界（零响应只占包头、零包头占 0 字节） |
| `TestOnlyExplicitFailureRefunds` | 仅明确失败归还、在途包号不可复用、未知/重复确认拒绝 | 成功不归还（阻止误用）、失败归还（可接受）、重复确认被拒绝 |
| `TestMigrationRouting` | 换地址新路径从零、迟到确认归原路径、验证回执路径隔离、`Report` 保留旧路径 | 新路径无收包发送被阻止、迟到失败只归还旧路径、旧路径验证不提升新路径（交错场景） |
| `TestVerificationLiftsLimit` | 验证解除限制、验证后收包仍累计、未知路径验证拒绝 | 同一包验证前被阻止、验证后可接受 |
| `TestInterleavedReservationAndReceive` | 收包与预留交错、重复收包不抬额度、逐包失败归还 | 边界（额度恰好占满）、重复事件被阻止、失败归还可接受 |
| `TestSendWithoutReceiveRejected` | 无入站数据报路径额度为零、未获准包确认被拒绝 | 全部发送尝试被阻止 |

### 已知验证限制

- 本环境中 `go vet ./...` 需要额外授权，未在本次运行中执行；已通过
  `gofmt -l`（无输出）、`go test ./... -v`（8/8 通过）与
  `go test -race`（通过）完成核验。
- 未覆盖项主要为防御性不可达分支（如在途包引用的路径被删除），正常
  API 调用序列无法触达。
