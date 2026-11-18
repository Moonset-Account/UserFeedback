# 组播准入模块（multicastadmission）

Go 1.20.2，仅依赖标准库（`net`、`sync`、`sort`、`errors`、`testing`）。

## 要解决的问题

隔离网络中的多台终端可订阅同一个组播组，但每台终端允许的发送源不同。
本模块接收每台终端本次接入的：

- 订阅（组播组 + 终端自己的源地址条件 `SourceCondition`）；
- 源地址条件（显式 Include 列表，未列出的源绝不送达该终端）；
- 网段策略 `SegmentPolicy`（允许前缀、拒绝前缀、受保护源清单）。

并满足以下交叉约束：

1. **上游订阅合并**：同一组播组的上游接收范围取各终端**实际准入**源的并集，
   避免重复向上游请求流量（`GroupReport.UpstreamSources`）。
2. **终端间隔离**：转发给终端前仍按该终端**当前**规则独立判定。另一台终端
   加入带来的新源不会扩大本机资格——该源会出现在本机 `Rejected` 中，原因是
   `not_requested`。
3. **会话级影响面**：`Update` / `Leave` 只影响对应终端的会话，其余终端视图与
   合并后的上游范围即时重算。
4. **紧急撤回立即生效**：受保护源被 `WithdrawSource` 撤回后，撤回判定优先于
   一切规则（撤回 → 本机条件 → 网段拒绝 → 网段允许），立即从所有已合并的上游
   订阅和所有终端视图中移除。非受保护源的撤回请求被拒绝（`ErrNotProtected`）。
5. **故障关闭（fail closed）**：网段允许前缀为空时，任何源都不准入。
6. **拒绝优先于允许**：同一源同时命中允许与拒绝前缀时，拒绝生效。

## 实际入口

包路径：`multicastadmission`，实现位于 `admission.go`。

| 入口 | 作用 |
| --- | --- |
| `NewController() *Controller` | 创建准入控制器（内部加锁，可并发使用） |
| `SetPolicy(segmentID, *SegmentPolicy)` | 注册/替换网段策略，立即生效 |
| `Join(terminalID, segmentID, group, SourceCondition)` | 建立终端会话（组地址必须是组播地址，网段必须已注册策略） |
| `Update(terminalID, group, SourceCondition)` | 仅替换本会话源条件；换组返回 `ErrGroupMismatch`，不产生任何变更 |
| `Leave(terminalID)` | 退出会话，只影响该终端 |
| `WithdrawSource(net.IP)` / `RestoreSource(net.IP)` / `IsWithdrawn(net.IP)` | 受保护源紧急撤回 / 恢复 / 查询 |
| `Report() []GroupReport` | 全部组播组的当前上游接收范围 + 每终端实际发送源集合（深拷贝） |
| `GroupReportFor(group) *GroupReport` | 单个组播组的报告（深拷贝） |
| `ParseSegmentPolicy(allowed, denied []string, protected []net.IP)` | 用 CIDR 文本构造网段策略的便捷构造器 |

判定结果：

- `TerminalView.AllowedSources`：该终端**实际可接收**的发送源集合；
- `TerminalView.Rejected`：每个未送达候选源的决定性原因
  （`withdrawn` / `not_requested` / `segment_denied` / `not_in_allowed_segment`）；
- `GroupReport.UpstreamSources`：该组当前合并后的上游接收范围。

## 验证范围与验收用例

测试文件：`admission_test.go`，全部使用 Go 内置 `testing` 编写真实断言。

| 测试 | 覆盖的交叉约束 | 性质 |
| --- | --- | --- |
| `TestMergedUpstreamButTerminalIsolation` | 上游合并为并集；T2 加入的源不会扩大 T1 资格（可接受结果 + 隔离断言） | 可接受结果 |
| `TestEmergencyWithdrawalOverridesMergedUpstream` | 撤回立即覆盖已合并上游与所有终端；非受保护源撤回被拒；恢复后回到常规判定 | 应被阻止/拒绝的结果 |
| `TestSegmentPolicyBoundaryAndDenyPrecedence` | `/24` 首尾地址边界、越界地址、拒绝前缀优先、空允许列表故障关闭 | 边界场景 |
| `TestUpdateAndLeaveScope` | 更新/退出只影响本会话；跨组更新被原子拒绝；其他终端不受影响 | 交错场景 |
| `TestInvalidOperationsRejected` | 空 ID、非组播地址、未知网段、nil 源、未知会话、重复加入、非法 CIDR | 拒绝路径 |
| `TestDistinctGroupsStayIndependent` | 不同组播组的上游范围与终端视图互不串扰 | 隔离 |
| `TestReportReturnsDefensiveCopies` | 返回报告为深拷贝，外部篡改不污染控制器内部状态 | 状态安全 |
| `TestConcurrentInterleaving` | 12 终端并发加入/报告/退出与撤回/恢复交错，`-race` 下保持一致 | 并发交错 |

### 本地验证命令

```bash
go test ./...          # 验收（当前结果：PASS）
go test -race -v ./... # 竞态检测 + 详细输出（当前结果：8/8 PASS）
gofmt -l .             # 格式检查（当前结果：无输出，即全部合规）
```

说明：`go build` 与 `go vet` 在本次沙箱中未获执行授权；`go test` 本身会完成
编译与链接，因此构建正确性已由测试运行间接验证。所有产物仅位于当前工作区，
未联网、未访问任何真实目标，全部夹具为本地合成的文档地址（`10.0.0.0/24`、
`239.0.0.0/8` 组播段）。
