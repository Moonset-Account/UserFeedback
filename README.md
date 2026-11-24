# 企业登录绑定流程模块

为账户服务提供一个**标签页范围**的企业身份绑定流程：多个浏览器标签可以同时为不同的本地工作资料发起绑定，回调会乱序、重送，用户可能取消，两个标签也可能选中同一个企业身份。

实现为纯内存、零依赖的 ESM 模块，运行于 Node.js 24.15.0。

```
src/binding-flow.mjs              实现
tests/binding-flow.basic.test.mjs       单标签正常流程、参数校验
tests/binding-flow.race.test.mjs        乱序、重送、并发竞争、取消
tests/binding-flow.interleaving.test.mjs 随机交错与全局不变量
examples/demo.mjs                 可直接运行的端到端演示
```

## 运行

```bash
node --test tests/*.test.mjs
# 或
npm test

# 端到端演示
node examples/demo.mjs
```

无需安装依赖，不联网。

## 核心概念

- **登记（registration）**：一次绑定尝试。启动时记录 `tabId`（发起标签）、`localProfileId`（本地工作资料）、`issuer`（企业身份源）和 `registrationId`（本次登记号）。
- **身份返回（identity return）**：身份服务回调。模块**只接收调用方已校验**的断言——`issuer` 与 `subject` 已由上游签验，本模块不做签名验证，只做一致性、去重和归属判断。
- **绑定（binding）**：唯一约束为 `(issuer, subject)` 组合最多关联一份 `localProfileId`。

## 公开 API

`createBindingFlow(options?)` 返回一个不可变的方法集合。

### 选项

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `now` | `() => Date.now()` | 时钟，测试可注入。 |
| `generateBindingId` | `bind_<uuid>` | 绑定 id 生成器。 |
| `generateRegistrationId` | `reg_<uuid>` | 未提供登记号时使用，保证不与已用 id 冲突。 |
| `maxDeferredReturns` | `1000` | 等待登记的乱序返回到达上限，超出抛 `DEFERRED_RETURN_CAPACITY`。 |

### `startBinding({ registrationId?, tabId, localProfileId, issuer })`

记下发起标签、本地资料、身份源和登记号，状态进入 `pending_identity`。

- `registrationId` 可省略，由模块生成并返回。
- 同一 `registrationId` 以**完全相同**的参数重复调用是幂等的（`created: false`），返回既有登记；参数不同则抛 `REGISTRATION_ID_CONFLICT`——登记号不能被改绑到别处。
- 返回 `{ created, deferredReturnApplied, registration }`。若此前有同一登记号的乱序返回到达，来自**本登记所选身份源**的那条会被立即应用（`deferredReturnApplied: true`），登记直接进入 `awaiting_confirmation`。

### `receiveIdentityReturn({ registrationId, issuer, subject, eventId? })`

接收身份服务已校验的返回。

- 登记不存在时**暂存**该返回（`outcome: "deferred"`），等待对应登记启动——用于回跳早于登记落库的乱序场景。暂存不创建登记，也不会被跨身份源误用。
- 身份源必须与启动时选择的一致，否则抛 `ISSUER_MISMATCH`。
- 重送去重：同一 `eventId` 视为重复（`duplicate`，`EVENT_ALREADY_PROCESSED`）；新事件但同一身份也视为重复（`ASSERTION_ALREADY_RECEIVED`）。
- 同一 `eventId` 若被用于**不同身份**，抛 `IDENTITY_RETURN_CONFLICT`——事件号是幂等键，不能被回收。
- 同一登记先后收到不同 `subject` 的已校验断言，抛 `IDENTITY_RETURN_CONFLICT`，首个断言保持不变。
- 已取消 / 已冲突的登记上的返回被忽略（`ignored`），不能复活登记。
- 已绑定登记上的返回：身份一致为 `duplicate`，不一致为 `ignored`。

返回 `{ outcome, reason, registration, identity }`，`outcome ∈ {accepted, duplicate, ignored, deferred}`。

### `confirmBinding({ registrationId, tabId })`

用户在**发起标签**内确认。

- 只有发起标签能确认（否则 `TAB_MISMATCH`）；尚无已校验返回时抛 `IDENTITY_NOT_READY`；已取消时抛 `REGISTRATION_CANCELLED`。
- **在确认时才做 `(issuer, subject)` 唯一性判断**：方法同步执行，检查与写入构成一个原子临界区，因此确认顺序即竞争结果。
  - 该组合尚无绑定 → 创建绑定，`outcome: "bound"`。
  - 已有绑定且指向**同一** `localProfileId` → 复用原绑定，`outcome: "already_bound"`，返回原 `bindingId` 与 `createdAt`。
  - 已有绑定且指向**不同** `localProfileId` → 记 `conflict`，`outcome: "conflict"`，携带 `conflict.occupiedBy.{bindingId, localProfileId, registrationId, boundAt}` 给出占用原因；该登记**保留自己的** `localProfileId`，不采用胜者资料。
- 重复确认：已绑定登记再次确认始终返回原来的绑定（`already_bound`）；冲突登记再次确认返回同一份冲突记录（幂等）。
- 返回 `{ outcome, binding, conflict, registration }`。

### `cancelBinding({ registrationId, tabId, reason? })`

取消登记，状态进入 `cancelled`。

- 只有发起标签能取消（否则 `TAB_MISMATCH`）；`reason` 默认 `USER_CANCELLED`。
- 取消是终态：后续返回被忽略、确认被拒、以相同登记号重启只会拿到 `cancelled` 的既有登记（新尝试需新登记号）。
- 已取消登记再次取消返回 `already_cancelled`，保留原 `cancelledAt`/`cancelReason`，不覆盖。
- 已绑定登记不能经此取消，抛 `ALREADY_BOUND`。
- 返回 `{ outcome, registration }`，`registration` 明确带 `cancelledAt`、`cancelledByTabId`、`cancelReason`。

### 查询

- `getRegistration(registrationId)` → 快照或 `undefined`。
- `listRegistrations({ tabId?, status? })` → 按 `startedAt` 排序的快照数组。
- `getBindingForIdentity({ issuer, subject })` → 绑定快照或 `undefined`。
- `listBindings({ issuer?, subject?, localProfileId? })` → 按 `createdAt` 排序的绑定快照数组。

所有返回值均为冻结的不可变视图，外部修改不会影响内部状态。

## 状态机

```
startBinding           receiveIdentityReturn
pending_identity ───────────────────────────► awaiting_confirmation
     │                                              │
     │ cancel                                       │ confirm
     ▼                                              ├──────────────► bound
  cancelled ◄───────────────────────────────────────┤   (无占用 / 同资料复用)
     (终态)                                          └──────────────► conflict
                                                         (他资料占用，终态)
```

`cancelled` 与 `conflict` 是终态；`bound` 不可取消。

## 并发语义

模块内所有操作同步执行，检查与写入之间无 `await`，因此单进程内天然串行化。多标签的“竞争”表现为**调用顺序**：

- **两个标签选中同一企业身份**：先调用 `confirmBinding` 的标签成功绑定；后调用者得到 `conflict`，`occupiedBy` 指明占用者，其本地资料保持不变。
- **重复成功确认**：返回首次绑定的原结果（`already_bound` + 原 `bindingId`/`createdAt`）。
- **乱序回调**：返回按 `registrationId` 路由，暂存机制处理“返回先于登记”的情况；不同登记的返回互不干扰。
- **重送回跳**：按 `eventId` / 身份去重。

若需跨进程或多实例一致性，应由调用方在同一存储层上加 CAS/唯一约束；本模块的语义可直接作为该约束的判定逻辑。

## 验证范围

均已在本工作区用 `node --test` 实测通过（44 项，全绿）：

- **正常流程**：单标签启动 → 返回 → 确认 → 绑定，绑定可被 `(issuer, subject)` 反查；身份接收本身不创建绑定。
- **乱序**：返回早于登记（同/异身份源）、跨标签交错投递、8 标签逆序投递、暂存槽在登记启动后释放。
- **重送**：同 `eventId`、新事件同身份、绑定后重送、暂存后重送；同一 `eventId` 改绑不同身份被拒；被异源启动丢弃的暂存事件号不污染新登记。
- **并发冲突**：两标签同一身份先确认者胜、后确认者冲突并保留自身资料、冲突终态忽略迟到返回、冲突可重复确认且结果幂等、胜者重复确认拿原绑定、同资料跨标签复用绑定。
- **取消**：显式状态与时间戳、迟到返回不复活、重复取消幂等、取消不影响另一标签、已绑定不可取消、取消号不能重启。
- **边界/安全**：发起标签专属确认与取消、身份源匹配、参数校验与长度上限、冻结视图、`(issuer, subject)` 键无拼接歧义、暂存上限、绑定 id 碰撞拒绝。
- **随机交错**：5 个固定种子的确定性 PRNG，随机投递/确认顺序下校验“每个身份至多一个绑定、绑定指向真实资料与登记、胜者资料与首次确认一致、重复确认返回原绑定”等全局不变量；取消与确认随机交错下无半绑定状态。

### 未覆盖

- 身份断言的密码学验签（由上游身份服务完成，模块只接收已校验结果）。
- 跨进程/跨实例持久化与分布式一致性（模块为单进程内存实现）。
- 真实浏览器标签生命周期、`window`/`BroadcastChannel` 集成。
