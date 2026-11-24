# 访问凭证核验模块

面向内部应用的访问凭证核验模块。以**已验证的人员、任职、应用和截止时间**作为凭证内容，
依据人事系统给出的**任职区间**判断某次访问是否可用，并支持对指定历史时点的复核。

仅使用 Node.js 内置能力（`node:test`），无任何外部依赖，不联网、不读取真实系统数据，
全部验证基于 `tests/fixtures.mjs` 中的本地合成人事/授权数据。

## 领域规则与建模

| 题面规则 | 建模方式 |
| --- | --- |
| 工号跟着人走，返聘重新分配任职编号 | 任职记录主键为 `appointmentId`，同一人返聘产生新编号；`personId` 保持不变 |
| 每个应用的授权绑定到一次任职 | 授权以 `(appointmentId, appId)` 为键；新任职必须另行授权，旧授权不串用 |
| 人事记录带有效时间和修订号 | 任职记录含 `startAt/endAt`（有效区间）、`revision`（修订号，越大越新） |
| 记录可能隔天补到 | 每条记录含 `recordedAt`（登记时间）；复核可用 `asOf` 限定只看该时刻之前已登记的信息 |
| 更正曾经录错的离任日期 | 同一 `appointmentId` 录入更高 `revision`，查询时折叠为视角内最新修订 |
| 判断当下是否可用 | `verifyNow()`，时间来自可注入的 `Clock`（测试用固定时钟） |
| 复核指定时点 | `verifyAt(credential, appId, at, { asOf })`，有效时间视角 `at` × 登记时间视角 `asOf` |
| 旧任职补来的授权/凭证归原任职 | 凭证与授权均显式携带 `appointmentId`，核验按编号归属，不随人员当前任职漂移 |
| 两次任职时间冲突需人工核实 | 同一人员两段任职在访问时刻同时覆盖该点 → `MANUAL_REVIEW`，带回冲突任职对与交集区间 |

**时间区间统一为半开 `[startAt, endAt)`**：开始时刻有效，结束时刻即失效；
在任任职 `endAt = null`。凭证截止时间同为半开语义。

## 目录结构

```
src/
  time.mjs                时间规范化、半开区间判定与交集
  clock.mjs               可注入时钟（固定时钟用于确定性测试）
  hrStore.mjs             人事任职记录存储（双时间、修订折叠、冲突检测）
  authorizationService.mjs 授权/凭证登记与核验判定链
  index.mjs               公共导出
tests/
  fixtures.mjs            本地合成数据（返聘、更正、补录、冲突四条时间线）
  hrStore.test.mjs        人事存储测试
  authorizationService.test.mjs 核验服务测试
```

## 公共 API

### `new HrStore()`

- `addAppointmentRecord({ appointmentId, personId, startAt, endAt, revision, recordedAt })`
  录入任职修订。`endAt` 传 `null`/省略表示在任。同一任职编号不允许改变工号、修订号不可重复。
- `getAppointment(appointmentId, asOf?)` → 视角内最新修订；
  编号不存在抛 `AppointmentNotFoundError`，视角内尚无记录抛 `AppointmentNotYetRecordedError`。
- `listAppointmentsForPerson(personId, asOf?)` → 该工号下每个任职编号的最新修订。
- `isActiveAt(appointmentId, at, asOf?)` → 任职有效区间是否覆盖 `at`。
- `findOverlapsAt(personId, at, asOf?)` → `at` 时刻同时覆盖该点的冲突任职对（含半开交集）。
- `listAllOverlaps(personId, asOf?)` → 该人员全部两两任职重叠区间。

### `new AuthorizationService(hrStore, clock?)`

- `registerAuthorization({ appointmentId, personId, appId, recordedAt })`
  登记/补录应用授权，授权绑定一次任职；同键重复登记按最新记录幂等覆盖。
- `registerCredential({ credentialId, personId, appointmentId, appId, issuedAt, expiresAt, recordedAt? })`
  登记凭证，内容即人员、任职、应用、截止时间；`recordedAt` 默认取 `issuedAt`。
- `verifyNow(credential, appId, options?)` → 以时钟当前时刻调用 `verifyAt`。
- `verifyAt(credential, appId, at, { asOf }?)` → 核验结果对象：

```js
{
  decision: 'ALLOW' | 'DENY' | 'MANUAL_REVIEW',
  reason: string | null,         // DENY/MANUAL_REVIEW 时为稳定原因码
  conflicts: [],                 // MANUAL_REVIEW 时为冲突任职对及交集区间
  at, asOf, credentialId, appointmentId, personId, appId,
}
```

`credential` 可传凭证对象或已登记的 `credentialId`；`asOf` 省略时使用全部已知记录（按最新修订复核）。

### 判定链（短路顺序）

1. 凭证在视角内存在且已登记（`CREDENTIAL_NOT_FOUND` / `CREDENTIAL_NOT_YET_ISSUED`）
2. 任职存在且视角内已登记（`APPOINTMENT_NOT_FOUND` / `APPOINTMENT_NOT_YET_RECORDED`）
3. 同一人员任职在 `at` 时刻冲突 → `MANUAL_REVIEW`（`APPOINTMENT_OVERLAP`，优先于一切自动放行）
4. `at` 落在任职有效区间内（`APPOINTMENT_NOT_ACTIVE`）
5. 凭证工号与任职一致、凭证应用与目标应用一致（`CREDENTIAL_PERSON_MISMATCH` / `CREDENTIAL_APP_MISMATCH`）
6. 该任职对目标应用存在视角内授权（`AUTHORIZATION_NOT_FOUND` / `AUTHORIZATION_NOT_YET_RECORDED`）
7. 凭证未到截止时间（`CREDENTIAL_EXPIRED`）
8. 全部通过 → `ALLOW`

## 运行方法

要求 Node.js ≥ 24（开发与实测环境：v24.15.0）。

```bash
node --test tests/*.test.mjs
# 或
npm test
```

## 验证范围（本地合成数据，均有断言实测）

合成时间线见 `tests/fixtures.mjs`，共 34 个测试用例，覆盖：

- **正常场景**：在任 + 授权 + 未过期放行；`verifyNow` 固定时钟判定当下；真实时钟分支。
- **边界（半开语义）**：任职开始时刻放行、离任时刻拒绝；凭证截止前一刻放行、截止时刻过期；
  在任任职（`endAt=null`）持续覆盖；任职间隙期拒绝。
- **返聘交错**：新任职需新凭证/新授权；旧凭证在返聘期被拒（任职不活跃）；
  旧凭证复核原任职区间仍归原任职并放行；新凭证不能回溯到旧任职；应用级授权隔离（APP-X/APP-Y）。
- **修订更正**：错误离任日（2/28）经修订2 更正为 3/31；更正前视角拒绝、更正后与最新视角放行；
  修订消除任职冲突后，最新视角恢复自动判定而历史视角保留人工核实。
- **隔天补录**：任职记录补登前的视角报 `APPOINTMENT_NOT_YET_RECORDED`，补登后复核同一访问放行；
  授权晚登记视角报 `AUTHORIZATION_NOT_YET_RECORDED`；凭证早于视角签发报 `CREDENTIAL_NOT_YET_ISSUED`。
- **冲突人工核实**：重叠时段内任一凭证都转 `MANUAL_REVIEW` 并带回冲突对；
  重叠左端点即触发、右端点（半开）自动恢复；人工核实优先于凭证过期。
- **归属篡改/材料缺失**：未知凭证、凭证工号与任职不一致、应用不一致、任职无对应授权、任职编号不存在。
- **存储校验**：修订号合法性、`endAt > startAt`、同任职工号一致性、修订号唯一性。
- 全部 11 个 `Reason` 原因码均有对应断言（实测通过，无跳过用例）。

## 边界与如实说明

- 本模块只实现题面规定的**防御性核验判断**，不含任何利用、入侵、攻击或规避逻辑。
- 模块为内存存储、单进程内调用，未涉及持久化、并发写入与分布式一致性；这些不在题面要求内。
- 冲突检测只比较同一 `personId` 下不同 `appointmentId` 的任职；同一任职的历史修订在查询时折叠为最新值。
- 未接入真实人事/授权系统，所有数据均为虚构合成数据；未做也不需要网络访问或依赖安装。
