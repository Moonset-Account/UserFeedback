# 告警通知协调器（Alert Notification Coordinator）

服务告警有 **开始（start）** 和 **恢复（recover）** 两种事件。一个无环依赖图
描述了哪些上游根因节点会抑制哪些下游症状节点。本模块负责把原始事件流转换为
去重、去噪、因果有序的通知流。

纯本地、无网络、无外部依赖。所有时间戳都由调用方给出（`at`，毫秒），协调器
不读取系统时钟，因此回放是确定性的。

## 实际入口

| 用途 | 入口 |
| --- | --- |
| 库入口 | `src/coordinator.mjs`（`package.json` 的 `exports`/`main`） |
| 验收测试 | `tests/coordinator.test.mjs`（`node:test`，`npm test`） |
| 可运行示例 | `examples/replay.mjs`（`npm run demo`） |

```bash
node --test tests/*.test.mjs   # 或 npm test
node examples/replay.mjs       # 或 npm run demo
```

运行时：Node.js 24.15.0（`engines` 声明 `>=22`）。仅使用标准库。

## API

```js
import { createNotificationCoordinator } from './src/coordinator.mjs';

const coordinator = createNotificationCoordinator({
  suppressions: [
    { cause: 'db-root', symptom: 'api' },
    { cause: 'network', symptom: 'api' },
    { cause: 'api', symptom: 'web' },   // 传递抑制：db-root / network 也抑制 web
  ],
  aggregationMs: 10_000,
  onNotification: (n) => deliver(n),    // 可选，按发出顺序同步回调
});

const notifications = coordinator.replay(events);
```

事件：`{ kind: 'start' | 'recover', faultId, nodeId, at }`

通知：`{ seq, kind, faultId, faultSeq, nodeId, at, trigger, ... }`

- `start` 通知：`trigger` ∈ `root-cause` | `aggregation-elapsed` | `suppression-cleared`
- `recover` 通知：`trigger: 'recovery'`，`pairedWithSeq` 指向配对的 start；若恢复
  曾被上游扣留，带 `heldWhileSuppressed: true`
- `faultSeq`：每个故障在首次 start 时分配的稳定编号，重送不会改变

## 语义规则

1. **聚合期**：症状（存在传递祖先的节点）首次出现后先等 `aggregationMs`；
   根因节点（无祖先）立即通知。
2. **抑制持有**：只要还有**活动的**抑制祖先（含传递祖先），症状即使等够也继续
   保留，不发出通知。
3. **释放**：最后一个活动祖先恢复时，已等够且仍活动的症状立刻以
   `suppression-cleared` 发出。
4. **静默恢复**：症状在聚合期内自行恢复 → 永久静默，之后祖先恢复也不会补发。
5. **覆盖后再露出**：已经通知过的症状后来被上游覆盖（自身恢复被扣留），再次
   露出时**沿用同一次事件**；最终只发一次配对恢复（`pairedWithSeq` 指向原
   start）。
6. **重送幂等**：同一 `faultId` 的重复 start / 重复 recover 不增加任何通知，也
   不重置聚合截止时间；恢复后重送 start 同样是 no-op。
7. **同刻顺序**：同一时间戳内先按输入顺序处理所有事件，再判断该时刻到期的聚合
   窗口。因此「祖先恰在截止时刻恢复」会在同一瞬间释放症状；「症状恰在截止时刻
   恢复」则保持静默。
8. **稳定次序**：不同时间戳按 `at` 升序；同一时间戳保持输入顺序；释放扫描按
   节点深度（上游先）再按 `faultSeq` 排序，接收方看到确定的次序。

## 配置校验

- 抑制图含环（含自环）→ 构造时抛错，消息包含 `cycle`。
- `aggregationMs` 必须为有限非负数字；`suppressions` 必须是数组。
- 事件缺少 / 非法 `kind`、`faultId`、`nodeId`、`at` → 抛 `TypeError`。
- 同一 `faultId` 绑定到不同 `nodeId`，或 recover 的节点与 start 不一致 → 抛错。

## 验证范围

`tests/coordinator.test.mjs` 共 **26** 个 `node:test` 用例，全部执行真实断言，
覆盖：

- **可接受结果**：根因立即通知并配对恢复；无覆盖症状在窗口结束时通知；一个祖先
  恢复不够、最后一个祖先恢复立即释放。
- **应被阻止 / 拒绝**：窗口内自行恢复保持静默；有环图被拒绝；非法配置与事件被
  拒绝；重复事件不产生通知；未知故障的恢复被忽略。
- **边界 / 交错**：恰好截止时刻处理事件再判到期（双向）；同刻多个祖先恢复按
  上游优先释放；传递抑制；窗口未满时祖先先恢复仍在窗口末发出；覆盖后再露出只
  发一次配对恢复；每故障稳定编号；`onNotification` 回调与返回顺序一致。

运行结果（本地合成夹具）：26 passed / 0 failed。

## 说明

本仓库只做本地基础设施组件：全部输入为合成的测试夹具，不连接网络、不包含任何
目标探测、攻击或漏洞利用代码。
