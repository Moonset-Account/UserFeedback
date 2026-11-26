# netresolve —— 离线名字解析 / 路由求值模块

设备报障现场常常只有本地解析快照、搜索域配置和每条接口的路由范围，
没有可用的真实 DNS。`netresolve` 在**完全离线、零网络访问**的前提下，
复算一个应用绑定在给定时刻实际会挑到哪些地址、按什么顺序尝试、
以及每个地址为何被跳过。

- 语言 / 工具链：Go 1.20.2（`go.mod` 声明 `go 1.20`），仅依赖标准库
  （`net/netip`、`time`、`sort`、`strings`、`fmt`、`errors`）。
- 不读取任何凭据，不发起任何网络请求，不安装第三方依赖。

## 公开 API

唯一入口函数：

```go
func Evaluate(snap Snapshot, binding AppBinding, now time.Time) (AppPlan, error)
```

### 输入

| 类型 | 作用 |
| --- | --- |
| `Snapshot` | 本地解析快照：`Answers`（接口 → 查询名 → 地址族 → 应答列表）与 `Interfaces`（接口状态 + 路由前缀范围） |
| `AppBinding` | 一个应用的绑定：`AppID`、`Host`、绑定接口列表、`SearchDomains`、`PreferIPv6` |
| `Answer` | 单条解析结果：`Kind`（正向 / 负缓存 / 超时）、`Family`、`Source`、`CNAMEChain`、`Records`、`ObtainedAt`、`ExpiresAt` |
| `AddrRecord` | 地址记录：`netip.Addr`、记录级 TTL、`Scope`、链路本地 `Zone` |
| `Interface` | 接口 ID、`Up` 状态、`Routes []netip.Prefix` 路由范围 |

关键语义：

- **查询来源可追溯**：每条应答带 `QuerySource{InterfaceID, QueryName, Original}`，
  搜索域展开后才得到的地址会保留实际查询名与原始名字。
- **应答有效期**：整体有效期取 CNAME 链各跳 TTL 与地址记录 TTL 的**最早值**，
  都缺失时退回顶层 `ExpiresAt`；`now == expiry` 即视为过期（左闭右开）。
- **未来应答不可见**：`ObtainedAt > now` 的应答（接口切换后才到达的新地址表）
  不会被当前时刻消费，候选会带 “not yet visible” 跳过说明。
- **作用域地址保护**：链路本地等本地作用域地址只附加到本地地址上，
  且只能由其 `Zone`（缺省兜底为来源接口）指向的正确接口消费；
  即使该接口拥有默认路由，作用域不匹配也先于路由判定被拒绝。
- **路由判定**：Go 1.20 的 `netip.Prefix.Contains` 对带 zone 地址恒为 `false`，
  模块在路由匹配前剥离 zone，由作用域检查单独负责接口归属。

### 输出 `AppPlan`

- `Status` 总体结论，区分题面要求的四类情形：
  - `StatusNoAnswer`：查询无应答（超时 / 完全无快照记录，且无有效负缓存）
  - `StatusNegativeCached`：负缓存仍有效（优先级高于过期正向记录）
  - `StatusNoRoute`：解析得到地址，但全部因接口下线 / 作用域不匹配 / 无路由而不可用
  - `StatusStale`：只剩已过期的正向地址表（区别于“从未解析到”）
  - `StatusOK`：至少存在一个可尝试候选
- `Candidates []Candidate`：每个地址带 `Rank`（可尝试候选从 1 开始的尝试位次，
  被跳过者为 0）、`Addr`（本地地址含 zone）、`Family`、`Scope`、`Source`、
  `ExpiresAt`、`Skip` 与人类可读的 `SkipNote`。
  跳过原因：`SkipExpired` / `SkipInterfaceDown` / `SkipScopeMismatch` /
  `SkipNoRoute` / `SkipDuplicate`。
- `NameTrials []NameTrial`：每个尝试过的查询名保留正向应答（含过期的）、
  负缓存（标注是否仍有效）、超时、无记录（Miss）证据。

### 候选尝试顺序

1. 可尝试候选与跳过候选分组，可尝试的整体在前；
2. 族内按 RFC 6724 风格的简化源地址优先（全球单播 > 私有/本地 > 其他）、
   来源接口、查询名、晚过期优先、地址字典序排序，保证结果完全确定；
3. 两族之间按 Happy Eyeballs 风格归并交错，由 `PreferIPv6` 决定谁先出，
   一族耗尽后另一族补齐；
4. 同一地址（地址 + zone）多来源只尝试一次：若先见路径可尝试，后续来源标记
   `SkipDuplicate`；若先见路径被跳过而后来的路径可尝试，则降级先见者、
   保留可用路径，避免遍历顺序错误抑制真实可达地址；两条路径都不可用时
   保留先见者的原始跳过原因。

## 运行方法

```sh
# 编译检查
go build ./...

# 全部测试（含竞态检测与覆盖率）
go test ./... -race -cover

# 生成覆盖率明细
go test ./... -coverprofile=coverage.out
go tool cover -func=coverage.out
```

## 验证范围

测试全部使用合成的固定时间快照（基准 `2026-11-27T10:00:00Z`），
无任何外部依赖，语句覆盖率 100%。覆盖点：

| 测试 | 验证内容 |
| --- | --- |
| `TestHappyEyeballsOrderingAndProvenance` | 双族交错顺序、IPv6/IPv4 偏好开关、Rank 连续性、查询来源回填 |
| `TestScopedLocalAddressEnforcement` | 本地作用域地址仅正确接口可消费、跨接口 ScopeMismatch、无 IPv6 路由时 NoRoute |
| `TestScopedLocalBeatsDefaultRoute` | 即使存在默认路由，作用域不匹配仍先于路由判定被拒绝 |
| `TestNegativeCacheTimeoutNoAnswer` | 负缓存有效 / 过期后退化为无应答 / 查询超时 / 完全无记录四种区分 |
| `TestNegativeCachePrecedenceOverStale` | 有效负缓存与过期正向记录并存时的状态优先级 |
| `TestSearchDomainExpansion` | 搜索域顺序、命中即短路、查询来源保留、FQDN 不展开搜索域 |
| `TestInterfaceSwitchTimeline` | 接口切换前后两张地址表：未来表不可见、下线、分族 TTL 交错过期、最终 Stale |
| `TestCNAMEChainTTL` | CNAME 链任一跳 TTL 先到期使整条应答失效、过期时刻边界 |
| `TestDedupAcrossInterfaces` | 跨接口同地址去重，重复候选不占 Rank |
| `TestDedupPromotesUsablePath` | 先见路径无路由、后见路径可用时，去重提升可用路径 |
| `TestDedupBothPathsUnusable` | 两条路径都不可用时保留先见者原始跳过原因 |
| `TestNoRouteStatus` | 解析成功但路由范围不覆盖 → `StatusNoRoute` |
| `TestInterfaceDownAndValidation` | 接口下线跳过与三类输入校验错误 |
| `TestSourcePreferenceAndTieBreaks` | 源地址优先级与 TTL / 字典序并列裁决 |
| `TestDefensiveBranches` | 空搜索域、无记录正向应答、过期/未来本地地址 zone 兜底、多播地址 |
| 白盒测试组 | `compareSource` / `sourcePreference` / `sortFamily` / `liveAt` / `mergeInterleave` 的边界分支 |

## 目录结构

```
go.mod
netresolve/
  types.go             # 输入输出类型、作用域/有效期/路由匹配等基础逻辑
  evaluate.go          # Evaluate 求值引擎：查询名展开、分类、判定、排序、状态汇总
  evaluate_test.go     # 端到端场景测试
  whitebox_test.go     # 内部确定性逻辑的白盒分支测试
```
