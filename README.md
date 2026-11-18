# 扩展消息处理器 origin/source 来源检查分析库

面向审阅者的 Node.js 静态分析库：输入一个**简化的处理器控制流图（CFG）**，对每个敏感调用判断——在所有可达路径上，最终用于调用的**同一条消息**是否同时满足该调用登记的 **origin** 与 **source** 检查条件。判定失败时给出一条具体失败路径及受影响参数；无法解析的调用结果保留为未知。

纯 ES Module，无第三方依赖，Node ≥ 18。所有样本均为本地合成数据。

## 输入：简化处理器 CFG（JSON）

```json
{
  "messages": ["m"],
  "entry": "r0",
  "nodes": {
    "r0": { "kind": "read", "target": "o", "object": "m", "field": "origin", "next": "cO" },
    "cO": { "kind": "checkOrigin", "value": "o", "expected": "https://app.example", "pass": "r1", "fail": "end" }
  }
}
```

- `messages`：本处理器可能收到的消息变量名。
- `entry` / `nodes`：入口与节点表。节点类型：

| kind | 字段 | 语义 |
| --- | --- | --- |
| `read` | `target, object, field, next` | 读取消息或对象变量的字段 |
| `const` | `target, value, next` | 常量赋值（来源记为 `const`） |
| `assign` | `target, from, next` | 变量拷贝 |
| `merge` | `target, base, overrides:{field:var}, next` | 合并对象：被覆盖字段的来源**重新追踪**为覆盖值的来源，未覆盖字段回落到 `base`（可为消息名） |
| `checkOrigin` | `value, expected, pass, fail` | 比较完整 origin；仅当 `value` 的来源确为某消息的 `origin` 字段时，`pass` 边为该消息登记 origin 事实 |
| `checkSource` | `value, expected, pass, fail` | 比较窗口 source（`expected` 为窗口标识符），登记规则同上 |
| `branch` | `cond, true, false` | 普通分支，不产生事实 |
| `call` | `api, args:{name:var}, require:{origin,source}, next` | 敏感调用分析点，`require` 为登记的 origin/source 条件 |
| `unknownCall` | `api, target, next` | 无法解析的调用，结果来源记为 `unknown` |
| `nop` | `next` | 空节点（`next: null` 表示出口） |

## API

```js
import { analyze } from './src/index.mjs';

const result = analyze(cfg, { iterationCap, witnessStateCap });
// result.stats: { converged, iterations, nodeCount }
// result.calls: 按节点 id 排序，每个 call 节点一条：
// {
//   nodeId, api,
//   verdict: 'protected' | 'violated' | 'unknown',
//   require: { origin, source },
//   usedMessages: [...],        // 调用参数实际追溯到的消息
//   missing: [{ message, condition: 'origin'|'source', expected }],
//   affectedParams: [...],      // 追溯到缺失检查消息的参数
//   unknownParams: [...],       // 含 unknown 来源的参数
//   paramSources: { arg: ['msg:m.data', ...] },   // 逐参数来源（可复核证据）
//   witness: { path: [{node, edgeIn}...], missing, affectedParams, paramSources } | null
// }
```

判定规则（对单个调用、综合所有可达路径）：

- `violated`：某条被调用使用的消息缺少登记的 origin 或 source 事实（must 分析：汇合处取交集，单分支检查不成立）。附 `witness` 失败路径。
- `unknown`：无缺失事实，但参数含无法解析的调用结果（`unknown`），无法证实。
- `protected`：上述两者都不成立——所有被使用的消息在所有路径上都同时满足登记的 origin 与 source 条件。

分析设计：变量来源是有限集合上的 may 分析（并集汇合），已确立检查是有限事实集上的 must 分析（交集汇合）；消息、字段、检查期望值均来自 CFG 本身，格有限，循环上的不动点迭代必然收敛（`stats.converged`）。witness 在 (节点, 抽象状态) 乘积图上 BFS，状态空间有限故必然终止，并带 `witnessStateCap` 保险上限。

## 命令

```bash
# 对任意 CFG 样本运行分析
node tools/analyze.mjs samples/c01-protected.json

# 本地核验
node tools/local_verify.mjs --list        # 列出用例
node tools/local_verify.mjs --case C01    # 运行单个用例（C01..C04）
node tools/local_verify.mjs               # 运行全部用例
```

核验器对每个用例输出一行 JSON：`case`、`assertions`（实际断言数）、`passed/failed/skipped`、`sample`（样本/种子标识）与逐条 `expected/observed` 摘要。任一断言失败、断言数为零或整组跳过时进程以非零码退出；未知用例或非法参数退出码为 2。

## 可重放样本

| 文件 | 场景 | 期望结论 |
| --- | --- | --- |
| `samples/c01-protected.json` | 同一消息完整 origin+source 检查后调用 | `protected` |
| `samples/c02-wrong-message.json` | 检查的是 m2，调用用的是 m1 | `violated`（witness 经过 m2 的检查节点） |
| `samples/c03-loop-checked.json` | 检查在循环前，循环体内 merge/read 携带来源 | 收敛且 `protected` |
| `samples/c04-unknown-result.json` | 4 个连续敏感调用：未知结果 / 已检查 / 未检查 m2 / 混合路径 | `unknown` / `protected` / `violated` / `unknown` |

C01–C04 核验用例直接加载上述样本文件并补充内联合成变体（只满足其一的检查、合并覆盖、分支合流、循环体内检查等）。

## 未覆盖边界（审阅时需注意）

- **过程内分析**：不建模函数调用图、回调与事件注册时序；`unknownCall` 之外不解析真实 API 返回值。
- **检查语义简化**：`checkOrigin`/`checkSource` 假定比较本身书写正确（完整比较、非前缀匹配）；库只追踪"被比较的值来自哪条消息的哪个字段"，不验证比较表达式的构造。
- **对象汇合精度**：两条路径上 `merge` 出 `base` 不同的对象时，未覆盖字段的来源回落会退化为 `unknown`（保守，结论偏向 `unknown` 而非误授信）。
- **may/must 组合**：某消息只在部分路径上被使用时，可能出现保守误报（`violated`），witness 路径可用于人工复核。
- **source 建模**：窗口 source 以标识符 token 比较建模，不模拟真实 `Window` 对象同一性。
- **witness 上限**：病态大状态空间下 witness 搜索可能被 `witnessStateCap` 截断（此时 `witness.error` 标明），但 violated 判定本身不依赖 witness。
- 核验包装器只是运行入口，不构成独立合格证明；审计仍应阅读 `src/analyzer.mjs` 的实现与真实日志，对不能证实的内容保留未知。
