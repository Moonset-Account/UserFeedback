# delegationguard — 服务身份委托请求判定模块

Go 1.20.2，仅依赖标准库（`testing`、`time`、`sort`、`errors`、`fmt`），无外部依赖。

## 解决的问题

服务身份把作业逐级委托给子作业时，本模块对一个待执行动作做授权判定，输入为：

- 已经验真的委托记录（`Delegation`，含父委托指针 `ParentID`、范围 `Scopes`、有效期 `NotBefore/ExpiresAt`）；
- 静态范围规则（`Rules`：最大链深 `MaxChainDepth`、资源白名单 `AllowedScopes`）；
- 撤销事件（`Revocation`，按生效时间判定）；
- 要执行的动作（`Action`：目标身份、请求范围、判定时刻）。

### 强制执行的约束

1. **逐级收窄**：从发起端（根委托）沿父链到当前作业逐级计算实际权限，子级只能拿到父级有效权限的子集——不得新增资源（`scope_narrowing_violation`），动词不得升级（`verb_escalation_violation`）。动词强弱为 `admin > write > read`，强动词隐含弱动词。
2. **链内取交集**：一条链的实际权限是链上所有节点范围的交集（动词取较弱者）。
3. **跨链不拼接**：同一身份可持有多条互不相干的委托链；一次动作必须由**同一条完整链独立覆盖全部请求范围**，不同链上的局部权限永远不能合并授权。覆盖不足的链标记为 `insufficient`（对更窄动作仍合法），而非 `blocked`。
4. **祖先撤销即时生效**：撤销事件按判定时刻生效；祖先被撤销后，经过该祖先的每条链在新一次 `Decide` 中立即阻断，并返回实际阻断位置（`BlockedChain.BlockedAt` 为被撤销节点 ID，而不是叶节点）。
5. **有效期边界**：`At == ExpiresAt` 仍允许，`At == NotBefore` 即生效；父级过期会阻断自身仍在有效期内的子级，阻断位置报在父级。
6. **阻断位置可定位**：每条链返回根→叶的完整 ID 序列、状态、阻断节点 ID 与原因，作业方可据此选择仍合法的独立链。

## 实际入口

包 `delegationguard`（见 `go.mod`），核心 API 位于 `delegation.go`：

| 入口 | 作用 |
| --- | --- |
| `NewEngine(delegations []Delegation, revocations []Revocation, rules Rules) (*Engine, error)` | 校验并索引输入（重复 ID、悬空父指针、未知撤销目标、倒置有效期直接报错），构造判定引擎 |
| `(*Engine).Decide(act Action) Decision` | 对目标身份持有的**每条**委托链独立求值，返回授权结论与逐链诊断 |
| `Decision.Allowed` / `Decision.ValidPath` | 是否放行及放行所用的完整链与其实际权限 |
| `Decision.BlockedChains` | 每条被阻断链的完整路径、阻断节点 ID、原因与细节 |
| `PathEvaluation.Status` | `valid`（链完整且覆盖请求）/ `blocked`（链本身失效）/ `insufficient`（链合法但范围不够） |

判定原因枚举：`revoked`、`expired`、`not_yet_valid`、`depth_exceeded`、`scope_narrowing_violation`、`verb_escalation_violation`、`unknown_scope`、`chain_cycle`、`scope_not_covered`、`no_delegation_for_delegatee`。

无命令行入口；本模块以库形式交付，验收通过 Go 内置测试框架执行。

## 验证范围

全部用例在 `delegation_test.go`，均为真实断言（无占位测试），执行：

```bash
go test ./...        # 14 个用例，全部 PASS
gofmt -l .           # 无输出（格式合规）
```

题面交叉约束的三类覆盖：

### 一、可接受结果（应放行）

- `TestDecide_AllowedBySingleChain`：单条完整链独立覆盖动作，校验根→叶交集后的实际权限（`billing:read` 保留、`storage` 被收窄掉）。
- `TestDecide_VerbImplication`：`write` 授权隐含 `read`；反向请求 `admin` 被拒。
- `TestDecide_AncestorRevocationAndIndependentPath`（后半段）：一条链被撤销后，独立链 D 仍可对更窄动作合法放行。
- `TestDecide_MixedPathOutcomesInOneDecision`：同一次判定中 valid / insufficient / blocked 三种链并存，合法链照常授权。

### 二、应被阻止或拒绝的结果

- `TestDecide_NoCombiningAcrossChains`：**核心反拼接约束**——`billing` 与 `storage` 分处两条独立链时，同时请求两者被拒，两链均为 `insufficient`，且分别报出范围丢失位置（C 与根 D）。
- `TestDecide_NarrowingViolationsBlockedAtChild`：动词升级与新增资源分别在违规子节点阻断。
- `TestDecide_AllowedScopesRule`：白名单外资源在引入节点阻断。
- `TestDecide_MaxChainDepth`：超出链深在超限节点阻断。
- `TestDecide_NoDelegation` / `TestDecide_ChainCycle`：无委托身份显式拒绝；环状父链被检出而非死循环。
- `TestNewEngine_ValidationErrors`：重复 ID、悬空父链、未知撤销目标、倒置有效期在构造期拒绝。

### 三、影响两者的边界与交错场景

- `TestDecide_ExpiryBoundary`：恰好 `ExpiresAt` 时刻放行、晚一秒阻断；补充叶节点单独过期时阻断位置精确报叶节点。
- `TestDecide_NotBeforeBoundary`：生效前一纳秒阻断、恰好生效时刻放行。
- `TestDecide_AncestorRevocationAndIndependentPath`：祖先 A 撤销后新判定立即受影响，阻断位置为 A 而非叶 C；独立链 D 仍可用。
- `TestDecide_FutureRevocation`：未来时刻才生效的撤销不影响当前判定。
- `TestDecide_ParentExpiryBlocksLiveChild`：父级到期、子级自身未到期的交错有效期，阻断位置报父级 P。

## 已知限制（如实记录）

- 本次会话中 `go vet ./...` 的执行需要额外审批且未获授权，故静态检查未实际运行；已通过 `gofmt -l`（无输出）与全部 `go test` 用例验证。
- 撤销事件随 `NewEngine` 输入；决策批次刷新撤销集合后重新构造引擎即可保证新判定即时反映撤销，引擎内部不做后台监听。
