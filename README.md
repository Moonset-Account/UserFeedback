# 会话输出组件（chain-session-output）

面向「推理端沿提议链连续送审草稿 Token、验证端逐段确认」场景的本地基础设施组件。
纯本地状态机，无网络、无外部目标、无真实负载；Token 一律建模为**整数**，停止符默认为 `0`
（可在构造函数通过 `stopToken` 配置）。

## 入口

- 组件实现：`src/session-output.js`
  - 命名导出 `SessionOutput`（默认导出同名）与 `SegmentStatus`
- 验收测试：`tests/session-output.test.mjs`（基于 `node:test` + `node:assert/strict`，全部为真实断言）
- 运行环境：Node.js 24.15.0（`engines` 标注 `>=20`）

## 如何运行测试

```bash
node --test
```

说明：Node 24 下 `node --test tests/` 会把目录参数当作入口模块而报 `MODULE_NOT_FOUND`，
因此本仓库使用不带路径参数的自动发现（自动匹配 `tests/*.test.mjs`）。
`package.json` 的 `scripts.test` 同样配置为 `node --test`（本次环境未授权执行 `npm`，
故实际核验使用的是 `node --test`，结果 18/18 通过）。

## 需求到实现的映射

| 题面约束 | 实现位置 / 行为 |
| --- | --- |
| 每段从上一段草稿末尾开始 | `submitSegment` 中子段 `start = parent.start + parent.tokens.length`，根段 `start = head` |
| 子回复可以先到，但父段全数原样确认后才能提交 | `verifySegment` 对父段仍为 `pending` 的回复做缓冲（`bufferedResult`）；`#pump` 只沿已确认的链头推进，未确认前确定输出不增长 |
| 验证端给出接受前缀长度，未全收可给一个替换 Token | `verifySegment({ id, acceptedPrefixLength, replacementToken })`；全收时携带替换项会被拒绝 |
| 父段截短/纠正：发出确认前缀与替换项，作废所有旧后继 | `#applyResult` 发出前缀（与替换项），`#invalidateDescendants` BFS 作废整棵子树并清空其缓冲回复，发 `invalidated` 事件 |
| 允许从纠正后的已提交位置另起一条链 | 纠正后 `activeRootId = null`，新根段从当前 `head` 开始 |
| 确认序列遇停止 Token 终止，停止符及后面内容不发 | 接受前缀命中停止符：只发其之前的内容；替换项等于停止符：替换项不发；两种情况都置 `terminated` 并拒绝新草稿 |
| 重连按已收 Token 数只补余下确定内容 | `replay(receivedCount)` 返回 `committed.slice(receivedCount)`；越界计数抛错；草稿/作废内容永远不会出现在补传中 |
| 整数 Token 重放「子回复先到、再遇父级纠正」 | 见测试 `核心交错` 分组，含三级链缓冲、纠正作废、新链重放与重连补传的完整断言 |

## API 概览

```js
import { SessionOutput, SegmentStatus } from './src/session-output.js';

const session = new SessionOutput({ stopToken: 0 });

// 提交草稿段（parentId 省略/null 表示根段）
session.submitSegment({ id: 'p', tokens: [10, 11, 12] });
session.submitSegment({ id: 'c', parentId: 'p', tokens: [13, 14] });

// 验证回复（可乱序到达；父未定时自动缓冲）
session.verifySegment({ id: 'c', acceptedPrefixLength: 2 });        // 子回复先到
session.verifySegment({ id: 'p', acceptedPrefixLength: 2, replacementToken: 99 });

session.committedTokens; // [10, 11, 99]，c 已作废
session.head;            // 3（已提交位置）
session.terminated;      // false

// 重连补传
session.replay(2);       // { tokens: [99], from: 2, to: 3, terminated: false, caughtUp: false }
```

段状态（`SegmentStatus`）：`pending` / `confirmed` / `corrected` / `stopped` / `invalidated`。

事件（继承 `EventEmitter`，同时保留在 `session.eventLog`）：

- `tokens`：Token 进入确定输出，`kind` 为 `segment`（确认前缀/整段）或 `replacement`（替换项）
- `committed`：整段全数原样确认
- `corrected`：段被截短/纠正，携带 `acceptedPrefix` 与 `replacementToken`
- `invalidated`：祖先纠正导致的子树作废，携带作废段 id 列表
- `stopped`：遇到停止 Token，会话终止

## 验证范围（tests/session-output.test.mjs，18 个用例）

1. **可接受结果**
   - 根段原样确认、链落定后释放提交头并续新链
   - 子回复先到被缓冲，父段确认后按链序泵出
   - 重连补传：零偏移、部分偏移、已追平三种情况
2. **应被阻止/拒绝的结果**
   - 活动链未落定时另起根段；向已作废/已纠正父段追加子段；同一父段两条后继
   - 重复 id、空/非整数 Token；前缀长度越界、全收却带替换项、重复验证、未知段 id
   - 重连计数越界；终止后再提交；作废段的缓冲回复被丢弃且不可再验证
3. **边界与交错场景（影响上述两类）**
   - 核心重放：三级链「孙/子回复先到 → 父级纠正」，断言前缀+替换项、整子树作废、
     事件顺序、从纠正位置另起新链、以及重连只补确定内容
   - 纯截短无替换项；多级链完全乱序回复；纠正发生在链中段（祖先保留、子树作废）
   - 停止符四种边界：整段含停止符、停止符在接受前缀中、替换项即停止符、
     停止符落在被拒绝后缀中（不影响已接受内容）
   - 事件日志与确定输出的一致性及 `replacement` 来源标记

## 范围声明

仅包含本地状态机组件与合成整数 Token 的单元测试；不包含任何网络通信、真实目标探测、
攻击载荷或探针逻辑。
