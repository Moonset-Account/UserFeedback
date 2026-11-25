# sourceselect — 多宿主机源地址选择与重建判定

纯 Go 1.20 模块，无第三方依赖、无网络访问。它用本地合成的接口、地址、路由和连接状态，实现题面要求的确定性源地址选择，并通过事件回放区分：

- **选路出网口改变**：新连接的最佳接口/源地址改变；只要原接口和旧源地址仍有效，已建连接不重建。
- **地址仅过首选期**：地址变为 deprecated，但仍在 valid lifetime 内；已建连接保留该源地址且不重建。
- **真正需要重建**：保留的源地址超过 `ValidUntil`、被删除，或原接口退场（删除/down）。

## 运行方法

本机实测工具链：

```text
go version go1.20.2 darwin/arm64
```

`go.mod` 的语言指令为 `go 1.20`；Go modules 不接受 `go 1.20.2` 这种补丁号格式（实测会报 `invalid go version '1.20.2': must match format 1.23`），但代码由 go1.20.2 编译和测试。

在当前工作区执行：

```bash
go test ./...
go test -race ./...
go test -v ./...
go test -cover ./...

# 运行内置事件回放命令
go run ./cmd/replay
```

## 公开 API 概览

核心类型和函数均在包 `sourceselect` 中：

| API | 作用 |
| --- | --- |
| `NewSelector() *Selector` | 创建并发安全的选择状态机。 |
| `UpsertInterface(Interface)` / `SetInterfaceState` / `RemoveInterface` | 维护接口及其 up/down/删除状态；更新接口不会清空其地址。 |
| `UpsertAddress(ifaceID, Address)` / `RemoveAddress` | 维护接口地址、策略级别、作用域和生命周期。 |
| `UpsertRoute(Route)` / `SetRouteState` / `RemoveRoute` | 维护 active/inactive/withdrawn 路由；允许路由先于接口出现，以支持交错事件。 |
| `SelectSource(dst net.IP, at time.Time) (Selection, error)` | 按当前状态和给定时间为新连接选择源。 |
| `OpenConnection(id, dst, at)` | 选择源并保存已建连接。 |
| `RebuildConnection(id, at)` | 按当前状态重新选择并清除 rebuild-needed 标记。 |
| `Evaluate(at)` / `EvaluateChanges(at)` | 检查所有连接；后者额外返回本次新转为 rebuild-needed 的连接。 |
| `EgressDrift(at)` | 对比连接当前保留路径与“如果现在新开连接”会选中的路径；只提示，不改连接。 |
| `Replay(events)` / `NewReplayer().Apply(event)` / `Report()` | 事件回放；每步包含连接快照、重建建议、新增重建和路径漂移。 |
| `DemoEvents()` | 题面场景的固定合成事件。 |
| `CommonPrefixLength(a,b)` / `ParseCIDR(s)` / `DeriveScope(ip)` | 公共辅助函数。 |

主要数据结构：

```go
Address{
    IP, PrefixLength, PolicyLevel, Scope,
    PreferredUntil, ValidUntil,
}
Route{ID, Destination *net.IPNet, InterfaceID, State, Metric}
Connection{ID, Destination, InterfaceID, Source, RouteID, State, RebuildReason}
RebuildRecommendation{ConnectionID, Reason, NewlyDetected, ...}
Event{Time, Kind, Label, Interface, Address, Route, ConnectionID, ...}
```

错误可用 `errors.Is` 判断：`ErrNoRoute`、`ErrInterfaceNotFound`、`ErrInterfaceDown`、`ErrNoUsableAddress`、`ErrConnectionExists`、`ErrConnectionNotFound`、`ErrRebuildUnsuccessful` 等。

## 选择规则

### 1. 路由选接口

`SelectSource` 先在状态为 `RouteActive` 的路由中寻找包含目的地址的前缀：

1. 最长前缀优先；
2. 前缀长度相同时，`Metric` 较小者优先；
3. 仍相同时，按稳定的路由 ID 字典序决定，避免 map 迭代造成抖动。

路由选出的接口必须存在且为 `InterfaceUp`。路由可以先引用尚未录入的接口；此时选择返回 `ErrInterfaceNotFound`，等接口和地址事件补齐后即可成功。

### 2. 地址过滤

在选中的接口上只保留：

- 与目的地址相同的地址族；IPv4-mapped IPv6 按 IPv4 处理；
- 在 `at` 时刻仍未超过 `ValidUntil` 的地址；
- 作用域能够覆盖目标作用域的地址。

作用域数值采用常见 IPv6 scope 编号：node/interface `1`、link `2`、admin `4`、site `5`、organization `8`、global `14`。覆盖规则为 **源作用域级别 >= 目标作用域级别**，即更广的源作用域可以覆盖更窄目标；例如 global 源可用于 link-local 目标，但 link-local 源不能访问 global 目标。未显式设置 `Address.Scope` 时，`DeriveScope` 从 IPv4/IPv6 地址推断。

### 3. 候选排序

候选依次比较：

1. **仍在首选期**：preferred 排在 deprecated 前；
2. **策略级别**：`PolicyLevel` 数值越大越优先；
3. **公共前缀长度**：`CommonPrefixLength(addr.IP, dst)` 越长越优先；
4. **地址稳定性**：按规范化 IP 字节升序，并用 stable sort 保留完全同键元素的稳定顺序。

仅过 `PreferredUntil` 但未过 `ValidUntil` 的地址仍是 deprecated 候选；如果没有 preferred 候选，新连接仍可选择它。

### 4. 生命周期边界

零值 `PreferredUntil` 表示永不变 deprecated，零值 `ValidUntil` 表示永不过期。非零边界采用半开区间：

```text
[创建, PreferredUntil)  => preferred
[PreferredUntil, ValidUntil) => deprecated
[ValidUntil, ...)       => invalid
```

因此在 `at == PreferredUntil` 时已经 deprecated；在 `at == ValidUntil` 时已经 invalid。测试对这两个精确边界都有断言。

## 已建连接保留与重建

连接打开后保存当时的 `InterfaceID`、`Source` 和 `RouteID`。后续普通路由变化不会改写它。

`Evaluate` / 事件回放按以下顺序诊断：

| 情况 | `RebuildReason` | 是否重建 |
| --- | --- | --- |
| 原接口已删除 | `interface-departed` | 是 |
| 原接口状态为 down | `interface-unavailable` | 是 |
| 源地址已从原接口删除 | `source-address-withdrawn` | 是 |
| 当前时间达到 `ValidUntil` | `source-valid-lifetime-expired` | 是 |
| 只达到 `PreferredUntil`，valid 未到期 | 无 | 否 |
| 路由改走其他接口，但旧接口和源地址正常 | 无；仅 `EgressDrift` | 否 |

一旦连接进入 `rebuild-needed`，即使地址被重新添加或接口恢复 up，也会保持待重建标记，直到调用 `RebuildConnection` 成功。重复 `EvaluateChanges` 会继续返回该连接，但只有第一次转换会出现在 `NewRebuilds` 中。

## 事件回放展示的区别

内置合成场景包含 `lan = 10.0.0.0/8` 内网和 `wan = 203.0.113.0/24` 外网，所有 IP 均为文档/合成地址，不产生真实流量。

`ExampleReplay` 会实际执行回放并逐行校验以下输出：

```text
egress-route-change: new-rebuilds=0 C-EGRESS wan/203.0.113.10 -> lan/10.0.0.2
preferred-lifetime-only: new-rebuilds=0 retained=C-LIFETIME=lan/10.0.0.2 best-now=lan/10.0.0.3
open-after-preferred: C-AFTER-PREFERRED=lan/10.0.0.3 preferred=true
source-valid-expiry: new-rebuild=C-LIFETIME/source-valid-lifetime-expired
egress-interface-departed: new-rebuild=C-EGRESS/interface-departed
final: pending=0 C-EGRESS=lan/10.0.0.3 C-LIFETIME=lan/10.0.0.3 C-AFTER-PREFERRED=lan/10.0.0.3
```

含义：

1. `egress-route-change`：新增 `10.0.0.0/8 -> lan` 路由后，新的最佳出口从 `wan/203.0.113.10` 变为 `lan/10.0.0.2`；但 `C-EGRESS` 的步骤快照仍保留 wan 源，`NewRebuilds=0`。
2. `preferred-lifetime-only`：`10.0.0.2` 刚过首选期但仍 valid；`C-LIFETIME` 继续保留它且不重建。由于 `10.0.0.3` 仍 preferred，此刻新开连接会选 `.3`。
3. `source-valid-expiry`：到 `ValidUntil` 后，`C-LIFETIME` 才进入新增重建列表。
4. `egress-interface-departed`：wan 被删除后，`C-EGRESS` 因原接口退场进入新增重建列表；随后显式重建到当前最佳的 lan 地址。

`go run ./cmd/replay` 会输出更详细的逐步 drift、连接快照和最终状态。该输出组装函数 `run(io.Writer)` 由 `cmd/replay/main_test.go` 实际调用，并断言上述关键行均存在。

## 已实现的测试与复核范围

已实际执行：

```bash
gofmt -l .          # 无输出
go test ./...       # 通过
go test -race ./... # 通过
go test -cover ./...
# ok  sourceselect             coverage: 约 72% of statements
# ok  sourceselect/cmd/replay  coverage: 约 82% of statements
```

`go test -v ./...` 覆盖并通过以下测试组：

- 双网主机按内网/默认路由选择 lan/wan；
- 最长前缀、路由 inactive 回退、metric 和稳定路由 ID；
- IPv4/IPv6 地址族过滤、global/link-local scope 过滤；
- preferred 优先于高 policy、policy 优先于公共前缀、公共前缀平分、IP 稳定排序；
- `PreferredUntil` / `ValidUntil` 精确边界；
- deprecated 唯一候选仍可选择，valid 到期后无候选；
- 仅过首选期不重建，valid 到期才重建，待重建状态 latch；
- 路由出口改变但旧接口仍在时不重建，`EgressDrift` 正确；
- 接口 down 与接口删除的重建原因及恢复/latch 行为；
- 路由先于接口、接口先于地址、地址删除后重新添加等交错事件；
- `DemoEvents` 完整回放和 `ExampleReplay` 精确输出；
- cmd/replay 输出关键事件。

## 明确未验证的部分

这是选择/状态判定模块，不操作真实主机：

- 未接入操作系统 netlink、路由表、网卡或真实 TCP socket；
- “重建连接”在 API 中表示重新选择源并给出/清除待重建状态，不发送真实 TCP RST、不执行重连或数据包验证；
- 未访问网络、真实目标或凭据；
- scope 推断和 policy 数值方向是本模块明确文档化的合成策略，不声称覆盖所有操作系统的 RFC 6724 细节。
