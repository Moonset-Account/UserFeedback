# 层级主题事件总线（本地基础设施组件）

纯本地、零依赖的事件总线实现，使用 Node.js 内置能力，无任何网络访问、外部目标或真实负载。

## 目录与入口

| 路径 | 作用 |
| --- | --- |
| `src/event-bus.js` | 组件唯一实现入口，导出 `EventBus` 类及纯函数 `matchLevels`、`validateFilter` |
| `tests/event-bus.test.mjs` | 基于 `node:test` + `node:assert/strict` 的验收测试（13 个用例，全部为真实断言） |
| `package.json` | `type: module`；`npm test` 等价于 `node --test tests/` |

运行测试（Node.js >= 24，本工作区实测 v24.15.0）：

```bash
node --test 'tests/**/*.test.mjs'
# 或
npm test
```

## 主题与匹配规则

- 主题与过滤器均以点号分层，例如 `order.created.eu`。
- `*` 只匹配一层：`a.*.c` 命中 `a.b.c`，不命中 `a.b` 或 `a.x.b.c`。
- `#` 只允许出现在过滤器末尾，匹配零层或多层：`a.#` 命中 `a`、`a.b`、`a.b.c`；
  单独的 `#` 匹配任意主题。过滤器中间出现 `#`（如 `a.#.b`）在订阅阶段直接抛错拒绝。
- 发布主题不得包含通配符，非法主题（空层级、含 `*`/`#`）直接抛错。

## 接口

```js
import { EventBus } from './src/event-bus.js';

const bus = new EventBus();

// 普通订阅者：不存在时按 capacity（默认 1）创建；可多次调用追加多条过滤器
bus.subscribe('subscriber-id', 'order.*.eu', /* capacity */ 1);

// 共享组：成员固定顺序 = 数组顺序；容量可整体默认或逐成员指定
bus.createSharedGroup('group-id', [
  { id: 'm1', capacity: 1 },
  { id: 'm2' }, // 使用 defaultCapacity
], /* defaultCapacity */ 1);
bus.subscribeSharedGroup('group-id', 'order.#');

// 发布：返回 { status: 'accepted' | 'deferred', messageId, queueLength, reason? }
bus.publish('order.created.eu', { any: 'payload' });

// 释放慢订阅者 / 共享组成员最旧的一封在途消息，自动触发暂缓队列重试
bus.release('subscriber-id');
bus.releaseGroupMember('group-id', 'member-id');

// 手动重试暂缓队列（release 内部已自动调用）
bus.retryPending();

// 只读诊断
bus.inboxOf('subscriber-id');
bus.memberInboxOf('group-id', 'member-id');
bus.cursorOf('group-id');          // -1 表示尚无成功记录
bus.memberOrderOf('group-id');
bus.pendingMessageIds();
```

## 投递与暂缓语义（题面交叉约束）

1. **普通订阅者规则合并**：一次发布即使多条过滤器同时命中，该订阅者收件箱也只入一份。
2. **共享组每份单投**：每个命中的共享组只把一份消息投给一个有空位的成员。
   成员按固定顺序，从**上次成功者之后一位**开始环形扫描（满员成员跳过），
   选中第一个有空位者，成功后轮转指针停在该成员；初始指针为 `-1`，首轮从首位开始。
3. **原子接受条件**：一次发布只有在
   （a）所有命中的普通订阅者都有空位，且
   （b）每个命中的共享组都能选出有空位成员
   时才被接受。否则整次进入 FIFO 暂缓队列：
   - 不向任何收件箱写入；
   - 任何共享组轮转指针都不前进；
   - 实现先完成全部可行性计算，再统一提交，杜绝部分投递。
4. **释放后按原轮次重试**：`release` / `releaseGroupMember` 腾出空位后自动按 FIFO
   重试暂缓队列。由于暂缓期间指针从未移动，共享组仍按其“原本应得的轮次”投递。
   队首仍受阻时立即停止，保留后续发布的原始顺序。
5. 无任何命中方的发布直接接受（空投递），不影响任何状态。

## 测试验证范围

`tests/event-bus.test.mjs` 共 4 组 13 个用例，覆盖题面要求的三类场景：

- **可接受结果**：精确/通配匹配、`#` 零层与多层、普通订阅者多规则合并为一份、
  共享组固定顺序轮转、普通订阅者与多个共享组同次发布各取所需。
- **应被阻止/拒绝的结果**：普通订阅者满员导致整次暂缓且共享组指针零前进；
  共享组全员满员时命中的普通订阅者同样收不到消息；非法过滤器与非法主题抛错拒绝。
- **边界与交错场景**：容量恰好占满/超出一封的边界；释放后共享组按原轮次投递；
  多个命中方同时满员时只释放一方仍无法解除暂缓（原子条件）；不均容量下跳过满员
  成员但保持“上次成功者之后轮转”；多封暂缓严格 FIFO 且轮次不被暂缓消耗；
  不命中发布不受满员影响。

所有断言均为真实执行的 `node:assert/strict` 断言，无模拟网络、无外部目标。
