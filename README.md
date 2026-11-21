# coverage-gate —— 新版本覆盖准入模块

针对合并请求的覆盖准入（coverage gate）：输入候选文件及内容摘要、逐行差异、
编译器标出的可执行行与分支出口、必需测试分片的覆盖记录，输出 **放行 / 暂缓 /
缺测位置**。只考查新版本增加或替换出来的可执行行及其全部出口。

- 运行时：Node.js **24.15.0**，仅使用 Node.js 标准库
- 模块体系：ECMAScript Modules（`"type": "module"`）
- 测试：内置 `node:test` + `node:assert/strict`，**零第三方依赖、零安装**
- 全部数据均为本地合成输入，不访问网络、不读取凭据

## 背景与判定规则

1. **待测行范围**
   待测行 = 逐行差异中新版本侧的 `add` 行（新增或替换出来的行）
   ∩ 编译器标出的可执行行。
   - `context`（未改动行）、`delete`（只存在于旧版本的行）不考查；
   - 新增但不可执行的行（注释、空行等）不考查；
   - **纯重命名**（零个 hunk、零新增行）不产生任何待测行。
2. **分支出口维度**
   编译器为可执行行列出 `exits` 时按分支行处理，每个出口 id 都必须被命中；
   未列出 `exits` 的普通语句按单一隐式出口（id 为 `"line"`）处理。
3. **分片整份替换**
   每个必需分片只取“完成成功（`success: true`）的最高尝试号”那一份结果，
   更早尝试的命中**不参与合并、不跨尝试累积**。
4. **摘要匹配才合并**
   分片覆盖中的文件 `summary` 必须与候选文件摘要一致，否则该文件的整份命中
   忽略（`ignored` 中给出 `summary-mismatch` 原因）；候选清单之外的路径标注
   `unknown-path`。
5. **收齐后才定论**
   任一必需分片尚无成功结果（无记录，或最高尝试失败）→ `status: "pending"`，
   列出等待中的分片，暂不输出缺测定论。
6. **重跑撤分支场景**
   因为命中按“出口 id”集合统计且高尝试整份替换低尝试，重跑撤掉某分支的唯一
   命中后，即使该行仍被其他分片（或同分片的其他出口）执行过，该分支仍会出现在
   `missing` 中（`kind: "branch"`）。

## 公开 API

模块入口：`src/coverage-gate.js`

### `evaluateCoverageGate(input)`（主入口）

输入对象：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `files` | `Array<{ path: string, summary: string }>` | 候选文件名及内容摘要 |
| `changes` | `Array<{ file: {path, summary}, hunks: Hunk[] }>` | 按文件给出的零或多处逐行差异 |
| `executable` | `Array<{ file: {path, summary}, executableLines: ExecLine[] }>` | 编译器标出的可执行行及分支出口 |
| `requiredShardIds` | `string[]` | 必需测试分片 id |
| `shardRecords` | `ShardRecord[]` | 各分片各尝试的覆盖记录 |

- `Hunk`：`{ oldStart, oldCount, newStart, newCount, lines: Array<{ type: 'context'|'add'|'delete', text }> }`
  （`oldStart/oldCount/newStart/newCount` 即新旧起始行号与行数；游标推进规则：
  `context`/`add` 推进新版本行号，`delete` 不推进）
- `ExecLine`：`{ line: number, exits?: Array<{ id: string }> }`
- `ShardRecord`：`{ shardId, attempt: number(≥1), success: boolean, coverage: { files: FileCov[] } }`
- `FileCov`：`{ path, summary, lines: Array<{ line, exits?: string[], executed?: boolean }> }`
  显式 `exits` 按分支出口记录；未提供 `exits` 时 `executed !== false` 归一到隐式出口。

返回对象：

```ts
{
  status: 'pass' | 'pending' | 'missing',
  pendingShards: string[],          // 尚无成功结果的必需分片
  missing: Array<{                  // 缺测位置（pending 时为空数组）
    path: string,
    line: number,
    kind: 'line' | 'branch',
    missingExits: string[],         // 未命中的出口 id
    coveredExits: string[],         // 同行已命中的出口 id
  }>,
  targets: Array<{                  // 本次考查的全部待测目标
    path, summary, line, isBranch,
    exits: Array<{ id, kind: 'implicit'|'branch' }>,
  }>,
  ignored: Array<                   // 被忽略的输入及原因
    | { shardId, reason: 'not-required' }
    | { shardId, path, reason: 'summary-mismatch' | 'unknown-path' }>,
  mergedHits: Array<{ path, line, exits: string[] }>, // 合并后的真实命中
}
```

### 同时导出的纯函数（便于单测与复用）

- `collectAddedLines(hunk) → number[]`：从单个 hunk 解析新版本新增/替换行号；
- `buildTargets(changes, executableByPath) → Target[]`：求待测行并归一化出口；
- `selectShardResults(requiredShardIds, shardRecords) → { selected, pending, ignored }`：
  每分片取成功的最高尝试；
- `mergeCoverage(selected, filesByPath) → { hits, ignored }`：仅合并摘要匹配的命中；
- `findMissingTargets(targets, hits) → Missing[]`：逐出口找缺测；
- 常量 `IMPLICIT_EXIT_ID`（值为 `'line'`）。

输入结构非法时统一抛出 `TypeError`，错误信息以 `coverage-gate:` 开头。

## 复现步骤

```bash
node --version            # 期望 v24.15.0
node --test               # 自动发现 tests/*.test.mjs
# 或显式指定：
node --test "tests/**/*.test.mjs"
```

无需 `npm install`（本模块没有任何依赖）。

## 验证范围（tests/coverage-gate.test.mjs，共 21 个用例，全部真实执行）

- **正常场景**：双分片凑齐分支两出口 + 普通行 → `pass`；多文件多 hunk 交错覆盖。
- **边界 / 暂缓**：必需分片缺失 → `pending`；最高尝试失败时低尝试成功也不合并；
  非必需分片记录进入 `ignored`；成功但零覆盖的分片。
- **核心规则（题面重点）**：
  - 重跑撤掉某分支唯一命中 → 同行仍被执行但该分支列入 `missing`（两阶段断言）；
  - 高尝试整份替换低尝试，不跨尝试累积出口（记录顺序故意颠倒）；
  - 普通行未命中按隐式出口报 `line` 级缺测。
- **摘要与候选范围**：摘要不匹配整份忽略；候选外路径标注 `unknown-path`。
- **待测行范围**：注释行不进 targets；context 行无覆盖不算缺测；
  纯重命名（零 hunk）直接放行；无必需分片但有待测行 → `missing`。
- **输入校验**：候选外文件、摘要不一致、重复尝试号、非法 `attempt/success` 等抛 `TypeError`。
- **纯函数**：`collectAddedLines` 的游标推进、空 hunk 与非法结构。

> 说明：所有用例均由 `node:test` 真实运行，断言基于模块对合成输入的真实计算结果，
> 不存在预置的演示输出。未执行的测试：无。

## 项目结构

```
package.json
src/coverage-gate.js          # 准入模块（零依赖 ESM）
tests/coverage-gate.test.mjs  # node:test 测试（21 个用例）
README.md
```
