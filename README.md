# connpool — HTTP/2 跨域连接复用选择器

两个站点共用同一网关与证书时，客户端希望把它们的请求放在同一条 HTTP/2
连接上。本模块只做**连接选择**：输入本地握手记录，输出某个域能否复用某条
连接、还是必须新建；并把 421 的作用范围限制到单个「域 × 连接」组合。

## 入口

- Go 模块：`module example.com/connpool`（`go.mod`，Go 1.20）。
- 唯一公开入口是包 `connpool`（根目录）：

| 符号 | 作用 |
| --- | --- |
| `connpool.New(Options)` | 构造池，必须提供 `Resolver` 与 `Zones`；可选 `Dialer` |
| `(*Pool).AddConnection(HandshakeRecord)` | 登记一条本地握手记录（协议、已核验域名、远端地址、隔离区） |
| `(*Pool).Acquire(domain) (*Stream, Decision, error)` | 为一个域选择连接或新建连接 |
| `(*Pool).Handle421(connectionID, domain)` | 只撤销该（域，连接）组合 |
| `(*Pool).ConnectionInfo()` | 快照：每条连接的地址、域名、活动流数、已撤销域名 |
| `(*Stream).End()` | 结束一条流；不影响同连接上的其他流 |

`Decision` 是记录在案的选择依据：`Action` 为 `reuse`/`dial`，`Resolved` 是本
次实际使用的解析结果，`Skipped` 逐条列出被跳过的连接及原因
（`protocol_not_http2`、`certificate_mismatch`、`address_mismatch`、
`zone_mismatch`、`revoked_by_421`）。`Decision.String()` 直接可作审计行。

### 复用判定（全部满足才复用）

1. 连接协商到 HTTP/2（`h2`）；
2. 证书已核验域名覆盖目标域（`*.example.com` 只覆盖恰好一层子域，
   不覆盖 `example.com` 也不覆盖 `a.b.example.com`）；
3. 连接实际远端地址仍在**本次**解析结果内（同 IP；两侧都带端口时端口也须一致）；
4. 连接隔离区等于该域的隔离区（无隔离区的域直接拒绝，不会与其他域合并）；
5. 该（域，连接）未被 421 撤销。

`Acquire` 每次都会重新调用 `Resolver`，因此地址轮换后立即用新值评估下一次
申请，无需额外刷新步骤。选择可复用的连接时取活动流最少者（并列按连接 ID），
把并发流摊开。

`Dialer` 返回的握手记录会经过与复用完全相同的校验收紧：协议、证书、地址、
隔离区任一不符则拒绝并返回对应错误，连接也不会进入池。

## 验证

测试与实现都在同一个包里，命令：

```sh
go test ./...
go test -race ./...
```

验收用例（`pool_test.go`，全部为真实断言）：

| 用例 | 覆盖的需求 / 交叉约束 |
| --- | --- |
| `TestThreeDomainsShareCertificateThenPerDomain421` | 同证书三个域共用一条连接；对 b 的 421 只撤销 b×conn-1，a/c 继续复用；b 新建连接；已开的流不受影响且可正常 `End`；对 conn-b 的 421 再撤销新的组合，仍未波及 a |
| `TestReuseRequiresAllThreeConstraints` | 证书 / 地址 / 隔离区 / 协议四个条件各自单独失败都强制新建；可接受结果（全部满足则复用、不触发 Dialer）与被拒结果并存 |
| `TestAddressRotationIsEvaluatedPerDomain` | 部分轮换：a 迁移地址后触发新建，b 仍用旧地址继续复用；b 随后轮换并复用 a 的新连接；在新连接上的 421 只撤销 b×该连接 |
| `TestWildcardCoverageBoundary` | 证书覆盖边界：`*.example.com` 覆盖一层子域，不覆盖裸域与两层子域 |
| `TestDialResultIsValidated` | 新建连接的握手结果被同一套约束校验：证书、地址、隔离区、协议、空记录 ID 各自拒绝且不入池 |
| `TestAddressPortBoundary` | 地址比较边界：端口不一致视为不在解析集合；裸 IP 解析可匹配带端口的远端 |
| `TestBookkeepingAndInvalidInputs` | 构造/登记/撤销/解析的拒绝路径（无 resolver、无 zone 策略、重复连接 ID、空域、无地址、无 dialer） |
| `TestConcurrentAcquireIsConsistent` | 32 路并发申请全部复用同一连接，活动流计数精确并在结束后归零 |
| `TestDecisionStringIsStable` | 审计行包含域名、隔离区、动作、连接、解析地址与跳过原因 |

实际运行结果：`go test ./...` 与 `go test -race ./...` 均通过。
`go vet ./...` 在本次环境中未获执行许可，未作为验证依据。

## 范围与边界

- 只处理选择与记账，不建立真实 TLS/HTTP2 连接；`Dialer`/`Resolver` 由调用方注入。
- `Pool` 用互斥锁串行化选择；文档已注明回调（Resolver/Dialer）不得重入同一个池。
- 421 撤销是永久的（进程内有效），没有自动冷却/重试计时；如需恢复由调用方重建连接或新增池。
- 地址解析限定为 IP 字面量（可带端口），不做 DNS 名称解析。
- 全部逻辑与夹具在本地、无网络访问。
