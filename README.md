# 影子执行器（Shadow Executor）

用于补丁验证的可注入双实现影子执行器：候选实现读到与基线相同的业务返回值，
其试算写入只进入独立覆盖层，绝不写入基线存储。

## 入口

- `src/shadow.mjs` — 核心 `ShadowExecutor`、`UnknownTaskError`、`deepEqual`
- `src/storage.mjs` — 参考内存存储驱动 `MapStorage`（驱动只需实现异步
  `read(resource)` / `write(resource, value)`）
- 测试入口（真实断言，基于 `node:test`）：
  - `tests/acceptable.test.mjs` — 可接受结果
  - `tests/blocked.test.mjs` — 应被阻止/拒绝的结果
  - `tests/interleave.test.mjs` — 边界与交错场景
  - 运行：`npm test`
    （等价于 `node --test tests/acceptable.test.mjs tests/blocked.test.mjs tests/interleave.test.mjs`，
    Node.js v24.15.0 验证通过）

## 用法

```js
import { ShadowExecutor } from './src/shadow.mjs';
import { MapStorage } from './src/storage.mjs';

const storage = new MapStorage({ 'R-A': 10 });
const exec = new ShadowExecutor(storage);

// 1) 基线：真实读写，并按任务标记记录外部交互
await exec.runBaseline([
  { id: 'task-1', async impl(ctx) {
      const a = await ctx.read('R-A');
      await ctx.write('W-1', a + 1);
      return a;
  } },
]);

// 2) 候选：回放基线读取，写入只进每任务覆盖层；任务可换序或并发交错
const report = await exec.runCandidate(
  [
    { id: 'task-1', async impl(ctx) { /* 同形 IO 的候选逻辑 */ } },
  ],
  { concurrent: false }, // true 时 Promise.all 并发，允许任意交错
);
```

## 语义与交叉约束

1. **基线记录**：按任务标记（`id`）记录每次外部读取的资源、任务内调用次序
   （`order` 为任务内全局序号，`occurrence` 为同一资源在该任务内的第几次读取）
   和返回值；写入同样按序记录。基线写入真实落库。
2. **配对回放，不依赖全局次序**：候选读取按
   `(任务标记, 资源, 任务内该资源第 n 次)` 与基线观测配对。因此两个候选任务
   可以换序完成、甚至并发交错；同一任务的第二次外部读取拿到的始终是它自己的
   第二个基线值，而不是全局队列里的下一个值。
3. **候选写入隔离**：候选 `write` 只写入该任务私有的覆盖层（overlay），返回
   正常 ack；同任务后续读取先见覆盖层。覆盖层读取不消耗基线回放槽位，不影响
   后续读取的 `occurrence` 配对。基线存储驱动在候选阶段不会收到任何写调用。
4. **无法配对的外部读取**：不虚构值，而是通过只读 fallback 驱动（默认即基线
   驱动；候选路径从不调用其 `write`）取得观测值，保留差异事件
   （资源、序号、原因 `resource-never-read-by-baseline` /
   `read-occurrence-without-baseline`、观测值、fallback 错误），并把依赖该读取
   的任务结果标记为 `unsubstantiated: true`（缺少依据）。已配对的读取与差异
   同时保留在报告中。
5. **未知任务标记**：候选任务在基线轨迹中不存在时，注册阶段即抛
   `UnknownTaskError`，不会开启任何候选任务槽位。
6. **值边界克隆**：跨驱动/覆盖层的值经 `structuredClone` 拷贝，夹具侧的引用
   别名不会伪造相等性。

## 报告（`report()` / 各 run 的返回值）

- `baselineTaskOrder` / `candidateTaskOrder`：两侧任务实际次序
- `tasks[id].baseline`：基线读写轨迹与结果
- `tasks[id].candidate`：候选读（含 `source: replay | overlay | fallback`）、
  候选写（均为 `layer: overlay`）、覆盖层快照、结果/错误
- `tasks[id].replayChecks`：每条配对读取与基线值的逐值相等校验
- `tasks[id].missingBasis`：无法配对读取的保留差异（即“缺少依据”明细）
- `tasks[id].unsubstantiated`：该任务结果是否缺少依据
- `tasks[id].writeDiffs`：两侧写入差异（候选-only / 基线-only / 同资源值序列比较）
- `tasks[id].resultsEqual`：业务返回值（或拒绝态）是否一致
- `summary.allSubstantiated` / `summary.anyUnpairedRead`：汇总结论

## 验证范围

- **可接受结果**（5 例）：基线轨迹完整性；候选等业务值且写入仅入覆盖层；
  任务换序后各自观测不变；写后读命中自身覆盖层；跨边界克隆隔离。
- **应阻止/拒绝**（7 例）：未知资源读取与已知资源的超次读取均保留差异并标记
  缺少依据；fallback 驱动失败被记录并重抛；业务值发散如实报不等；未知任务
  标记前置拒绝；候选任务重复执行拒绝；非法驱动/任务形状 TypeError。
- **边界/交错**（3 例）：两任务并发、读取刻意交错（门控 Promise）下的按任务
  配对与独立覆盖层；覆盖层读夹在回放读之间不占用回放槽位；并发注册时未知
  任务仍被整体拒绝。

全部 15 个用例本地通过。所有夹具均为工作区内合成的内存驱动，不涉及任何真实
外部目标探测或网络访问。
