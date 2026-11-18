# forwardguard

受管会话内跳板转发通道的启用判定模块。它不发起任何真实网络连接，只依据
会话批准的转发方向、目标资源、监听网段以及绑定适配器返回的**实际**地址与
端口，输出可启用通道与被阻止的具体边界。

## 入口

模块路径由 `go.mod` 声明为 `forwardguard`，Go 版本 1.20，无第三方依赖，
仅使用标准库 `net`/`testing` 等。

代码入口（`forwardguard.go`）：

| 类型/函数 | 作用 |
| --- | --- |
| `NewEvaluator(policy SessionPolicy)` | 用初始批准策略构造判定器，并校验策略自洽性（如临时回环批准不得包含非回环网段） |
| `(*Evaluator).Submit(Application)` | 登记一条转发申请（方向、目标资源、目标 IP/端口、静态或动态端口） |
| `(*Evaluator).RegisterBinding(id, BindingResult)` | 登记绑定适配器返回的实际监听地址与端口；同一通道的多个 socket 必须共用一个端口 |
| `(*Evaluator).Tighten(next SessionPolicy)` | 在确认前收紧监听策略；只允许收窄，任何扩大批准面（含取消回环限制）的尝试返回 `ErrTightening` 且旧策略继续生效 |
| `(*Evaluator).Confirm() Decision` | 以**当前生效策略**逐条复核全部待启用通道，返回 `Enabled` 与 `Blocked` |

`Decision.Blocked` 中每条 `BlockedChannel` 都带一个或多个 `BlockReason`，
包含 `ReasonCode`、可读 `Detail`、被越过的 `Boundary`、观测值 `Actual` 与
地址族 `Family`，便于安全侧定位具体边界。

## 判定规则

1. **方向**：申请方向必须在当前策略的 `AllowedDirections` 内，否则
   `direction_denied`。
2. **目标资源**：目标名必须存在（`target_unknown`），目标 IP 必须落在该
   资源批准 CIDR 内（`target_out_of_range`），目标端口必须落在批准端口
   范围内（`port_not_approved`）。
3. **静态端口**：实际绑定端口必须等于申请端口，否则
   `static_port_mismatch`。
4. **动态端口**：端口只对同一次申请有效。最早提交并取得该端口的动态申请
   拥有它；其它申请复用同一端口得到 `dynamic_port_reused`。
5. **监听网段**：每个实际绑定地址都必须落在批准监听 CIDR 内，否则
   `listener_out_of_range`（并标注 ipv4/ipv6）。
6. **临时回环 + 双栈**：`TemporaryLoopback` 生效时，若一条通道同时绑定
   IPv4 与 IPv6，且任一地址族出现通配地址（`0.0.0.0`/`::`）或非回环地址，
   即视为该族扩大了可达范围，整条通道以 `dual_stack_expansion` 阻止；
   两族都绑定在具体回环地址（`127.0.0.1` + `::1`）则可启用。单族通配不
   走该码，而走 `listener_out_of_range` + `loopback_only_violation`。
7. **策略收紧复核**：确认返回前调用 `Tighten` 后，`Confirm` 不缓存任何
   旧结论，而是用收紧后的策略重新检查全部待启用通道。收紧只能移除方向、
   缩小目标 CIDR/端口范围与监听 CIDR、提高 `Revision`；回环限制一旦生效
   不可在收紧中取消。

## 验证范围

测试入口为标准 Go 内置 testing，所有断言均为真实断言（比较启用/阻止集合、
原因码、边界标签与实际值），无占位或空断言。运行方式：

```sh
go test ./...        # 验收
go test ./... -v     # 查看逐条用例
```

`forwardguard_test.go` 覆盖三类交叉约束：

- **可接受结果**：双栈均绑定具体回环地址的通道启用；静态端口与一次性动态
  端口各得其所（`TestAcceptableDualStackLoopback`、
  `TestAcceptableStaticAndDynamic`）。
- **应被阻止/拒绝的结果**：双栈 IPv6 通配扩大可达范围、IPv6 侧落到可路由
  地址、单族通配、方向被移除、目标未知/越界 CIDR/越界端口、动态端口跨申请
  复用、静态端口漂移、未返回绑定、监听地址越段（`TestBlocked*`）。
- **影响两者的边界与交错场景**：CIDR/端口范围的首尾边界值同时验证启用与
  阻止（`TestCIDRAndPortBoundaries`）；确认前收紧策略后，先前可启用的通道
  按新策略被重新分类（`TestTightenRechecksEveryPendingChannel`）；收紧
  拒绝同版本号、扩大监听面、加回方向、更换会话、取消回环限制，且拒绝后
  旧策略保持不变（`TestTightenRejectsLoosening`、
  `TestTightenCannotDropLoopbackRestriction`）；另含构造器与输入校验用例。

## 范围说明

本模块是纯内存判定逻辑，不做真实目标探测、不建立连接、不生成攻击载荷；
所有网络数据均为测试夹具中的合成地址。
