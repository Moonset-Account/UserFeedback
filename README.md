# 接管协调模块（端点防护代理升级）

端点防护代理升级时，把多个观测源逐个从旧版交接给新版，保证任一时刻每个
序号都有且仅有一个负责处置方。

## 实际入口

- 模块入口：`src/takeover-coordinator.mjs`
  - 导出 `TakeoverCoordinator`、`TakeoverError`、`OWNER`
- 测试入口：`tests/takeover-coordinator.test.mjs`（`node:test`，真实断言）
- 运行测试：`node --test`（或 `npm test`），运行时 Node.js 24.15.0

## 使用概要

```js
import { TakeoverCoordinator } from './src/takeover-coordinator.mjs';

const c = new TakeoverCoordinator();
c.registerSource('sensor-1', { firstSeq: 1 }); // 源的连续序号起点
c.ingest('sensor-1', 1, payload);              // 事件接入，按归属提交
c.reportOldWatermark('sensor-1', 1);           // 旧版消费水位
c.reportOldBackfillLimit('sensor-1', 1);       // 旧版实际能补齐的边界
c.confirmNewReady('sensor-1');                 // 新版准备确认 → 影子接收
c.commitTakeover('sensor-1', 2);               // 可控升级事件：提交接管
c.submitDisposition('sensor-1', 2, { by: 'new', decision: 'blocked' });
c.reportNewDisconnect('sensor-1');             // 断开回退
c.verifyCoverage('sensor-1');                  // 覆盖性核验
```

## 语义与交叉约束

1. **单一负责方**：每个序号在接入时提交（commit）给唯一归属方；影子期
   新版可提前接收相同事件（`previewedBy`），但不提交处置。
2. **接管边界校验**：`commitTakeover` 要求新版已就绪，且边界 ≤ 旧版消费
   水位 + 1，否则以 `NEW_NOT_READY` / `BOUNDARY_GAP` 拒绝，归属不变。
3. **新版提交范围**：新版仅能对归属自己的序号（≥ 接管边界）提交处置，
   越权以 `NOT_OWNER` 拒绝。
4. **断开回退**：`reportNewDisconnect` 回退到旧版实际能补齐的边界
   （`oldBackfillLimit + 1`）。新版已提交的处置决定保持不变；归属新版但
   未处置且旧版能补齐的序号重新划归旧版；谁也补不齐的序号作为观测缺口
   单独拦截（记入 `gaps`，不再归属任何一方）。
5. **迟到旧回执**：旧版对新版已提交序号的回执只记入 `history`（补历史），
   不改变已提交的处置决定；对仍归旧版且未处置的序号正常补录。
6. **缺口拦截**：接入序号跳变时，缺失区间同样作为观测缺口单独记录。
7. **覆盖性核验**：`verifyCoverage` 断言 `[firstSeq, lastSeq]` 内每个序号
   要么有归属提交，要么已被缺口拦截。

## 验证范围（tests/）

- **可接受结果**：完整生命周期（旧版负责 → 影子预览 → 合法接管 → 新版
  提交处置），迟到旧回执只补历史，覆盖性核验通过。
- **应被阻止/拒绝**：未就绪接管、越水位边界、新版越权提交、重复/回退序号。
- **边界/交错**：新版断开后的回退边界、旧版补齐重划、不可补齐序号的缺口
  拦截、已提交决定不变、回退后新事件归属旧版。

## 限制

纯内存、单进程协调模型；不包含持久化、网络传输与真实代理对接。
