# 双路回放模块（dual-path-replay）

给服务压测器使用的双路离散事件回放模拟器。基线（baseline）与候选（candidate）
拿到**同一份**初次到达安排，各自独立地按会话依赖、同会话并发上限与重试政策派发请求；
一边应答快不会提前另一边的初始负载，一边的失败重试也只在本路发生、仍归属原逻辑请求。

纯 Node.js 标准库、ECMAScript modules、无第三方依赖，运行时要求 Node.js 24.15.0。

## 解决的问题

基线里有请求一直不返回、候选很快结束时，需要证明：

1. 两路下一轮的初始到达记录仍能逐条对上（共享到达安排，路间不互相提速）；
2. 实际发出尊重各自的会话前序与同会话并发上限；
3. 一次失败后的重试在本路独立发生，仍是原逻辑请求的一次尝试，不产生新的原始负载；
4. 每路都能给出计划、实际发出、从未发出和最终完成四类结果；
5. 配对既不会把额外重试当成另一组负载，也不会只剩两条线上都成功的请求；
6. 结果能解释同一会话的等待为何只在一边增加，并显示另一会话未被拖延。

## 目录结构

```
src/
  records.mjs        输入记录与路配置的校验、规范化
  priority-queue.mjs 最小二叉堆（同键 FIFO，保证模拟确定性）
  engine.mjs         单路离散事件引擎（虚拟时钟，不使用真实时间）
  report.mjs         每路汇总 + 按逻辑 ID 的双路配对
  explanation.mjs    挂起/重试定位、会话等待对比与文字解释
  index.mjs          对外入口
tests/
  engine.test.mjs    单路引擎测试（正常/边界/交错，22 个）
  dual-path.test.mjs 双路验收测试（题面场景/隔离/恢复/多轮，10 个）
examples/
  headline-scenario.mjs 题面场景的可运行示例（演示输出，不替代测试）
```

## 输入格式

### 请求记录（两路共享同一份数组）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `logicalId` | string | 逻辑请求 ID，全局唯一；重试不生成新 ID |
| `round` | 正整数，可选 | 到达轮次，默认 1；多轮初始负载用它区分 |
| `plannedArrival` | 非负整数 | 计划初次到达时刻（虚拟毫秒） |
| `sessionId` | string | 所属会话；并发上限与依赖都在会话内生效 |
| `predecessor` | string \| null | 同会话前序逻辑 ID；前序成功后才可发出 |
| `retryPolicy.maxAttempts` | 正整数 | 最大尝试次数（含首次） |
| `retryPolicy.backoffMs` | 非负整数 | 失败后到下一次尝试的退避时长 |

前序引用必须存在且属于同一会话，规范化阶段直接报错。

### 路配置（baseline / candidate 各一份）

| 字段 | 说明 |
| --- | --- |
| `sessionConcurrency` | 每个会话的同会话在途上限（正整数） |
| `outcome(logicalId, attempt)` | 每次尝试的结果回调，返回：<br>`{ status: "success", latencyMs }`<br>`{ status: "failure", latencyMs, retryable? }`（`retryable` 默认 true）<br>`{ status: "hang", latencyMs? }`（永不返回，持续占用会话槽位直到截止） |
| `deadlineMs` | 可选截止时刻；不填则跑到自然结束。`t = deadline` 的事件仍处理 |

## 公开 API

```js
import { runDualPathReplay, runSinglePath, FINAL_STATUS } from "./src/index.mjs";

const { report, explanation } = runDualPathReplay(records, {
  baseline: { sessionConcurrency, outcome, deadlineMs },
  candidate: { sessionConcurrency, outcome, deadlineMs },
});
```

- `report.sharedArrivalSchedule`：两路共享的规范化到达安排。
- `report.paths.baseline` / `report.paths.candidate`：每路
  - 计数：`plannedCount`、`sentCount`、`neverSentCount`、`successCount`、
    `failedCount`、`inFlightCount`、`totalAttempts`、`retryAttempts`；
  - `requests[]`：每个逻辑请求的终态（`succeeded` / `failed` / `in-flight` /
    `never-sent`）、每次尝试的 `sentAt`/`completedAt`/`outcome`、首发等待 `waitMs`、
    从未发出原因 `blockingReason`（`dependency` / `concurrency-cap` /
    `deadline-before-arrival` / `retry-not-reached`）；
  - `sessions[]`：按会话汇总的最大/平均等待（从未发出的请求按截止时刻计入等待）。
- `report.pairing.pairs[]`：按 `logicalId` 的双路配对，每个元素含两路各自的终态、
  尝试明细与 `extraRetries`。配对数恒等于逻辑请求数：重试嵌套在 `attempts` 内，
  不会膨胀为新配对；一路失败/挂起/从未发出也不会把该配对剔除。
- `explanation`：
  - `hung` / `retried`：两路分别定位挂起请求与发生过重试的请求；
  - `sessionComparison[]`：逐会话、逐请求对比双路首发时刻与等待差；
  - `delayedSessions` / `unaffectedSessions`：被拖延与未被拖延的会话；
  - `narrative[]`：中文文字解释。
- `runSinglePath(records, pathConfig, label)`：只跑一路，便于单测与单独观察。

### 模拟语义（验收口径）

1. **到达门控**：任何尝试的 `sentAt >= plannedArrival`，会话空闲也不提前捎带。
2. **依赖门控**：前序到达 `succeeded` 后后继才可发出；前序最终失败则后继 `never-sent`（`dependency`）。
3. **并发门控**：同会话在途数达到 `sessionConcurrency` 时排队（`concurrency-cap`），不同会话互不挤占。
4. **重试归属**：失败且可重试、未超 `maxAttempts` 时，退避 `backoffMs` 后在本路重发；
   退避期内状态为 `awaiting-retry`，不会被提前派发；尝试仍挂在原 `logicalId` 下。
5. **挂起**：`hang` 不安排完成事件，槽位占用到截止，终态为 `in-flight`。
6. **路间隔离**：两路各自维护虚拟时钟与状态，唯一共享输入是不可变的到达安排；
   配对阶段会防御性校验双路 `plannedArrival`/`round` 一致，不一致直接抛错。

## 复现步骤

```bash
node --version   # 应为 v24.15.0

# 运行全部测试（不安装任何依赖）
node --test 'tests/**/*.test.mjs'

# 运行题面场景示例
node examples/headline-scenario.mjs
```

也可用 `npm test` / `npm run example`（package.json 仅封装上述命令，无依赖安装）。

## 验证范围

32 个 node:test 用例，全部真实执行通过（`tests/` 下两个文件，无跳过用例）：

- **正常路径**：到达门控、会话链串行、同会话 cap=1/2 排队、跨会话互不挤占、成功收口。
- **边界**：截止时刻边界（`t=deadline` 仍处理、之后到达标记 `deadline-before-arrival`）、
  无截止自然结束、空记录集、零延迟、退避跨越截止（`retry-not-reached`）、
  不可重试失败立即终止、输入校验（重复 ID、跨会话前序、非法时间/策略）。
- **交错/故障恢复（题面验收项）**：
  - 基线挂起、候选快速结束：基线 A 链后续 `never-sent`，候选失败-退避-恢复后全成功；
  - 配对恒为 5 个逻辑请求，重试不另成负载，失败侧配对保留而非 inner-join 剔除；
  - 会话 A 等待只在基线增加（示例中最大等待 900ms 对 70ms），会话 B 双路首发逐条相同；
  - 候选零延迟不提前基线负载；重试只出现在失败那一路（`extraRetries` 不对称）；
  - 两路独立失败-退避-恢复时间线、一路彻底失败另一路恢复；
  - 第二轮到达在两路都按计划加入；共享安排被篡改时配对阶段报错；
  - 同输入两次运行结果深度一致（确定性），MinHeap 同键 FIFO。

`examples/headline-scenario.mjs` 仅用于人工查看输出结构，功能正确性以测试为准。

## 约束与边界

- 纯本地合成数据与虚拟时钟，不访问网络、不读取凭据、不安装依赖。
- 模拟器只描述调度/重试/挂起语义，不生成或执行任何攻击、漏洞利用或规避载荷。
