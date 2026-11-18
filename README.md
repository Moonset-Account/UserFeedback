# 工作负载身份申请裁决模块（Workload Identity Adjudicator）

为本地签发器提供工作负载身份申请的**裁决**能力。模块读取三类已经验证/登记的本地证据：

1. **已验证的启动测量**（boot measurement）：作业实例在节点上启动时生成并经测量流水线验证；
2. **调度租约**（scheduling lease）：调度器授予的、带版本与有效窗口的作业占位凭证；
3. **服务身份登记**（service identity registration）：镜像摘要（image digest）对应的服务身份、允许受众与发布许可。

裁决分两阶段完成，所有判定（含拒绝）都带稳定原因码、证据快照与绑定事实，写入不可变决策日志，形成可追溯依据。

## 入口与运行方式

- 公共入口：`src/index.mjs`
  - `createAdjudicationWorld()`：构造共享逻辑时钟的完整环境（六个登记处 + 裁决器）
  - `Adjudicator` / 各 Registry / `LogicalClock` / `REASONS` 均按需具名导出
- 核心实现：`src/adjudicator.mjs`（裁决）、`src/stores.mjs`（登记处）、`src/clock.mjs`（时钟）、`src/reasons.mjs`（原因码）
- 验收测试：`tests/adjudicator.test.mjs`，基于 `node:test` + `node:assert/strict`，无第三方依赖
- 运行环境：Node.js 24（ESM，`"type": "module"`）

```bash
# 推荐（显式测试文件，Node 24 已验证）
node --test tests/adjudicator.test.mjs

# 或（自动发现 tests/ 下的 *.test.mjs）
node --test

# npm 脚本等价于第一条命令（本环境 npm 执行需额外授权，未在核验中使用）
npm test
```

## 裁决模型

### 阶段一：`adjudicator.requestApproval(req)`

依次做交叉约束检查，任一失败即返回 `verdict: 'DENY'` 并附原因码：

| 约束域 | 规则 | 拒绝原因码 |
| --- | --- | --- |
| 启动测量 | 登记存在、已验证、作业/实例/节点三者一致 | `MEASUREMENT_NOT_FOUND` / `MEASUREMENT_NOT_VERIFIED` / `MEASUREMENT_JOB_MISMATCH` / `MEASUREMENT_INSTANCE_MISMATCH` / `MEASUREMENT_NODE_MISMATCH` |
| 测量复用 | **测量仅可用于原节点上的原作业实例**；调度重试的新实例必须提交自己的测量 | `MEASUREMENT_INSTANCE_MISMATCH`（节点维度为 `MEASUREMENT_NODE_MISMATCH`） |
| 调度租约 | 存在、范围匹配、状态 `ACTIVE`、处于生效窗口 | `LEASE_NOT_FOUND` / `LEASE_SCOPE_MISMATCH` / `LEASE_NOT_ACTIVE` / `LEASE_NOT_YET_VALID` / `LEASE_EXPIRED` |
| 身份登记 | 服务身份已登记且有效，登记的镜像摘要与测量摘要一致 | `IDENTITY_NOT_REGISTERED` / `IDENTITY_REVOKED` / `IMAGE_DIGEST_MISMATCH` |
| 身份受众 | 申请受众在登记允许集合内 | `AUDIENCE_NOT_PERMITTED` |
| 许可撤销 | 身份级/受众级/许可级撤销命中即阻断 | `IDENTITY_REVOKED` / `AUDIENCE_REVOKED` / `PERMISSION_REVOKED` |

通过后生成一次性审批 `apv-N`，并把**租约版本（leaseVersion）与身份受众（audience）显式绑定**到审批（`approval.bindings`）。

### 阶段二：`adjudicator.issue(consume)`

消费审批、签发身份凭证前做**活证据复验**：

- 审批存在且未被消费（一次性）→ 否则 `APPROVAL_NOT_FOUND` / `APPROVAL_ALREADY_CONSUMED`；
- 消费请求重申的租约版本、受众必须与审批绑定一致 → 否则 `APPROVAL_BINDING_MISMATCH`；
- 租约仍 `ACTIVE`、未过期且版本未变；**租约替换后旧审批不可消费**（旧租约在新租约登记时自动置 `REPLACED`）→ `LEASE_NOT_ACTIVE` / `LEASE_EXPIRED` / `APPROVAL_BINDING_MISMATCH`；
- 测量仍验证有效、身份登记仍有效且摘要仍匹配 → `APPROVAL_EVIDENCE_STALE`；
- 审批之后新发布的撤销同样阻断本次签发 → `PERMISSION_REVOKED` / `AUDIENCE_REVOKED` / `IDENTITY_REVOKED`。

通过后审批标记 `consumed`，签发内嵌主体（job/instance/node）、镜像摘要、租约版本、受众、测量 ID 与审批 ID 的凭证 `cred-N`。

### 可追溯性

每个决策对象包含：`decisionId`、`verdict`、`stage`（`APPROVAL`/`ISSUANCE`）、`requestId`、稳定原因码 `reasons[].code/message`、`evaluatedAt`、`evidence`（测量/租约/身份/撤销快照）；ALLOW 决策另带 `basis`（裁决依据）与绑定事实。可通过 `getDecisionLog()`、`getDecision(id)`、`getDecisionsForRequest(requestId)` 检索。

### 时钟与撤销语义

- `LogicalClock` 把**单调序列号**（`next()`，排序/编号）与**墙上时间**（`wallMs()`/`now()`，租约窗口）解耦；测试中的 `MutableClock` 可手动拨表构造过期/未生效边界。
- 撤销为只追加事件流，三级范围：`IDENTITY`（整身份）> `AUDIENCE`（身份+受众）> `PERMISSION`（身份+具体许可），按最新匹配命中。

## 测试验证范围（24 个用例，7 个分组）

| 分组 | 覆盖内容 |
| --- | --- |
| 可接受链路与可追溯性 | 完整审批+签发通过；凭证绑定租约版本/受众；决策日志按 ID 与 requestId 可追溯 |
| 测量绑定（拒绝 + 边界） | 新实例复用旧测量、跨节点、跨作业、未验证、缺失均被阻止；**边界：新实例提交自己的测量与新租约可通过** |
| 租约版本与替换 | 租约替换后旧审批不可消费、新版本重新审批可通过；消费绑定失配拦截；生效窗口边界（未生效/过期/审批后到期的交错场景） |
| 许可撤销 | 审批前撤销直接拒绝、**审批后撤销阻断随后签发**；受众级撤销的隔离性；身份级撤销；未申请许可被撤销不影响的边界 |
| 身份/镜像/受众 | 受众越权与消费时串用受众；镜像摘要不一致；身份未登记 |
| 一次性消费与综合交错 | 重复消费拦截并追溯到已发凭证；9 步综合时间线（重试拦截→租约替换→撤销→换许可恢复→旧凭证重放）；同节点不同构建作业并存 |
| 输入防御 | 畸形请求抛 `TypeError` 且不产生任何裁决记录 |

题面交叉约束的三类必备场景均有覆盖：**可接受结果**（完整通过链路、新实例自带测量通过、同节点并存）、**应被阻止/拒绝的结果**（测量复用、租约替换后消费旧审批、撤销后续发、受众越权、重复消费）、**影响两者的边界/交错**（生效窗口边界、审批后到期/撤销、综合时间线）。

## 范围声明

本模块只做本地、确定性的策略裁决与审计记录，不联网、不接触真实目标节点，所有测试数据均为工作区内合成夹具；不含也不生成任何攻击或探测载荷。
