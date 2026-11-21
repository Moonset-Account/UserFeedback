# 开环压测器（可挂接本地模拟后端）

一个纯本地、无网络依赖的开环（open-loop）压测组件。调用方预定每条请求的到达时刻、
并发槽数量和有界等待队列长度；模拟后端的完成时刻通过时钟抽象完全可控。

## 入口与运行环境

- 运行时：Node.js 24.15.0（`package.json` 声明 `engines.node >= 24`），ESM，无第三方依赖
- 库入口：`src/index.js`
  - `LoadGenerator`（`src/load-generator.js`）：开环生成器核心
  - `MockBackend`（`src/mock-backend.js`）：可挂接的本地模拟后端，服务时长与“拖慢前 N 个请求”可配
  - `VirtualClock` / `RealClock`（`src/clock.js`）：虚拟时钟（确定性，主路径）与真实定时器时钟（冒烟用）
- 示例：`node examples/demo.js`
- 测试：`node --test tests/load-generator.test.mjs`（`npm test` 已配置同样的命令；本环境 `npm` 执行需额外授权，故验收使用前者）

## 核心语义（对应题面交叉约束）

1. **开环到达**：每条到达事件在预定 `scheduledAt` 时刻无条件触发，槽满也照常生成请求。
   槽与队列都放不下时记一条丢弃记录（`reason: 'queue-full'`），**不重试、不延后**，到达曲线本身不变。
2. **同刻顺序**：同一时刻先处理完成/释放事件（优先级 0），再处理新到达（优先级 1）。
   释放槽时先从 FIFO 等待队列取走最早一项，之后同刻新到达才参与判定。时钟对同刻事件按
   `(time, priority, insertion sequence)` 排序，虚拟时钟与真实时钟实现一致。
3. **三项可独立核对的耗时**：每个完成项携带
   `scheduledAt`（原计划时刻）、`arrivedAt`（实际到达）、`dispatchedAt`（实际派发）、
   `completedAt`（完成时刻）、`queueMs = dispatchedAt - scheduledAt`、
   `serviceMs = completedAt - dispatchedAt`。丢弃项另记 `inFlight / queued / queueCapacity` 现场。
4. **拖慢前两个再恢复**：`MockBackend` 的 `slowFirst: 2, slowDuration` 只影响前两个**已派发**请求的
   服务时长，之后自动回到正常 `serviceTime`（常量或按请求计算的函数均可）。后半段到达编号与时间不变，
   变化只体现在排队/服务时间与丢弃数上。

## 最小用法

```js
import { VirtualClock, MockBackend, LoadGenerator } from './src/index.js';

const clock = new VirtualClock();
let generator;
const backend = new MockBackend({
  serviceTime: 6,
  slowFirst: 2,
  slowDuration: 20,
  clock,
  onComplete: (record) => generator.completionHandler(record),
});
generator = new LoadGenerator({
  clock,
  backend,
  concurrency: 2,
  queueCapacity: 2,
  arrivals: [0, 1, 2, 3, 4].map((t) => ({ time: t, id: `req-${t}` })),
});
generator.start();
clock.run(); // 虚拟时钟一次性推进到底
// generator.completed / generator.dropped 即验收数据
```

真实定时器模式把 `VirtualClock` 换成 `RealClock`，到达时刻用 `clock.now()` 锚定，
结束用 `await clock.idle()`；事件顺序语义相同，但墙钟时刻存在真实调度漂移。

## 测试验证范围

测试文件：`tests/load-generator.test.mjs`，使用 `node:test` + `node:assert/strict`，全部为真实断言。

| 用例 | 类型 | 覆盖的交叉约束 |
| --- | --- | --- |
| 前两个请求拖慢后恢复 | 可接受结果 | 前两个 `serviceMs=20`、其余 6；后半段编号/计划时刻不变；排队耗时逐项核对；1 条丢弃且丢弃时刻不变 |
| 槽满 + 零容量队列 | 应被阻止/拒绝 | 2 条丢弃保留各自计划时刻，不会因槽在 t=10 释放而被延后受理；完成记录字段精确匹配 |
| 同刻释放与到达交错 | 边界/交错 | t=10 完成先放队列头 B，再判 C（入队）、D（队列满丢弃）；若顺序相反丢弃的会是 C，用断言钉死该语义 |
| 零队列下同刻释放即受理 | 边界 | 队列容量 0 时，到达与释放同刻仍能拿到刚释放的槽，`queueMs=0` |
| 相同时刻多条到达 | 边界 | 同刻到达按声明顺序依次占用槽/队列，超出者丢弃 |
| 函数式服务时长与 payload 透传 | 可接受结果 | 自定义时长策略、请求载荷原样回传 |
| 输入校验 | 拒绝 | 非法并发/队列容量、时间倒流、重复 id、负数时间、拖慢配置缺失均在事件触发前抛错 |
| 真实时钟冒烟 | 端到端 | 真实定时器下丢弃计数、完成顺序、事件排空成立（墙钟时刻按容差断言） |

## 范围与限制

- 仅本地基础设施组件：无外部目标、无网络连接、无真实负载；模拟后端只做时钟调度，不发起任何 I/O。
- 虚拟时钟按到达数组线性排序事件，面向确定性单元测试规模；真实时钟基于 `setTimeout`，仅用于冒烟。
- 到达时间要求有限、非负且非递减（相同时刻允许，按声明顺序处理）；id 可省略（默认用数组下标），显式 id 必须唯一。
