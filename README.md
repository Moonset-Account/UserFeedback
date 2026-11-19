# 可撤回学习的本地词典（Retractable Bigram Ledger）

面向受管终端的本地候选词组账本。输入是**带来源段编号的有序词元**，以及由调用方
提供的**会话标记**和**隐私标记**。账本按相邻词元累计二元组候选，并支持事后把某个
来源段标记为私密，从而**精确撤回**该段带来的全部历史贡献。

无运行时依赖，仅使用 Node.js 内置模块。

## 入口

| 项 | 路径 |
| --- | --- |
| 实现入口 | `src/bigram-ledger.js` |
| 包入口（`exports`） | `"."` → `./src/bigram-ledger.js` |
| 测试入口 | `tests/*.test.mjs`（`node --test`） |
| 运行命令 | `node --test`（或 `npm test`） |
| 运行时 | Node.js v24.15.0（`>=24`） |

`package.json` 声明 `"type": "module"`，因此源码以 ES 模块解析，不依赖上层目录的
包作用域。

## 数据模型

- **来源段（segment）**：`{ id, session, private?, tokens? }`。段是来源编号的载体，
  自身携带会话与隐私标记。
- **词元（token）**：`{ text, segment }`。有序追加，段落边界本身**不**打断相邻性。
- **贡献（contribution）**：原始流中一对**相邻**词元。只有当两端所在段都公开、
  且属于**同一会话**时，才向 `(prev, next)` 候选加 1。
- **隐私段是屏障而非过滤器**：私密段的词元不参与计数，且其两侧词元**不会**因此
  变成相邻。因此撤回中间段后，左右两侧不会新拼成一组。
- **跨会话断开**：相邻两端会话不同则整对不计数；撤回后也不会因“中间空出来”
  而改变这一判定。

## API

```js
import { BigramLedger, replay, buildFromScratch } from './src/bigram-ledger.js';

const ledger = new BigramLedger().ingest([
  { id: 'seg-1', session: 'call-a', tokens: ['alpha', 'beta'] },
  { id: 'seg-2', session: 'call-a', tokens: ['gamma', 'delta'] },
]);

ledger.query();               // [{ bigram: ['alpha','beta'], count: 1 }, ...]
ledger.countFor('beta', 'gamma'); // 1：跨段但同会话且都公开

ledger.markPrivate('seg-2');  // 撤回 seg-2 的全部贡献
ledger.query();               // 只剩 [['alpha','beta']]
ledger.countFor('beta', 'gamma'); // 0
```

细粒度接口：

- `defineSegment({ id, session, private? })`：只登记元数据，不追加词元。
- `append([{ text, segment }, ...])`：按序追加词元；引用未登记段会被拒绝，且整次
  追加是原子的（拒绝时不留部分状态）。
- `addSegment({ id, session, private?, tokens? })`：登记段并连续追加其词元。
- `ingest([...])` / `ingestTranscript({ segments, tokens })`：批量便利方法。
- `markPrivate(id)`：撤回该段所有活跃贡献；重复调用幂等。
- `query()`：返回计数为正的候选，按 `(prev, next)` 稳定排序。
- `countFor(prev, next)` / `isPrivate(id)`：单点查询。
- `events()`：导出事件日志。

重放与一致性：

- `replay(events)`：按记录顺序重放 `define-segment` / `append-tokens` /
  `mark-private` 事件，重建等价账本。
- `buildFromScratch(transcript, { privateSegmentIds })`：按**最终隐私标记**直接从零
  构建的参考实现，不含任何撤回步骤。

## 交叉约束与验证范围

题面的核心交叉约束：**重放撤回后的候选与计数，必须与按最终标记从头重建一致**。
测试用三条相互独立的路径对照：

1. `live`：真实追加 + 交错撤回；
2. `replay(live.events())`：事件日志重放；
3. `buildFromScratch(...)`：最终标记直接构建。

另加一个**不共享实现代码的一遍扫描 oracle**（`tests/property.test.mjs`），对 250 个
确定性随机种子逐一比对完整计数表。

测试文件与覆盖：

| 文件 | 覆盖 |
| --- | --- |
| `tests/adjacency.test.mjs` | 同会话相邻计数、重复词对累加、跨段不断开、**跨会话被阻断**、出生即私密的段作屏障、查询排序、非法输入被拒（未登记段 / 重复 id / 非布尔隐私标记 / 空会话） |
| `tests/retraction.test.mjs` | 撤回中间段与端段、**其他公开来源对相同词组的贡献保留**、撤回幂等、撤回前/后追加的对均被阻断、撤回全部段后无候选、重放与从头构建一致、未知事件类型被拒 |
| `tests/interleaved.test.mjs` | 跨段词组随任一端被撤回、**撤回中间段后左右不新拼成一组**、同会话内的会话断点、跨会话共享词组在一侧撤回后保留、追加/撤回交错后三方一致 |
| `tests/property.test.mjs` | 250 个种子的 `replay`≡`live`≡`scratch`，以及与独立 oracle 的逐计数比对 |

每类场景都同时含**可接受结果**（应计数的词组、应保留的其他来源贡献）与**应被拒绝 /
阻止的结果**（跨会话对、私密段对、撤回后不得新生成的邻接、非法输入）。

## 本地运行

```sh
node --test          # 运行 tests/*.test.mjs
```

测试仅使用本地合成夹具，不联网、不读取外部配置、不访问任何真实目标。
