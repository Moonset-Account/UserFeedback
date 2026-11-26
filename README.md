# downloadkey — 签发前校验状态机

下载密钥服务在员工**传输中途换绑认证设备**时使用的签发前校验状态机。状态机只做题面
规定的防御性判断：校验已验真注册回执、传输段号与绑定版本，维护设备换绑的切点
（cutover），并向登录模块返回当前可批准的段范围、实际匹配的绑定和清晰的失败原因。

本模块不包含任何网络访问、凭据存储或密钥材料；不生成、不执行任何利用/攻击/规避载荷。
所有数据均为进程内合成数据。

## 领域规则与状态机语义

1. 一次传输按 1 起始的连续段号分段，批准状态始终保持**前缀连续**：已批准段恰好是
   `1..HighWater`，不存在空洞。
2. 设备换绑只从**提交换绑时尚未批准的第一个段**（`HighWater+1`）开始生效，该段称为
   切点（cutover）。
3. 切点一旦确认即**不可变**：重复提交换绑、旧设备上传重试、新设备续领、被拒绝的请求
   都不能移动它。
4. 切点之前（`< cutover`）只能由旧绑定回执批准；切点及之后（`>= cutover`）只能由当前
   （最高版本）绑定回执批准。续领必须由新设备完成。
5. 正在重送的旧段：若落在切点之前，仍可凭原回执请求**相同范围**（幂等，重复段不会再次
   计入新增）；若已落在切点之后，则被拒绝并给出明确原因。
6. 当前生效绑定续领时不得跳过未批准段（缺口请求被拒绝）；重试已批准段是幂等成功。
7. 跨切点的请求只批准本回执治理区间内的子范围（`PARTIAL_APPROVAL`），其余段保持不动，
   调用方须换另一台设备的回执重试。
8. 换绑提交前，已注册但尚未生效的更高版本绑定治理不了任何段。

## 公开 API

包名 `machine`（module `downloadkey`），全部类型与方法均为并发安全（内部 `sync.RWMutex`，
每次调用原子完成）。

### 构造

| 签名 | 说明 |
| --- | --- |
| `New(employeeID string, bindings []Binding) (*Machine, error)` | 为一次传输建状态机；绑定版本必须唯一且至少一个，最高版本为当前绑定。空列表返回 `ErrNoBindings`，重复版本返回错误。 |

### 输入/输出类型

- `Binding{Version int, DeviceID string}`：一条已注册设备绑定。
- `Receipt{EmployeeID string, BindingVersion int, DeviceID string}`：已验真的注册回执；
  状态机仍会复核员工归属、版本是否存在、设备与版本是否一致。
- `SegmentRange{First, Last int}`：闭区间段范围，方法 `Valid()` / `Overlaps()` /
  `Intersect()`。
- `Result`：`Decision`、`Reason`、`Message`（人类可读，不含敏感信息）、`Requested`、
  `Approvable`（该回执下可批准的完整子范围）、`NewlyApproved`（本次实际新增批准的段，
  幂等重试为空）、`MatchedVersion`、`MatchedDeviceID`、`Cutover`。
- `RebindOutcome`：`AlreadyLocked`、`Cutover`、`Reason`、`Message`。

### 方法

| 方法 | 说明 |
| --- | --- |
| `Validate(receipt Receipt, r SegmentRange) Result` | 签发前校验并原子记录批准结果。 |
| `SubmitRebind(receipt Receipt) RebindOutcome` | 提交换绑；首次成功锁定切点，之后的提交返回 `CUTOVER_CONFIRMED_IMMUTABLE` 且切点不变。 |
| `Cutover() (int, error)` | 已确认切点；未换绑时返回 `ErrCutoverNotEstablished`。 |
| `CutoverLocked() bool` | 切点是否已锁定。 |
| `HighWater() int` / `FirstUnapproved() int` / `IsApproved(n int) bool` | 批准前缀观测。 |
| `CurrentVersion() int` | 当前（最高）绑定版本。 |

### 决策与原因码（稳定的机器可读词汇）

决策：`APPROVED`、`PARTIAL_APPROVAL`、`REJECTED`。

原因码：

| 原因码 | 触发场景 |
| --- | --- |
| `OK` | 完整批准（含幂等重试）。 |
| `INVALID_SEGMENT_RANGE` | 段号非 1 起始或区间倒置。 |
| `EMPLOYEE_MISMATCH` | 回执员工不拥有该传输。 |
| `UNKNOWN_BINDING_VERSION` | 回执绑定版本未注册。 |
| `DEVICE_MISMATCH` | 回执设备与注册版本的设备不一致。 |
| `BINDING_HAS_NO_GOVERNED_RANGE` | 该绑定在此传输上治理不到任何段（如换绑前的新版、切点为 1 时的旧版）。 |
| `OLD_BINDING_AFTER_CUTOVER` | 旧绑定请求切点之后的段。 |
| `STALE_BINDING_BEFORE_CUTOVER` | 新绑定请求切点之前的段。 |
| `NEW_BINDING_GAP` | 当前绑定跳过未批准段形成空洞。 |
| `CUTOVER_CONFIRMED_IMMUTABLE` | 重复换绑提交，切点已锁定。 |
| `REBIND_EMPLOYEE_MISMATCH` / `REBIND_UNKNOWN_BINDING_VERSION` / `REBIND_VERSION_NOT_CURRENT` / `REBIND_DEVICE_MISMATCH` | 换绑提交的四类回执校验失败，均不会锁定或移动切点。 |

被拒绝的请求不会改变任何状态（测试中对 `HighWater` 与切点做了前后断言）。

## 运行方法

要求 Go 1.20.2（仅使用标准库，无外部依赖、无需联网）：

```bash
go test ./...            # 常规运行
go test ./... -race      # 竞态检测
go test ./... -v         # 查看全部用例与可执行 Example
```

## 验证范围（本地合成数据）

`machine_test.go` 使用进程内合成的员工、两个/三个绑定版本与段范围，覆盖：

- **正常流程**：未换绑时的单设备批准、幂等重试、空洞拒绝、新版未生效前零治理区间。
- **切点建立**：在已批准 `1..3` 后换绑，切点固定为 4；以及零批准时换绑（切点为 1，旧版
  立即失去治理区间）的边界。
- **乱序交错（题面重点）**：旧版迟到重试切点段被拒、新版从切点续领成功、旧版前缀段乱序
  重送幂等成功且切点不动、跨切点宽范围请求被裁剪为 `PARTIAL_APPROVAL`。
- **切点免疫**：重复/伪造换绑提交、新旧两侧各类重试与拒绝请求后，切点保持不变。
- **校验失败**：非法区间、员工/版本/设备不匹配、旧版越界、新版缺口，且断言拒绝不产生
  状态变更。
- **多版本绑定**：三个绑定版本下仅最高版本为当前版本，退役版本共享切点前缀区间。
- **并发交错**：16 个 goroutine × 200 轮混合换绑重试、旧版重送、新版续领、缺口请求，
  `-race` 下验证三大不变量（切点恒定、批准前缀连续、两侧绑定互不可越界）。
- **可执行文档**：`ExampleValidate_outOfOrderCutover` 以固定输出演示头条交错场景。

实测证据：`go test ./... -count=1 -race` 通过；并重复执行 `-count=50 -race` 与
`-count=100`（无竞态检测）各一批，全部通过。开发期间发现的一次偶发失败经定位是**测试
夹具缺陷**（并发续领把“远段缺口”推进成了合法的下一段），已改用任何调用量都无法到达的
段号修复；状态机实现本身的前缀连续性判断未发现问题。`go vet` 在当前环境需交互式授权，
未纳入自动执行，已如实记录。

## 设计边界（假设）

- 状态机面向“一次传输、一次生效换绑”的题面场景；支持注册任意数量的绑定版本，但切点
  只由第一次有效换绑锁定，不实现切点之后的再次迁移。
- 回执的密码学验真由注册子系统在进入本状态机前完成；本模块负责题面要求的归属、版本与
  设备一致性复核，不接触凭据或密钥。
