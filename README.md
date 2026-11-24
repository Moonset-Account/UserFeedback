# 策略协商模块

两家分支机构分别提交自己的**允许发送**和**允许接收**范围后，本模块按以下四元组求双方共识：

```text
协议 + 源地址段 + 目的地址段 + 目的端口
```

`A -> B` 方向使用 `A.Send ∩ B.Receive`；`B -> A` 方向独立使用 `B.Send ∩ A.Receive`。反方向不会复用正方向的裁决结果。

模块只做本地策略集合运算，不发起网络连接、不依赖第三方库，也不实现隧道数据面。

## 运行环境

- Go 1.20.2（或兼容的 Go 1.20.x）
- 无外部依赖

## 快速验证

```bash
go test ./...
```

带竞态检测和重复执行：

```bash
go test -race ./...
go test -count=20 ./...
```

查看覆盖率：

```bash
go test -cover ./...
```

## 最小示例

```go
package main

import (
    "fmt"

    negotiation "policynegotiation"
)

func main() {
    a := negotiation.Party{
        ID: "beijing",
        Send: negotiation.Policy{Grants: []negotiation.Grant{{
            Protocol:    "tcp",
            Source:      negotiation.MustParseAddressRange("10.10.0.0/24"),
            Destination: negotiation.MustParseAddressRange("10.20.0.0/24"),
            Ports:       negotiation.Ports(80, 443),
        }}},
    }

    b := negotiation.Party{
        ID: "shanghai",
        Receive: negotiation.Policy{Grants: []negotiation.Grant{{
            Protocol:    "tcp",
            Source:      negotiation.MustParseAddressRange("10.10.0.0/16"),
            Destination: negotiation.MustParseAddressRange("10.20.0.0/16"),
            Ports:       negotiation.Ports(80, 443),
        }}},
    }

    result, err := negotiation.Negotiate(a, b)
    if err != nil {
        panic(err)
    }

    for _, rule := range result.Forward.Agreed {
        fmt.Printf("%s %s %s -> %s ports %v\n",
            rule.Direction, rule.Protocol, rule.Source, rule.Destination,
            rule.DestinationPorts)
    }
}
```

## 协商与排除语义

### 两个方向分别裁决

| 方向 | 发送方申请 | 接收方申请 | 共识 |
|---|---|---|---|
| `A -> B` | `A.Send` | `B.Receive` | 两者交集 |
| `B -> A` | `B.Send` | `A.Receive` | 两者交集 |

每个方向同时返回：

- `Agreed`：双方都允许、可下发的互不重叠规则。
- `SenderUnmatched`：发送方有效申请中未被接收方接受的精确剩余范围。
- `ReceiverUnmatched`：接收方有效申请中未被发送方覆盖的精确剩余范围。

顶层还提供：

- `Result.Rules`：两个方向的全部已协商规则，每条规则带 `Direction`。
- `Result.Unmatched`：带 `PartyID`、`Channel`（`send`/`receive`）和方向的扁平清单，便于生成变更单。

### 排除项

`Policy.Exclusions` 会先从本侧申请中扣除，再参与协商：

- `ReservedAddresses`：预留地址段；若它出现在源地址或目的地址任一维度，对应流量被排除。
- `SourceAddresses`：只从源地址段排除。
- `DestinationAddresses`：只从目的地址段排除。
- `DestinationPorts`：排除单个或连续的目的端口；单端口可用 `negotiation.SinglePort(22)`。

被本侧排除的内容不属于“有效申请”，因此不会出现在未通过清单中。

## 地址、协议和端口格式

### 地址段

`ParseAddressRange` 支持：

- 单个地址：`10.0.0.1`、`2001:db8::1`
- CIDR：`10.0.0.0/24`、`2001:db8::/64`
- 起讫区间：`10.0.0.10-10.0.0.20`

地址区间为**闭区间**，首尾都包含。IPv4 与 IPv6 分开处理，不能在同一条 Grant 中混用。

`AddressRange.Prefixes()` / `CIDRStrings()` 可把精确区间转换为最小 CIDR 集合，便于对接只接受 CIDR 的下发系统。

### 协议

`Grant.Protocol` 支持：

- 名称：`tcp`、`udp`、`icmp`、`icmpv6`（大小写不敏感）
- 数字协议号：`6`
- 逗号集合：`tcp,udp`
- 连续数字区间：`1-17`
- 空字符串、`any`、`*`：`0-255`

不连续协议会拆成独立规则，避免构造含义不清的单条下发规则。

### 端口

端口为闭区间，合法范围是 `1-65535`。`Ports(80, 443)` 生成两个单端口；也可以直接提供：

```go
[]negotiation.PortRange{{Start: 8000, End: 9000}}
```

`Grant.Ports` 为空时表示全部有效目的端口。规范化会排序并合并重叠或相邻端口区间。

## 合并与对应关系

规则在以下字段完全相同时，才允许合并相邻地址段：

- 协议区间相同；
- 另一个地址维度相同；
- 目的端口集合完全相同。

因此，若 `10.10.0.0/24` 开放 `{80,443}`，而相邻的 `10.10.1.0/24` 只开放 `{8080}`，输出仍会保留两条规则及其端口对应关系，不会错误合并成 `10.10.0.0/23` 开放 `{80,443,8080}`。

同一地址范围上的多个端口会规范化到同一条 `Scope.DestinationPorts` 中。所有输出规则都经过四维集合差运算和非重叠校验。

可使用以下公开辅助函数复核或重新规范化外部规则：

- `MergeScopes([]Scope) ([]Scope, error)`
- `MergeRules([]Rule) ([]Rule, error)`（按 `Direction` 分组）
- `ScopesOverlap(a, b Scope) bool`
- `RulesOverlap(a, b Rule) bool`
- `RulesPairwiseDisjoint(rules []Rule) bool`
- `CheckRulesDisjoint(rules []Rule) error`

不同 `Direction` 的规则被视为不同下发方向，不判定为重叠。

## 主要公开 API

```go
func Negotiate(a, b Party) (*Result, error)
func NegotiatePolicies(a, b Party) (*Result, error) // Negotiate 的描述性别名

type Party struct { // Branch 是 Party 的别名
    ID      string
    Send    Policy
    Receive Policy
}

type AddressRange struct{ Start, End netip.Addr } // IPRange 是其别名

type Policy struct {
    Grants     []Grant
    Exclusions Exclusions
}

type Grant struct {
    Protocol    string
    Source      AddressRange
    Destination AddressRange
    Ports       []PortRange
}

type Result struct {
    Forward DirectionResult
    Reverse DirectionResult
    Rules   []Rule
    Unmatched []UnmatchedScope
}
```

结果结构和地址/协议/端口类型均带 JSON tag，`ProtocolRange` 的 JSON 形式为 `"tcp"`、`"udp"`、`"any"` 或 `"7-255"` 这样的规范字符串，并支持往返反序列化。

## 已执行的验证范围

当前工作区已实际运行：

```text
go test ./...
ok  policynegotiation
```

测试覆盖：

1. 基本双方向协商，确认正方向和反方向分别使用各自的发送/接收申请。
2. 地址和端口闭区间边界，例如 `1-3` 与 `2-4` 的交集为 `2-3`。
3. 双方各自未通过范围的精确性，并用独立的流量元组集合 oracle 比对。
4. 预留源地址、预留目的地址和单个排除端口。
5. 源/目的地址段与端口集合的对应关系：端口集合不同不得错误合并；相同且相邻时正确合并；存在地址空隙时不跨空合并。
6. 重叠 Grant、重叠地址段、交错端口和排除项的随机化组合。
7. 输出规则的几何非重叠检查，以及逐元组枚举的暴力非重叠检查。
8. IPv4/IPv6 地址解析、CIDR 边界和 CIDR 回转。
9. 非法地址、非法端口、未知协议和 IPv4/IPv6 混用时返回错误。
10. JSON 编码和往返反序列化。
11. `MergeRules` 的外部重叠范围规范化及对协商结果的幂等性。
12. 公开包级 `ExampleNegotiate`，其期望输出由 `go test` 校验。

随机 oracle 测试使用固定种子 `15877`，每次 `go test` 执行 300 组四策略随机场景；此前已用 `go test -count=20 ./...` 重复执行并通过，相当于 6000 次固定种子随机场景。`go test -race ./...` 也已通过；最近一次 `go test -cover ./...` 记录的语句覆盖率为 `81.6%`。

## 非目标与限制

- 这是策略协商库，不包含 IPsec/WireGuard 等隧道配置生成或真实报文拦截。
- 不访问网络、不读取凭据、不上传材料，也不安装依赖。
- 未实现持久化存储或变更单外部系统对接；`Result.Unmatched` 可作为对接数据源。
- 地址区间可能很大时，调用方不应自行逐地址枚举；库内部使用区间运算，测试中的逐元组枚举只用于小范围 oracle 校验。
