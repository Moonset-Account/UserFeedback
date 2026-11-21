# l2switch —— 带标签映射与每域端口状态的二层学习交换模型

纯 Go 1.20.2 标准库实现，无任何外部依赖。模型模拟一台汇聚交换机：
两个机柜都在线侧使用 VLAN 10 承载各自业务，进入交换机后依据**入端口 +
线侧标签**映射到**不同的桥域（Bridge Domain）**；帧离开端口时再依据
**出端口 + 桥域**映射回该端口自己的线侧标签（也可以是无标签）。

## 1. 模型语义

### 1.1 标签映射（每个端口分别配置）

- **入站映射** `(端口, 线侧标签[含“无标签”]) -> 桥域`：决定帧属于哪个桥域；
  找不到映射的入站输入直接丢帧并返回 `ErrNoIngressMapping`。
- **出站映射** `(端口, 桥域) -> 线侧标签[含“无标签”]`：决定帧从该端口发出时
  携带的标签；`HasLabel=false` 表示发出无标签帧。
- “无标签”是一等公民映射值，入站、出站均支持，不与标签 0 混淆。
- 配置入站映射时端口自动以 `StateBlocking` 加入该桥域，并安装一个对称的
  默认出站映射（可用 `SetEgressMapping` 覆盖，例如改为无标签发出）。

### 1.2 每（端口, 桥域）独立的端口状态

| 状态 | 入端行为 | 是否可作为出端 |
| --- | --- | --- |
| `StateBlocking`（默认） | 直接丢帧，不学习 | 否，泛洪与单播均排除 |
| `StateLearnOnly` | 仅更新本桥域源 MAC 学习项，不转发 | 否 |
| `StateForwarding` | 先学习源 MAC，再查目的 MAC | 是 |

同一物理端口在不同桥域可处于不同状态，互不影响。

### 1.3 转发规则（`Receive` 处理顺序）

1. 解析入站映射，未命中则丢帧报错；
2. 入端为阻断：丢帧（在任何学习之前）；
3. 入端为只学习/可转发：学习源 MAC（全零源 MAC 不学习）；
4. 只学习入端到此为止，不产生任何出帧；
5. 可转发入端查目的 MAC：
   - 广播地址或未知单播：在**本桥域内泛洪**；
   - 命中学习项：**只尝试**对应的一个出端；
6. 只有与入端**不同**且在本桥域处于 `StateForwarding` 的端口才能发出；
   出端等于入端（hairpin）时丢帧；
7. 每个出帧携带出端口自己的出站标签。

### 1.4 学习表维护规则

- 学习项按 `(桥域, MAC)` 索引，**同一 MAC 在新端口出现立即覆盖改道**；
- 端口在某桥域变为阻断时，清理该端口在**该桥域**的全部学习项；
- 其他桥域的学习项与端口状态完全不受影响——因此变更一个桥域的端口状态后，
  另一个存在相同 MAC 的桥域仍按原路径转发。

## 2. 公开 API（包 `l2switch`）

| 标识符 | 说明 |
| --- | --- |
| `type Label int` | 线侧标签；配合 `HasLabel` 区分“无标签” |
| `type PortID string` / `type BridgeDomain string` | 端口与桥域标识 |
| `type MAC [6]byte` | MAC 地址，含 `ParseMAC` / `MustParseMAC` / `String` |
| `var BroadcastMAC` | 以太网广播地址 `ff:ff:ff:ff:ff:ff` |
| `type PortState int` | `StateBlocking` / `StateLearnOnly` / `StateForwarding`，含 `String()` |
| `type Frame` | 入帧：`Ingress, InLabel, HasLabel, SrcMAC, DstMAC, Payload` |
| `type EgressFrame` | 出帧：`Egress, OutLabel, HasLabel, SrcMAC, DstMAC, Payload` |
| `func NewSwitch(name string) *Switch` | 构造交换机 |
| `func (*Switch) AddPort(p PortID)` | 注册物理端口 |
| `func (*Switch) SetIngressMapping(p, label, hasLabel, bd) error` | 入站标签 -> 桥域，并加入桥域（默认阻断） |
| `func (*Switch) SetEgressMapping(p, bd, label, hasLabel) error` | 桥域 -> 出站标签（可设为无标签） |
| `func (*Switch) SetState(p, bd, state) error` | 修改每（端口, 桥域）状态；转阻断时清理该域学习项 |
| `func (*Switch) State(p, bd) (PortState, error)` | 查询状态 |
| `func (*Switch) Receive(f Frame) ([]EgressFrame, error)` | 处理一帧并返回出帧；空切片表示丢弃/不转发 |
| `func (*Switch) LookupLearned(bd, mac) (PortID, bool)` | 诊断：查询学习表 |
| `func (*Switch) LearnedCount(bd) int` | 诊断：桥域学习项数量 |

错误哨兵（配合 `errors.Is`）：`ErrUnknownPort`、`ErrNoIngressMapping`、
`ErrNoEgressMapping`、`ErrNoPortState`、`ErrInvalidPortState`。
所有方法带 `sync.RWMutex` 保护，可并发使用。

## 3. 题设场景的拓扑建模

```
机柜1  cab1 ──VLAN10──┐
                      ├─ 交换机 aggregation
机柜2  cab2 ──VLAN10──┘        cab1.VLAN10  <-> BD-A <-> agg1.VLAN100
                               cab2.VLAN10  <-> BD-B <-> agg2.VLAN200
```

- 两个机柜线侧同为 VLAN 10，但映射到 `BD-A`、`BD-B` 两个不同桥域，互不漏帧；
- 上行口分别以 VLAN 100 / VLAN 200 发出；回程在机柜口重新压回 VLAN 10；
- 同一 MAC 可同时存在于两个桥域的学习表中，指向不同端口。

## 4. 复现步骤

要求本地安装 Go 1.20.2（本工作区实测 `go version go1.20.2 darwin/arm64`），
无需联网、无需安装依赖：

```bash
go test ./... -v          # 运行全部功能测试
go test -race -cover ./... # 竞态检测 + 语句覆盖率（实测 94.6%）
gofmt -l .                # 格式检查，无输出即合格
```

## 5. 验证范围（均为真实断言测试，无预置演示输出）

共 15 个测试函数，全部通过；每个测试对应题面的一条或多条要求：

| 测试 | 场景类别 | 验证点 |
| --- | --- | --- |
| `TestUnknownUnicastFloodsWithMappedEgressLabel` | 正常 | 未知单播泛洪、出站标签重写（VLAN10→VLAN100）、源 MAC 学习、跨域不产生学习项 |
| `TestKnownUnicastTriesOnlyLearnedEgress` | 正常 | 学习后命中只走单一出端，不再泛洪 |
| `TestSameVlanTenDifferentBridgeDomainsDoNotLeak` | 正常/隔离 | 两个机柜同用 VLAN 10 但分属不同桥域，帧不互串，各自出站标签正确 |
| `TestBlockingDropsIngressClearsLearningAndExcludesEgress` | 边界 | 阻断入端丢帧且不学习；阻断口排除在泛洪外；转阻断清理本域学习项 |
| `TestLearnOnlyUpdatesSourceMacButNeverForwards` | 边界 | 只学习入端仅更新源 MAC、零出帧；非转发态出端抑制单播；转可转发后恢复 |
| `TestMacMoveReroutesImmediately` | 正常 | 同一 MAC 在新端口出现立即覆盖，后续流量即刻改道 |
| `TestStateChangeInOneBridgeDomainKeepsSameMacPathInOther` | 交错 | 一个桥域阻断端口并清学习项后，另一桥域同 MAC 的学习项与原转发路径不变，回程标签仍为 VLAN 10 |
| `TestPortStateIsIndependentPerBridgeDomain` | 交错 | 同一物理端口在两个桥域分别阻断/只学习/可转发，行为独立 |
| `TestUntaggedFramesAndUntaggedEgressAreValidMappings` | 边界 | 无标签入站映射、无标签出站映射均有效，与带标签端口混存 |
| `TestBroadcastFloodAndHairpinDrop` | 边界 | 广播不回入端；命中出端即入端时丢帧；从第三端口访问同一 MAC 正常 |
| `TestMACParsingAndString` | 工具/边界 | MAC 解析、规范化输出、非法输入报错与 panic 契约 |
| `TestUnknownPortConfigurationErrors` | 错误路径 | 未知端口的映射/状态查询返回对应哨兵错误 |
| `TestLearnedCountIsScopedPerBridgeDomain` | 诊断 | 学习计数按桥域隔离，MAC 移动覆盖而非新增 |
| `TestPortStateStringAndZeroSourceNotLearned` | 边界 | 状态字符串渲染；全零源 MAC 不学习 |
| `TestDefaultStateAndConfigurationErrors` | 错误路径 | 新成员默认阻断；无入站映射/未知端口/非成员/非法状态的错误哨兵 |

### 未覆盖与局限（如实说明）

- `go vet ./...` 在本环境因执行权限审批未运行；代码已通过 `gofmt`、
  `go build`（含在 `go test` 内）与 `-race` 检测，可在允许的环境补跑。
- `ErrNoEgressMapping` 的防御分支在正常配置流程下不可达（加入桥域即安装
  默认出站映射），因此不计入已执行覆盖；`floodLocked` 中对该错误采用跳过策略。
- 模型为内存态单交换机抽象，不模拟物理链路、队列、STP 协议报文、MAC 老化
  定时器与 VLAN 0/优先级标签等以太网细节；泛洪顺序按端口 ID 排序以保证
  测试确定性。
