# 通行密钥登记模块（Passkey Registration）

面向“浏览器可能在两个标签页同时发起通行密钥登记”的并发场景，实现绑定到**已登录账户**的
一次性挑战登记流程，并消费验签组件返回的**可信凭据结果**。纯内存、零依赖、ESM，运行于
Node.js 24.15.0。

## 实际入口

- 库入口：`src/index.js`（统一导出服务、存储、验签组件与错误类型）
- 核心服务：`src/passkey-registration-service.js` 的 `PasskeyRegistrationService`
- 测试入口（真实断言，`node:test` + `node:assert/strict`）：
  - `tests/registration.test.mjs`：单标签页核心规则与幂等
  - `tests/concurrency.test.mjs`：多标签页 / 跨账户交错裁决
- 运行方式（本环境已实际核验）：

  ```bash
  node --test tests/*.test.mjs
  ```

  `npm test` 等价于上面的命令（本工作区执行策略未放行 `npm`，故以 `node` 直接运行为准）。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `src/passkey-registration-service.js` | 登记编排：发起、冻结、完成、幂等回放、查询 |
| `src/account-session-store.js` | 已登录账户会话；新会话替换旧会话，旧会话标记 `replaced` |
| `src/registration-store.js` | 登记会话与一次性挑战索引；状态 `pending/frozen/succeeded/rejected` |
| `src/credential-store.js` | 凭据编号全局唯一绑定（首绑生效） |
| `src/trusted-credential.js` | 验签组件可信结果类型，实例冻结防篡改 |
| `src/verifier.js` | 本地合成验签组件（`LocalVerifier`），可编排成功/失败结果 |
| `src/errors.js` | `RegistrationError` 与错误码 `ErrorCodes` |

## 规则与实现映射

1. **一次性挑战绑定已登录账户**：`startRegistration` 要求传入 active 的账户会话；
   挑战值全局唯一，重复签发抛 `CHALLENGE_ALREADY_USED`。
2. **冻结的登记会话不得完成**：`freezeRegistration` 后完成回调落为
   `rejected / REGISTRATION_FROZEN`，不产生绑定。
3. **已替换的账户会话不得完成**：`replaceAccountSession` 使旧会话变为 `replaced`；
   挂在旧会话上的登记完成时落为 `rejected / SESSION_REPLACED`。每个登记按**自身绑定的
   会话**判断，新会话上的登记不受影响。
4. **同一凭据编号只首绑到一个账户**：`credentialId` 全局唯一。同账户重复绑定 →
   `CREDENTIAL_ALREADY_BOUND`；跨账户抢占 → `CREDENTIAL_BOUND_TO_OTHER_ACCOUNT`，
   并在 `details.existingAccountId` 给出已占用账户。
5. **重复回调返回原有结果**：以 `registrationId` 为幂等键，已终结登记（成功或拒绝）的
   后续调用回放首次 `outcome`，响应额外带 `replay: true`；即使第二次携带不同凭据也不改变
   首次结果。
6. **另一标签页的未完成挑战按自己的状态判断**：登记会话相互独立，一个冻结/失败不影响
   另一个 `pending` 登记；完成路径为同步原子操作（不跨 `await`），并发回调时首绑判定
   不会被交错破坏。
7. **可信边界**：完成回调只接受 `TrustedCredential` 实例；普通对象、验签失败结构一律
   落为 `rejected / NOT_TRUSTED_CREDENTIAL`。
8. **查询面**：`listCredentials(accountId)` 返回当前账户可用凭据（仅成功绑定）；
   `listRegistrations(accountId)` 返回每次登记的完整状态历史；所有返回值均为快照，
   外部修改不影响内部状态。

## 验证范围（16 个用例，全部通过）

**可接受结果**
- 已登录账户凭一次性挑战成功登记，凭据进入可用列表，登记状态为 `succeeded`。

**应被阻止 / 拒绝的结果**
- 普通对象伪装验签结果 → 拒绝，无绑定；
- 一次性挑战重复签发 → 类型化错误；
- 冻结登记完成 → `REGISTRATION_FROZEN`；
- 会话替换后旧会话登记完成 → `SESSION_REPLACED`；
- 同账户重复绑定同一凭据 → `CREDENTIAL_ALREADY_BOUND`；
- 验签组件失败结构 → 拒绝且无绑定；
- 未知登记 / 未知账户会话 → `RegistrationError`。

**影响两者的边界与交错场景**
- 跨账户双标签页抢同一凭据：首绑生效，败方拒绝且其另一条未完成挑战换凭据后仍可成功；
- 同账户双标签页抢同一凭据：先成后拒，双方重复回调各自回放；
- 冻结一个标签页不影响另一标签页 `pending` 挑战；
- 会话替换与冻结并存时按各登记自身状态分别裁决，之后新登记仍可成功；
- 成功结果幂等回放（第二次携带不同凭据仍返回首次结果）；拒绝结果幂等回放同一原因；
- 查询面覆盖 `pending/frozen/rejected/succeeded` 全状态且返回快照不可回写。

## 设计边界

- 本模块只做防御性的登记状态机与并发裁决，不发起任何网络请求、不进行真实设备探测；
  验签环节由可注入的 `Verifier` 接口承担，测试使用 `LocalVerifier` 与本地合成夹具。
- 存储为进程内内存结构，进程重启数据即失效；接入持久化存储时替换三个 Store 即可，
  服务层的同步原子语义需在事务/行锁内保持。
