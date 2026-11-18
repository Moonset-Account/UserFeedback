# 离线路由授权裁决模块（routeauth）

一个完全离线、内存内运行的路由起源授权裁决引擎，语义对齐 RPKI ROA 路由起源
校验流程，但只依赖调用方提供的**已验证授权记录**和显式**撤销事件**，不发起
任何网络请求。

## 裁决规则

对一条路由宣告（前缀 + 起源 AS），在指定时刻 `t` 按下述顺序裁决：

1. 授权记录在 `t` 时刻有效：`NotBefore <= t < NotAfter`（下界包含、上界排除），
   且未在 `t` 之前或当时被撤销（撤销时刻 `At <= t` 即视为已撤）。
2. 授权前缀**覆盖**宣告前缀：同地址族，授权前缀不比宣告前缀更具体，且宣告
   前缀整体落在授权前缀内。
3. 宣告前缀长度不超过该记录的 `MaxPrefixLength`。
4. 宣告起源 AS 与授权 `OriginAS` 相同。

三种结果严格区分：

| 状态 | 含义 |
| --- | --- |
| `valid` | 至少一条有效覆盖记录完全匹配（条件 1–4 全满足），放行 |
| `invalid` | 存在有效覆盖记录但无一匹配（起源错误和/或前缀过长），拒绝 |
| `not_found` | 没有任何有效授权覆盖该宣告，单独处理 |

- 新旧授权重叠时，**任一**有效匹配记录即可判 `valid`；其余记录的不匹配不会
  污染结果。
- 撤销只撤掉事件指定的记录 ID，重复撤销和撤销未知记录均报错且不改变引擎状态。
- 裁决结果的 `Associated` 字段返回与决定关联的记录 ID（valid 为全部匹配记录，
  invalid 为全部覆盖记录，not_found 为空），ID 排序保证确定性。
- `EvaluateAt` 支持在任意指定时刻复核同一宣告；撤销时刻与有效窗口均按时刻
  比较，因此历史时刻的裁决可被稳定重放，不受后续状态变化影响。

## 实际入口

代码位于包 `routeauth`（无独立可执行程序，作为库使用）：

- `NewEngine() *Engine`：创建引擎。
- `(*Engine).Add(Authorization) error`：录入一条已验证授权；重复 ID 返回
  `ErrDuplicateRecord`，畸形记录返回包裹 `ErrInvalidInput` 的错误。
- `(*Engine).Revoke(Revocation) error`：按记录 ID 精确撤销；未知 ID 返回
  `ErrUnknownRecord`，重复撤销返回 `ErrAlreadyRevoked`。
- `(*Engine).Evaluate(Announcement) (Decision, error)`：以当前时刻裁决
  （内部等价于 `EvaluateAt(a, time.Now())`）。
- `(*Engine).EvaluateAt(Announcement, time.Time) (Decision, error)`：**主要
  复核入口**，以指定时刻裁决，保证可重放。
- `(*Engine).Get / Revoked / RevokedAt`：记录与撤销状态查询。

关键类型：`Authorization`（ID、CIDR 前缀、最大前缀长度、起源 AS、有效窗口）、
`Revocation`（记录 ID、撤销时刻）、`Announcement`（CIDR 前缀、起源 AS）、
`Decision`（状态、原因、关联记录 ID）。同时支持 IPv4 与 IPv6（基于
`net/netip`），跨地址族永不构成覆盖。

## 验证范围

运行：

```sh
go test ./...
```

验收用例（Go 内置 `testing`，全部为真实断言，无 mock 网络）：

- **可接受结果**：`TestValidAccepted` —— 前缀在内、长度合规、起源匹配 →
  `valid`，并校验关联记录。
- **应被阻止/拒绝的结果**：
  - `TestInvalidWrongOrigin` —— 有覆盖但起源不匹配 → `invalid`（而非
    `not_found`）；
  - `TestInvalidPrefixTooSpecific` —— 长度恰为上限通过、超出 1 位被拒；
  - `TestNotFoundNoCoverage` —— 完全无覆盖 → `not_found`，关联记录为空。
- **边界与交错场景**：
  - `TestValidityWindowBoundaries` —— `NotBefore` 包含、`NotAfter` 排除的
    时刻边界；
  - `TestOverlappingAuthorizationsEitherMatch` —— 新旧授权重叠，任一匹配即
    通过；两记录覆盖同一宣告、只有一条匹配时只关联匹配记录；均不匹配时
    `invalid` 且返回全部覆盖记录；
  - `TestRevocationOnlyWithdrawsTarget` —— 撤销只影响指定记录；撤销时刻
    边界（前一秒仍有效、当时即失效），并演示结果由 `valid` 退化为
    `invalid` 再到 `not_found`；
  - `TestPointInTimeReplay` —— 后续撤销不影响历史时刻复核结果；
  - `TestIPv6AndFamilyMismatch` —— IPv6 长度/起源裁决及地址族隔离；
  - `TestInputValidation` / `TestRevocationValidation` —— 畸形前缀、越界
    最大长度、ASN 0、空窗口、重复 ID、未知/重复撤销等拒绝路径；
  - `TestGetRecord` —— 查询辅助方法。

## 工作区内容

- `go.mod` —— 模块定义（`go 1.20`，零第三方依赖）。
- `engine.go` —— 引擎实现。
- `engine_test.go` —— 验收测试。
- `README.md` —— 本说明。
