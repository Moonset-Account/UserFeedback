# 厂区服务通告投影模块（Campus Discovery Projection）

设备发现网关从通告中读取服务实例、用途标签、主机别名、地址引用与有效期，
为每个目的区（zone）投影出一组**可独立解析**的 DNS 风格记录。本模块是纯函数式
快照投影：给定一份通告和一个时刻 `asOf`，返回每个目的区的记录与服务状态，不改
变输入、不联网、不读取任何凭据。

## 快速开始

```sh
go test ./...          # 运行全部测试
go test -race ./...    # 竞态检测
go test -run TestInterleavedAnnouncements -v ./...
```

要求 Go 1.20.2。无第三方依赖，无构建脚本，`go.mod` 仅声明模块与语言版本。

## 领域模型

| 概念 | 类型 | 说明 |
| --- | --- | --- |
| 通告快照 | `Announcement` | 服务、别名、地址、目的区策略与撤回列表 |
| 服务实例 | `Service` | `ID`、`Purpose`（用途标签）、`Private`、`Validity`、`EntryNames` |
| 主机别名 | `Alias` | `Name`、`Owner`（所属服务）、`Target`、`Validity` |
| 地址记录 | `Address` | `ID`、`Name`、`IP`、`Validity`；可被多个服务共享 |
| 目的区 | `Zone` | `ApprovedPurposes`、`ApprovedServices`、`DeniedServices` |
| 有效期 | `Window` | 半开区间 `[NotBefore, Until)`，零值表示该侧无界 |

服务到记录的解析链路是：

```
EntryName --CNAME--> 别名 --CNAME--> ... --> 地址名 --A--> IP
```

链路中每一跳的别名都必须属于同一服务；跨服务**只允许**共享 `Address` 记录。
因此别名不会在服务之间泄漏解析关系。

## 公开 API

```go
// 投影一次通告快照。asOf 为判定有效期的时刻。
func Project(a Announcement, asOf time.Time) (Result, error)

// 仅做结构校验；Project 内部先调用它。
func Validate(a Announcement) error

type Window struct{ NotBefore, Until time.Time }
func (Window) ActiveAt(t time.Time) bool   // t ∈ [NotBefore, Until)
func (Window) PendingAt(t time.Time) bool  // t < NotBefore

type Result struct{ AsOf time.Time; Zones []ZoneResult }
func (Result) Zone(name string) (ZoneResult, bool)

type ZoneResult struct {
    Zone     string
    Records  []Record       // 已排序、区内去重
    Services []ServiceResult
}

type ServiceResult struct {
    ID, Purpose string
    Private     bool
    Status      ServiceStatus  // published / incomplete / withdrawn / pending / expired
    RecordCount int            // 仅 published 时非零
    Issues      []Issue        // 未发布原因
}

type Record struct{ Name string; Type RecordType; Value string } // "name TYPE value"
```

### 语义约定

1. **按区过滤**：目的区只看到自己获准的服务。公开服务按 `Purpose` 或显式 `ID`
   获准；私密服务（`Private`）只接受显式 `ID`。`DeniedServices` 永远优先。
2. **按需依赖**：只为获准服务解析其入口别名链；别的服务的别名即使存在也不投影。
3. **共享地址去重**：多个已发布服务引用同一 `Address` 时，该 `A` 记录在每个区内
   只出现一次。
4. **撤回**：`Withdrawn` 中的服务不产出任何别名或记录；它独占的地址随之消失，
   但**其他已发布服务仍引用的共享地址继续保留**。
5. **原子发布**：服务的所有入口名都解析成功才发布；任一入口失败则整体
   `incomplete`，不泄漏任何部分记录。补齐依赖的后续通告会使其自动转为
   `published`（见 `TestMissingAddressThenComplete` 与
   `TestInterleavedAnnouncements`）。
6. **有效期边界**：区间左闭右开。`asOf == NotBefore` 生效，`asOf == Until` 失效。
   服务、别名、地址各自独立判定：服务自身 `pending`/`expired` 时不解析依赖；
   依赖过期或未生效则服务 `incomplete`。
7. **名称规范化**：去首尾空白、去单个尾点、ASCII 小写化；别名引用与地址名同样
   规范化后再匹配，因此 `WWW.Factory.Example.COM.` 与 `www.factory.example.com`
   等价。
8. **可重复**：相同输入产生相同输出，记录按 `(Name, Type, Value)` 排序。

### 结构校验（`Validate` / `Project` 返回的错误）

以下属于**通告本身有误**，返回 `ValidationError`：

- 服务、地址、目的区 ID/名称重复；别名重名；地址名与别名重名或地址间重名
- 别名 `Owner` 指向不存在的服务；目标类型未知；引用为空或非法
- 有效期起点不早于终点（空窗口除外）
- 地址 IP 非法；服务缺少入口名；名称为空或含非法字符

以下**不是**校验错误，而是运行期的临时依赖缺口，用 `Issue` 表达：

- 别名链引用不存在的地址（`missing_address`）或地址尚未生效/已过期
  （`address_not_yet_valid` / `address_expired`）
- 入口或中间别名缺失（`missing_alias` / `missing_alias_target`）
- 别名属于其他服务（`alias_foreign_owner`）、别名过期/未生效、别名环
  （`alias_cycle`）、链过深（`alias_chain_too_deep`）

## 测试与验证范围

`go test ./...` 共 31 个顶层测试、47 个含子测试的用例，全部通过
（另以 `-race` 复核通过）。覆盖范围：

**正常场景**
- 两区不同授权，记录按区筛选与排序（`TestProjectBasicZones`）
- 多跳别名链（`TestAliasChain`）、别名目标/地址名大小写与尾点规范化
  （`TestAliasTargetNormalized`、`TestNameNormalization`）
- 空通告、无目的区、空目的区（`TestProjectEmptyAnnouncement`、
  `TestNoZonesShowsOnlyPolicy`）

**题面要求的边界**
- 私密服务不被用途标签暴露，仅显式授权可见
  （`TestPrivateServiceNeedsExplicitApproval`）
- 拒绝优先于用途/显式授权（`TestDeniedServiceWinsOverApprovals`、
  `TestDeniedBeatsExplicitApproval`）
- 同一地址被多服务共享时区内只发一次（`TestSharedAddressEmittedOncePerZone`）
- 有效期左闭右开的四个临界点（`TestWindowBoundaries`）
- 服务、别名、地址三层有效期独立判定（`TestAliasExpired`、
  `TestAliasValidityIndependentOfService`、`TestAddressNotYetValid`、
  `TestAddressExpiredWithNoRenewal`）

**交错与状态迁移**
- 私密服务撤回后别名消失、共享地址仍供其他服务使用
  （`TestWithdrawPrivateServiceKeepsSharedAddress`）
- 缺地址 → 补齐 → 私有服务撤回 → 地址续期的四阶段连续快照
  （`TestInterleavedAnnouncements`）
- 缺依赖时标为未完成、补齐后自动发布（`TestMissingAddressThenComplete`）
- 一个入口成功、一个缺失时整体不发布（`TestChainFailuresAreAtomic`）
- 跨服务别名不可借用、别名环、链过深防护
  （`TestForeignAliasIsolation`、`TestAliasCycle`、`TestAliasChainTooDeep`）
- 两区各自只保留本方所需记录（`TestSharedAddressAcrossZonesKeptPerZone`）

**不变量**
- 不修改输入（`TestProjectDoesNotMutateInput`）
- 结果确定且有序（`TestProjectDeterministic`）
- 非法通告全部拒绝（`TestValidateRejectsBadAnnouncements`）

### 未覆盖 / 已知限制

- 仅支持 `A` 与 `CNAME` 记录，未实现 `AAAA`、`TXT`、`SRV` 等类型。
- 不做 DNSSEC 签名、传输层编码或网络发布；输出为内存结构，序列化方式由调用方
  决定。
- 未做规模/性能基准；通告按小规模厂区快照假定。
- 共享地址的“仍被引用”判定严格基于**同一快照内已发布的服务**，不做跨快照的
  历史引用统计。

## 文件

- `model.go` — 数据模型与状态/原因常量
- `projection.go` — 校验、按区投影、别名链解析
- `projection_test.go` — 合成数据测试（全部使用本地构造数据，无外部依赖）
