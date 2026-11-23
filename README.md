# recdns — 本地报文夹具驱动的递归 DNS 解析器

一个只依赖 Go 1.20.2 标准库的递归式 DNS 解析器。它不做任何真实网络 I/O：
所有“报文交换”都通过内存中的 `Fixture`（本地报文夹具）完成，夹具按解析
步骤提供父区委派（referral）、别名（CNAME）、正答案以及两类负答案
（NXDOMAIN 与 NODATA）。每条记录都携带其**原区（Origin）**与 **TTL**。

## 设计要点与题目要求的对应关系

| 题目要求 | 实现位置 / 机制 |
| --- | --- |
| 从最长已知后缀委派继续查询 | `Resolver.longestDelegation`：从 QNAME 逐标签向根回退，跳过已过期区切，命中第一个存活委派；过期子区自然回退到父区 |
| 给出使用的服务器序列 | `Fixture.Log` / `Fixture.ServerSequence` 记录每次交换；`Result.Servers` 汇总（含失败重试与换代重启） |
| 最终答案与剩余有效期 | `Result.Answers`、`Result.TTL`（在解析结束时刻计算） |
| 记录带原区和 TTL | `RR.Origin`、`RR.TTL`；负答案的 `SOA.Origin` / `SOA.MinTTL` |
| 别名链结果不能比链上任一记录活得久 | `chainExpiry` 取链上所有 CNAME 的最早到期，`finalTTL` 再与终端记录取最小值 |
| NXDOMAIN 与 NODATA 缓存范围分开 | 负缓存以 `(name, type)` 为键；NODATA 用 `RCodeOK` 标记、NXDOMAIN 用 `RCodeNXDomain` 标记，互不串扰，也不跨类型 |
| 父区过期换代后，旧子区在途答复不得写入新委派缓存 | 每个区切带 `generation`；答复落缓存**之前**校验代际，换代后在途答复返回 `ErrStaleDelegation` 并重启，绝不写缓存 |
| 仍有效的无关区答案继续可用 | 缓存按 `(name,type)` 与区切分别存放；`TestDelegationCutover` 用独立解析器验证 |
| 重复别名返回环上的名称 | `seen` 集合检测 CNAME 环，`Result.Status=StatusLoop`，`Result.Loop` 列出环上名称 |
| 两区别名链 + 同名不同类型 + 负缓存到期前后 + 换代 | `TestTwoZoneAliasChain`、`TestChainSameNameDifferentType`、`TestDelegationCutover` |

### 代际（generation）校验

`InstallDelegation` 每次安装/替换区切都把该 zone 的 `gen` 加一。解析在
发出请求前记下当前 `gen`，响应回来后、任何缓存写入前重新比对：代际不一致
即视为陈旧答复，丢弃并通过 `Resolve` 的重启循环从当前委派集重新开始。

## 公开 API（包 `recdns`）

核心类型与函数（均无网络访问）：

- `NewFixture(handler HandlerFunc, clock func() time.Time) *Fixture`
  - `(*Fixture).Query(Server, Query) (*Message, error)`：执行一次交换
  - `(*Fixture).Gate(server, name, type) *gate`：安装一次性阻塞点，用于确定性地构造并发交错
  - `(*Fixture).Log() []QueryLog`、`ServerSequence() []Server`、`ResetLog()`
- `NewResolver(fx, rootServers, rootTTL, clock) *Resolver`
  - `(*Resolver).Resolve(name Name, typ Type) (*Result, error)`
  - `(*Resolver).InstallDelegation(Delegation)`：安装或换代区切
- 数据类型：`Name`、`Type`（A/AAAA/CNAME/TXT）、`Server`、`RR`、`SOA`、
  `Delegation`、`Message`、`Query`、`Result`
- 结果状态：`StatusOK`、`StatusNXDOMAIN`、`StatusNODATA`、`StatusLoop`
- 哨兵错误：`ErrNoDelegation`、`ErrNoResponse`、`ErrStaleDelegation`（内部重启用）
- 测试/自省辅助：`DelegationGeneration`、`HasCachedRR`、`NegativeCacheEntry`、`CachedRRCount`

`Result` 字段：

- `Status`、`Answers`（终端正答案，含原区）、`CNAMEs`（按遍历顺序的全部别名）
- `Loop`（环上名称）、`Servers`（联系过的服务器序列）、`TTL`（剩余有效期）、`FromCache`

## 目录结构

```
go.mod
recdns/
  types.go          # 基础类型、RCode/状态常量、名称/区工具函数
  fixture.go        # 本地报文夹具、交换日志、一次性并发闸门
  resolver.go       # 递归解析、最长后缀委派、代际校验、正/负缓存、别名链、环检测
  debug.go          # 只读缓存自省辅助
  world_test.go     # 合成 DNS 区树与报文夹具（含故障/闸门编排）
  resolver_test.go  # 9 个验收测试
```

## 复现步骤

要求：Go 1.20.2（本仓库在 `go1.20.2 darwin/arm64` 下验证），无外部依赖。

```sh
go test ./...            # 运行全部测试
go test ./... -v         # 查看每个用例
go test ./... -race      # 竞态检测
go test ./... -count=10  # 重复运行确认稳定性
```

## 验证范围（9 个测试，均含真实断言）

1. `TestTwoZoneAliasChain` — 跨两个不同区（alpha / bravo）的 CNAME 链；
   断言服务器序列、两条 CNAME 的原区、链 TTL 取最短、过期后从最长已知后缀重建。
2. `TestChainSameNameDifferentType` — 同一名称分别查 A 与 TXT；TXT 在终端为
   NODATA，断言负缓存在到期前离线命中、到期后只重查终端区，正答案不受影响。
3. `TestNegativeCacheScopedByType` — NXDOMAIN 与 NODATA 分槽、按类型隔离、
   SOA minimum 决定负缓存 TTL、到期前后行为与精确到期边界。
4. `TestCNAMELoop` — CNAME 环检测，`Loop` 报告环上名称，后续可由缓存离线识别。
5. `TestDelegationCutover` — 用两个闸门把单次解析钉在“父区委派→旧子区在途
   答复”，期间令父区过期并换代；断言旧答复不进新缓存（计数=1）、重启走新
   服务器、无关区答案仍可用、同名另一类型走新委派。
6. `TestTransientFailures` — 传输故障与 SERVFAIL 的跨服务器重试，永久故障
   返回 `ErrNoResponse` 且不被负缓存。
7. `TestTTLExpiryBoundary` — 正答案在到期前 1ns 仍命中缓存、到期瞬间重查。
8. `TestConcurrentResolutions` — 24×2 个并发解析，`-race` 下验证锁正确性，
   热缓存第二轮零报文。
9. `TestNoUsableDelegation` — 根委派过期后返回 `ErrNoDelegation`。

### 边界与故障交错覆盖

- 时间边界：正/负缓存到期前 1ns、到期瞬间、到期后均有断言。
- 并发交错：换代与在途答复的竞态通过闸门确定性复现，而非依赖时序运气。
- 故障恢复：单次/永久传输失败、SERVFAIL、根委派失效。

## 范围限制（明确未做）

- 不实现真实 DNS 线路协议（无 UDP/TCP、无 wire 编解码）；网络由夹具模拟。
- 不做 DNSSEC 校验、QNAME 最小化、大小写保留、EDNS 等扩展。
- 负缓存 TTL 直接采用报文 `SOA.MinTTL`，未再与区 TTL 取 min（夹具语义）。
- 所有数据均为本仓库内合成数据，不访问任何真实主机、账户或外部服务。
