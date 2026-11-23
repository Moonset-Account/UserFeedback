# 幂等执行入口（Idempotent Execution Entry）

面向 RPC 服务端的幂等执行入口。调用方可能在业务已经生效之后才收到超时，因此入口以
「业务键 + 参数」为身份，用**稳定的执行号**驱动业务适配器；同一键的并发调用共享一次
业务执行，而每个调用方的等待各自独立结束。进程重启后，任何未确认的记录都先查证再继续，
**不会**用新执行号重跑。

运行时：Node.js **24.15.0**，仅使用标准库（`node:crypto`、`node:test`、`node:assert`、
`node:timers/promises`），`ECMAScript modules`，无第三方依赖。

## 目录

```
src/params.mjs    参数校验与规范化身份（顺序无关、类型精确）
src/store.mjs     有界、插入有序的记录表（只能按窗口淘汰，无任意删除）
src/server.mjs    入口：接纳、单飞执行、等待、冲突、容量、重启恢复
tests/*.test.mjs  node:test 测试（正常 / 边界 / 崩溃交错）
tests/helpers/    合成业务适配器与可注入时钟
```

## 公开 API

```js
import { createIdempotentExecutionServer, parameterIdentity } from './src/server.mjs';

const server = createIdempotentExecutionServer({
  adapter,                 // 必填：{ execute(req), query(req) }
  capacity: 1024,          // 有限幂等缓存容量
  executionIdFactory,      // 可选：默认 randomUUID()
  clock,                   // 可选：() => epoch ms，默认 Date.now
  timers,                  // 可选：{ setTimeout, clearTimeout }
  snapshot,                // 可选：上次 snapshot() 的产物，用于重启恢复
});
```

### `adapter`（业务适配器契约）

- `execute({ executionId, businessKey, operation, retryDeadline, hadReceipt, freshVerify })`
  必须按 `executionId` 幂等：同一执行号至多生效一次。返回
  `{ ok: true, outcome: 'effective', effect }`、
  `{ outcome: 'not_effective' }` 或 `{ outcome: 'unknown' }`。
- `query({ executionId, businessKey, operation, retryDeadline })`
  凭稳定执行号查询最终效果，返回 `{ status: 'effective', effect }`、
  `{ status: 'not_effective' }` 或 `{ status: 'unknown' }`（暂时未知）。

服务端把「抛异常」和「未知」同等对待：都无法区分「未生效」与「已生效」。

### `submit({ businessKey, params, retryTtl, deadline, signal? })`

唯一的执行入口。`retryTtl` 是接纳后该键绑定这些参数的时长（毫秒）；`deadline` 是
**本次调用方**停止等待的绝对时刻。返回 `accepted: true` 时不等待全局、只等到自己的
`deadline`，其中 `status` 为：

| `status` | 含义 |
| --- | --- |
| `effective` | 已确认生效，`effect` 为本调用方的回执 |
| `pending` | 本次等待结束但执行仍在进行（或尚未开始重试） |
| `unknown` | 业务效果暂时无法确认，稍后用同一执行号查证 |

`accepted: false` 时 `reason` 为：

| `reason` | 场景 |
| --- | --- |
| `INVALID_KEY` / `INVALID_PARAMS` / `INVALID_DEADLINE` | 输入非法 |
| `PARAMETER_MISMATCH` | 同键不同参数，且仍在重试期限内（附 `retryDeadline`、`retryInMs`） |
| `PENDING_CONFIRMATION` | 键已过期限，但上一笔效果仍未知，必须先行查证才能改绑 |
| `CAPACITY` | 缓存已满，且**所有**记录都还在重试期限内（或仍在执行/未确认） |

### `recover({ businessKey })` / `recoverAll()` / `status()` / `snapshot()` / `inspect()`

- `recover`：查证单个键。已知执行号 → `query`；返回 `unknown` 时如实返回「仍在确认」，
  绝不新开执行号。只有业务确认 `not_effective`（且从未发出过回执）才用**同一执行号**继续。
- `recoverAll`：启动时对全部驻留记录逐一查证。
- `status`：非阻塞读取 `{ status, executionId, retryDeadline, safeToReuse, delivered, error }`。
- `snapshot`：重启用的持久视图；把产物传给新实例的 `snapshot` 选项即可。
- `inspect`：诊断视图，不属于 RPC 公开形状。

## 设计要点

1. **写入先于调用**：执行号在调用 `adapter.execute()` 之前即写入记录。这样「执行已开始、
   效果未记录」之间崩溃时，重启仍能凭该执行号查证。
2. **恢复即假设**：重启后所有带执行号的记录一律视为 `unknown`，只经 `query` 确认；
   没有执行号的记录才允许首次执行（不可能重复）。
3. **未知不可重跑**：`unknown` 记录既不会被淘汰，也不会被换执行号；只有业务明确
   `not_effective` 才允许同一执行号续跑。已发出回执的键即使业务后来否认，也拒绝重跑。
4. **单飞 + 各自的等待**：每个业务键至多一个在途执行；调用方的 `deadline`/`signal` 只影响
   自己的等待，不需要等到全局，也不会取消共享执行。
5. **参数身份**：对象成员顺序无关；数组顺序、`0`/`-0`、`null`/缺省、数字/字符串严格区分；
   拒绝函数、`NaN`、`BigInt`、`Date` 等无稳定身份的值，避免不同参数被误判为同一键。

## 复现与验证

```bash
node --version          # 应为 v24.15.0
node --test 'tests/*.test.mjs'
```

各文件覆盖的场景：

| 文件 | 覆盖 |
| --- | --- |
| `tests/idempotency.test.mjs` | 单次生效、同键同参共享、成员顺序等价、身份区分、非法参数、参数防篡改 |
| `tests/concurrency.test.mjs` | 并发共享一次执行、各自等待独立结束、abort、25 路并发、不同键独立 |
| `tests/binding-capacity.test.mjs` | 期限内冲突、期限边界释放、不可延长窗口、未知时不改绑、缓存满拒绝新键、过期淘汰、执行中不淘汰 |
| `tests/recovery.test.mjs` | 执行前/生效后崩溃、回执丢失后两个客户端先后取回、`unknown` 保持确认中、适配器抛异常、全量恢复 |
| `tests/interleaving.test.mjs` | 生命周期四个崩溃点、恢复中的并发调用、超时后重试复用、恢复期冲突 |

### 关键验收场景：生效一次、回执丢失、重启后两个客户端先后取回

`tests/recovery.test.mjs` 的 *two clients fetch the same response one after another*
即题面要求的情形，实际执行路径为：

1. 首次 `submit` → 业务已生效，响应丢失 → 调用方得到 `unknown`（`business.applied` 长度 1）。
2. `snapshot()` → 新实例 `restart` → `recover` 用同一执行号 `query` 得到 `effective`。
3. 客户端一 `submit` 得到回执；客户端二随后以相同参数（成员顺序不同）`submit`，
   拿到**同一回执**，`join: 'record'`。
4. 断言：`business.executeCalls.length === 1`，`effectsFor(key).length === 1`。

## 验证范围与未覆盖项

- 已真实执行：上表全部 36 个测试，均通过（`node --test` 输出为准，连续三次运行结果一致）。
- 业务适配器为**本地合成实现**，用于观测真实生效次数与执行号；未接入任何真实服务、
  网络或账户。
- **未覆盖**：真实跨进程崩溃（测试用 `snapshot()` + 新实例模拟重启）、真实网络超时、
  多实例共享存储、持久化到磁盘的原子写。实现把「记录持久化」抽象为 `snapshot`，
  真实部署需替换为事务性存储。
- 容量淘汰按插入顺序选取「已过重试期限且可回收」的最早记录；未实现 LRU 或 TTL 后台清理。
