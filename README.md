# 列式表达式执行器（Columnar Expression Executor）

按批计算包含 `CASE`、布尔 `AND`/`OR`、整数比较和整除的表达式。执行器接收列式
数据、有效位（null 位图）与选中行号，**只返回被选中的行并维持原选择顺序**，
结果同时包含成功行与逐行错误。

## 运行环境与入口

- 运行时：Node.js 24.15.0（ESM，`"type": "module"`，无第三方依赖）
- 库入口：`src/index.js`
  - `compile(expression)` → `{ execute }`
  - `ExpressionError`（逐行错误类型，携带原始输入行号）
  - `truncateTowardZero`（向零取整工具函数）
- 测试入口（真实断言，基于 `node:test` + `node:assert/strict`）：

```bash
node --test 'tests/**/*.test.mjs'
# 或（package.json 中已配置同一命令）
npm test
```

## 表达式 AST 节点

| kind      | 形态 | 说明 |
|-----------|------|------|
| `column`  | `{ kind:'column', name }` | 读列；null 由列值或有效位决定 |
| `literal` | `{ kind:'literal', value }` | 整数 / 布尔 / `null` |
| `compare` | `{ kind:'compare', op, left, right }` | `op ∈ eq, neq, lt, lte, gt, gte` |
| `and`/`or`| `{ kind:'and'|'or', left, right }` | SQL 三值逻辑 + 短路 |
| `div`     | `{ kind:'div', left, right }` | 整除，向零取整 |
| `case`    | `{ kind:'case', branches:[{when,then}...], else? }` | 惰性分支 |

## 调用方式

```js
import { compile } from './src/index.js';

const executor = compile({
  kind: 'case',
  branches: [
    { when: { kind: 'compare', op: 'gt', left: { kind:'column', name:'a' }, right: { kind:'literal', value:0 } },
      then: { kind: 'div', left: { kind:'literal', value: 100 }, right: { kind:'column', name:'b' } } },
  ],
  else: { kind: 'literal', value: -1 },
});

const { outputs, errors, results } = executor.execute({
  columns: { a: [5, 0], b: [2, 0] },
  validity: {},                       // 可选：{ 列名: 位图(1/true=有效) }
  selectedRows: [1, 0],               // 保持该顺序，允许重复
});
```

返回结构：

- `outputs`：`[{ row, value }]`，仅成功行，按选择顺序排列
- `errors`：`[{ row, code, message, details }]`，逐行错误，按选择顺序排列
- `results`：`[{ status:'value', row, value } | { status:'error', row, error }]`，
  成功与错误交错的完整视图，按选择顺序排列

## 语义约定（对应题面约束）

1. **选择顺序**：仅处理 `selectedRows` 中的行；输出顺序与选择顺序完全一致，
   允许重复行号；行号即该批数据中的原始输入行号，错误也回传该行号。
2. **CASE**：仅当条件严格为 `true` 才进入对应分支；条件为 `false` 或 SQL `NULL`
   均落空继续；没有分支命中时走 `else`（“空条件走另一分支”），无 `else` 则结果为
   `null`。未命中分支的 `then` 表达式**不会被求值**。
3. **AND/OR 短路与三值逻辑**：
   - `AND` 左侧为 `false` 立即返回 `false`，不求值右侧；
   - `OR` 左侧为 `true` 立即返回 `true`，不求值右侧；
   - 未短路时按 SQL 三值逻辑：`NULL AND false → false`、`NULL AND true/NULL → NULL`；
     `NULL OR true → true`、`NULL OR false/NULL → NULL`。
4. **整数比较**：六个比较运算符；任一操作数为 `NULL` 结果为 `NULL`。
5. **整除**：向零取整（`-7/2 → -3`，并归一化负零 `-0 → 0`）；
   - 分子为 `NULL` → 结果 `NULL`，且**不求值分母**（不会在未走到的行上产生除零错误）；
   - 分母为 `NULL` → 结果 `NULL`；
   - 分子非空且分母为 `0` → 逐行 `DIVISION_BY_ZERO` 错误，错误体带原始输入行号与分子值，
     不中断整批执行。
6. **共享子表达式**：同一子表达式节点可被多个分支引用；只有真正走到的分支才会求值，
   未走到的行不会因其中的除零而报错（见 `tests/executor.test.mjs` 复用用例）。
7. **批次隔离**：编译产物可跨批次复用，但每次 `execute` 直接从当前批次的列缓冲读取，
   不缓存任何行值；新批次不会沿用上一批的结果。
8. **有效位**：`validity[列名][行]` 显式为 `0/false` 时该单元按 SQL `NULL` 处理；
   未提供位图时以列内 `null` 值为准。

错误码：`DIVISION_BY_ZERO`、`TYPE_ERROR`、`UNKNOWN_COLUMN`、`ROW_OUT_OF_RANGE`、
`INVALID_EXPRESSION`。表达式树结构错误在 `compile` 阶段即抛出（快速失败）。

## 验证范围（tests/，共 27 个用例）

- `tests/logic.test.mjs`：选择顺序与重复行；CASE 真/假/NULL 条件与缺省 ELSE；
  AND/OR 短路（用除零表达式证明右侧未求值，并设对照组证明非短路时会被拒绝）；
  三值逻辑真值表；六类比较的 NULL 语义与正常布尔结果。
- `tests/division.test.mjs`：正负号向零取整与负零归一；NULL 分子/分母；
  非空分子除零的逐行错误与原始行号（含重排选择）；有效位触发的 NULL 与除零边界。
- `tests/executor.test.mjs`：
  - **可接受结果**：共享除法子表达式在未进入分支的行上正常成功；批次复用时
    独立求值；成功/错误交错返回且顺序正确；嵌套 OR 的 NULL + 短路。
  - **应被阻止/拒绝的结果**：进入含除零分支的行返回 `DIVISION_BY_ZERO`；
  - **边界/交错场景**：同一表达式中短路行与除零行交错、CASE NULL 条件与
    短路 AND 交叉、越界选中行、空选择、编译期非法树、未知列、错误行号回传。

所有用例均为本地合成夹具，无网络、无外部目标。
