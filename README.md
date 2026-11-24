# 接续认证模块（Session Continuation Authentication）

共享协作终端在使用者临时离开时锁屏，并在其返回后接续原会话；另一位成员确认接手时，
终端切换到该成员的新会话。本模块只消费**模拟认证设备**提供的已校验事件，不持有凭据、
不执行真实认证，也不访问任何真实目标。

## 判定规则

模拟设备对每台终端维护单调递增的**接入编号**（`accessNo`，从 1 开始），并产生两类事件：

| 事件 | 形状 | 含义 |
| --- | --- | --- |
| 进出报告 | `{ type:'presence', memberId, terminalId, timestamp, accessNo, status:'in'|'out' }` | 成员进入/离开终端感应区 |
| 认证确认 | `{ type:'auth', memberId, terminalId, timestamp, accessNo }` | 成员在认证设备上完成确认 |

状态机（每台终端独立）：

1. **认证确认开会话**：空闲终端收到 `auth` → 建立该成员的活跃会话。
2. **离场即锁屏**：活跃会话成员的 `presence/out` → 终端 `locked`，会话保留。
3. **宽限内恢复**：锁屏后 `gracePeriodMs` 内，同一成员的 `auth` → 恢复**原会话**
   （边界为闭区间：`auth.timestamp - lockedAt <= gracePeriodMs` 可恢复）。
4. **宽限过期**：超过宽限后的同成员 `auth`，或 `sweepExpired()` 定时清理 → 旧会话以
   `grace-expired` 结束，另开新会话。
5. **成员接手**：任何状态下另一成员的 `auth` → 旧会话以 `taken-over` 结束，
   终端进入新成员的新会话。
6. **在场集合**：`presence/in` 登记、`presence/out` 移除；非当前用屏人的离场只记录，
   不会锁屏。

### 乱序处理策略（`ordering` 构造参数）

- **`sequence`（默认）**：严格按接入编号定序。
  - 编号大于"下一个期望编号"的事件先缓冲（`buffered`），缺口补齐后按序连续回填，
    回填动作随触发事件的返回值 `drained` 一并报告。
  - 同编号同载荷视为幂等重发（`duplicate`）；同编号不同载荷拒绝（`rejected/access-no-conflict`）。
- **`watermark`**：到达即应用，维护已处理水位。
  - 编号高于水位且等于"水位+1"的事件正常应用；跳号缺口不回退水位，只归档。
  - 编号不大于水位的事件属于历史窗口：同载荷为 `duplicate`，其余判定为**迟到（`stale`）**，
    只追加到该成员最近一次会话的审计记录（无历史会话则进孤儿记录），**不改变当前用屏人**。
  - 这精确对应题面"接手后晚到的旧用户确认留在旧记录中，当前用屏人继续使用他的会话"。

## 公开 API

入口：`index.js`（ESM，无第三方依赖，Node.js ≥ 24.15.0）

```js
import {
  SessionContinuationService, // 核心服务
  SimulatedAuthDevice,         // 模拟认证设备（测试夹具，合成事件）
  createAuthEvent,             // 事件工厂（带校验）
  createPresenceEvent,
  validateEvent,
  TERMINAL_STATUS,             // 'idle' | 'active' | 'locked'
  SESSION_STATUS,              // 'active' | 'locked' | 'ended'
  ORDERING,                    // 'sequence' | 'watermark'
} from './index.js';
```

### `new SessionContinuationService(options?)`

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `gracePeriodMs` | `60000` | 锁屏宽限（毫秒），非负数，`0` 表示锁屏即不可恢复 |
| `ordering` | `'sequence'` | 乱序策略：`ORDERING.SEQUENCE` 或 `ORDERING.WATERMARK` |
| `clock` | `Date.now` | 注入时间源，测试使用虚拟时钟 |

非法构造参数抛出 `TypeError`。

### `service.handleEvent(event)` → 结果信封

```js
{
  status: 'applied' | 'buffered' | 'stale' | 'duplicate' | 'rejected',
  terminalId, accessNo, event,
  action,      // 状态机动作，见下表；buffered 时为 'buffered'
  sessionId,   // 动作归属/归档的会话，无法归属时为 null
  reason,      // rejected 时给出，如 'access-no-conflict'
  waitingFor,  // buffered 时当前缺口编号
  drained,     // sequence 回填时本次连带应用的事件摘要
}
```

主要 `action`：`session-started`、`auth-reaffirmed`、`locked-on-leave`、
`session-resumed`、`new-session-after-grace`、`takeover-session-started`、
`presence-in-recorded`、`presence-in-active`、`presence-out-recorded`、`stale-ignored`。

事件结构非法（缺字段、类型错误、编号非正整数、`presence.status` 非法等）抛出 `TypeError`，
错误信息聚合全部非法字段。

### 查询与维护

- `sweepExpired(now?)`：结束超过宽限仍锁屏的会话，终端回到 `idle`，返回结束的会话 id 列表。
- `getTerminalState(terminalId)`：终端状态快照（含在场成员、缓冲编号）；未知终端返回 `null`。
- `getCurrentSession(terminalId)` / `getSession(terminalId, sessionId)` /
  `listSessions(terminalId)`：会话快照（深拷贝，含已结束会话）。
- `getAuditTrail(terminalId)`：合并全部会话记录与孤儿记录、按接入编号排序的审计轨迹。
- `getPresentMembers(terminalId)`：当前在场成员 id（按进入顺序）。
- 所有查询返回深拷贝，外部修改无法污染内部状态。

### `SimulatedAuthDevice`（测试夹具）

```js
const device = new SimulatedAuthDevice({ clock });
device.enter(memberId, terminalId, timestamp?);        // presence/in
device.leave(memberId, terminalId, timestamp?);        // presence/out
device.authenticate(memberId, terminalId, timestamp?); // auth
device.nextAccessNo(terminalId);                       // 下一个编号（断言用）
```

每台终端独立编号；不传时间戳时使用注入时钟的当前时间。

## 运行方法

```bash
node --test tests/*.test.mjs
# 或
npm test
```

无需安装任何依赖（`package.json` 不声明 dependencies）。

## 验证范围（本地合成数据，23 个用例全部通过）

测试使用手动推进的虚拟时钟，不接触真实时间、网络或凭据。

1. **正常生命周期**：认证开会话 → 进入 → 离场锁屏 → 宽限内同成员恢复同一会话（含 `resumeCount`）。
2. **宽限边界**：恰好等于宽限期可恢复；超出 1ms 旧会话 `grace-expired` 并另开新会话；
   宽限为 0 时锁屏即不可恢复。
3. **定时清理**：`sweepExpired` 只清理超宽限的锁屏终端，未超宽限保持锁屏。
4. **成员接手**：活跃态与锁屏态下另一成员确认均结束旧会话（`taken-over`）并进入新会话。
5. **迟到归档（题面核心交错）**：watermark 下接手后晚到的旧成员 `auth`/`presence` 判定为
   `stale`，进入旧会话审计记录；当前用屏人、终端状态、水位均不变，且随后仍可正常锁屏/恢复。
6. **乱序与回填**：sequence 下缺口缓冲、补齐后连续回填（含回填触发的锁屏与后续恢复）；
   缓冲内外的幂等重发与同号冲突分别处理。
7. **watermark 守卫**：跳号缺口不回退水位；历史窗口异载荷事件归档为 `stale`，
   迟到的他人确认不能接手；水位+1 的真实新事件正常应用并可接手。
8. **防御性校验**：非法事件/构造参数/清理时间抛 `TypeError`；未知终端查询返回安全空值；
   查询快照为深拷贝；多终端状态与编号相互独立；孤儿事件（无会话时的进出报告）可追溯。

## 安全边界

- 仅实现题面规定的防御性判断（事件结构校验、接入编号定序、状态机授权判定、审计留痕）。
- 不含、不读取、不存储任何凭据；不发起任何网络请求；无攻击、利用或规避逻辑。
- 模拟设备是测试夹具，其"已校验成员"身份仅在合成数据上下文中可信。
