# 批量化相关标量子查询求值模块

按批求值外层行上带 `IF` 和**相关标量子查询**的评价树。核心目标：在保持外层行原序、原重复行的前提下，把同一读视图内参数完全相同的子查询合并成一次发起，并且只在被选中的 `IF` 分支上求值、只在被选中的分支上发起查询。

- 运行环境：Node.js **24.15.0**，仅用 Node 标准库与原生 ECMAScript modules。
- **零依赖**：不安装任何包，`package.json` 中没有 `dependencies`。
- 全部数据均为本地合成数据（`src/fixtures.js`），不访问网络、账户、凭据或真实系统。

## 复现步骤

```sh
node --version                 # 应为 v24.15.0
node --test tests/*.test.mjs   # 全量测试
node scripts/replay.mjs        # 题目要求的回放，退出码 0 表示全部检查通过
```

（`package.json` 中同样提供 `npm test` / `npm run replay`，但上述 `node` 命令不需要任何包管理器参与。）

不需要任何准备工作：没有 `npm install`，没有环境变量，没有外部服务。

## 公开 API

模块入口：`src/index.js`。

### `evaluateBatch({ tree, rows, runner, missingCorrelation?, readView? }) → Promise<BatchResult>`

在**一个新建读视图**上求值一批外层行。

- `tree`：评价树（见下文“表达式节点”），所有外层行共用。
- `rows`：外层行数组，保序。每项形如 `{ params: Record<string, unknown>, id?: string }`。`params` 是该行的参数环境。相同的行可以重复出现，重复行必须各自产出一个结果。
- `runner`：查询执行器，必须提供 `resolve({ readView, requests }) → Promise<responses>`。
- `missingCorrelation`：`'mark-missing'`（默认）或 `'error'`，见“相关参数的缺失”。
- `readView`：可选，指定读视图 id，仅用于日志/测试。

返回：

```js
// 成功
{ ok: true, readView, values: unknown[], stats }
// 失败
{ ok: false, readView, error: { code, reason, rowIndex, path, detail }, stats }
```

**失败时没有 `values` 字段**：整批不返回任何部分结果。

### `new ReadView(runner, { id? })`

跨多个批次复用同一读视图缓存时的入口。`view.evaluateBatch({ tree, rows, missingCorrelation? })` 语义同上，但缓存 `(sql, args)` 键在**该读视图的整个生命周期内**有效，因此后续批次可直接复用先前批次已解析过的请求，不再发查询。新建 `ReadView` 即清空缓存（对应“新的读视图，数据可能已变化”）。

### `subqueryKey(sql, args) → string`

计算合并键。键完全由 `sql` 与参数绑定决定：

- **空参数列表** `[]` 是一个合法且独立的键，不与任何带参数的请求混用。
- 每个参数有三种互不混淆的状态：
  - **缺失**（`missing: true`）：该参数在外层行上不存在 —— 键分量 `'missing'`；
  - **存在且为 NULL**（`value: null`）：键分量 `'present'` + `['null', null]`；
  - **存在且有值**：键分量 `'present'` + 带类型的值（如 `['string', 'alice']`、`['number', '1']`）。
- 参数名唯一；参数按名排序后参与键，因此树的书写顺序不影响合并。
- 值带类型标签，`"1"`、`1`、`1n`、`true` 是四个不同的键。

### `EvalError` / `CODES`

`EvalError` 携带 `code`、`reason`、`rowIndex`（原外层行号，0 起）、`path`（行内求值路径）、`detail`。`CODES` 列出全部稳定错误码：

| code | 含义 |
| --- | --- |
| `SCALAR_CARDINALITY` | 标量子查询返回多于 1 行 |
| `DIV_BY_ZERO` | 除数为 0 |
| `TYPE` | 类型不匹配（算术非数字、条件非布尔、参数为 `undefined` 等） |
| `NULL_CONDITION` | `IF` 条件求值为 NULL/UNKNOWN |
| `MISSING_PARAM` | `missingCorrelation: 'error'` 下的相关参数缺失 |
| `BAD_KEY` / `DUPLICATE_KEY` | 子查询参数描述非法 / 参数名重复 |
| `UNKNOWN_NODE` | 未知节点类型 |
| `RUNNER` | 执行器抛错或未按约定作答 |
| `BAD_RESPONSE` | 回包结构非法（未知/重复/缺失 requestId，行列数不为 1，`rows` 与 `error` 同时或都未出现） |

### `ReferenceInterpreter`（`src/reference.js`）

一个刻意写得朴素的**逐行参考解释器**，作为批引擎的 oracle：逐行、按顺序求值，每次都真发一次查询，完全不做合并。它记录每次调用的行号、路径、SQL、参数与返回行，用于核对“发起次数、行内容、错误位置”。与批引擎**不共享任何求值代码**，避免用同一份实现自证正确。

### `startRow` / `Suspension` / `RowContext`（`src/interpreter.js`）

行内求值器。遇到标量子查询时抛出 `Suspension`（携带请求与 `resume` 续延），由批引擎收集、合并、解析后续跑。采用**续延传递**而不是生成器/`Promise` 延迟，使得“只有真正走到的节点才会登记请求或报错”——这正是 `IF` 分支剪枝精确的根因。

## 表达式节点

```js
{ kind: 'const', value }
{ kind: 'param', name }
{ kind: 'if', cond, then, else }            // 惰性：只求值选中分支
{ kind: 'scalar', sql, args: [argSpec] }    // 相关标量子查询
{ kind: 'error', code?, reason?, detail? }  // 显式本地失败
{ kind: 'add'|'sub'|'mul'|'div', left, right }
{ kind: 'neg', arg }
{ kind: 'coalesce', items: [node] }
```

`scalar` 的 `argSpec` 必须恰好给出 `param`、`value`、`missing` 三者之一：

- `{ name, param: 'customer' }`：从外层行环境取 `params.customer`；不存在时按 `missingCorrelation` 处理；
- `{ name, value: 42 }`：常量参数；
- `{ name, missing: true }`：显式声明该相关键在此请求中缺失。

条件节点（`cond`、比较、布尔组合）支持 `const`/`param` 作操作数，以及 `not`、`and`、`or`、`eq`/`ne`/`lt`/`lte`/`gt`/`gte`，采用 SQL 三值逻辑；`and`/`or` 在结果已确定时短路。

## 语义约定（验收要点）

1. **外层行保序 + 保重复**：`values[i]` 恒对应 `rows[i]`，重复行各自产出，绝不合并成一行。
2. **同一读视图内合并**：`(sql, 参数)` 完全相同的请求只发起一次，回包广播给所有提出该请求的行。
3. **空参数独立**：`args: []` 是合法键；**缺失参数与 NULL 参数是不同键**，三者永不相混。
4. **标量子查询行数**：0 行 → `NULL`；1 行 → 该值；多行 → `SCALAR_CARDINALITY`，位置记在**原外层行号**与行内路径上。
5. **回包乱序**：回包可按任意顺序到达，按 `requestId` 匹配，与到达顺序无关；投影顺序不受影响。
6. **只报最早有效错误**：每行只记第一个错误；整批取**最小外层行号**的那个错误；不返回任何部分结果；行号大于该错误的外层行不再继续求值。
7. **未选分支无害**：未选中的 `IF` 分支即使会得到多行或显式错误，也不会被求值、不会发查询、不会影响选中分支的成功结果。
8. **错误位置**：由 `rowIndex` + `path`（如 `["then","left"]`）+ `code` 共同定位，批引擎与逐行解释器必须一致。

## 回放：两行共享查询 + 第三行更早子表达式失败

`scripts/replay.mjs`（并作为 `tests/replay.test.mjs` 断言）运行固定场景：

```
row 0: IF hasTotal THEN ((0 / divisor) + scalar(orders.total WHERE customer))
row 1: 同形状，customer 与 row 0 相同
row 2: 同形状，divisor = 0
```

要点与预期（回放会打印并断言）：

- 发起次数：批引擎对共享的 `customer=alice` 只发起 **1** 次；逐行解释器发起 **2** 次（每行一次）。行 2 因为左侧 `0 / 0` 在到达子查询前就失败，**不会**为它发起查询。
- 行内容：共享请求参数为 `[{ name: 'customer', value: 'alice' }]`，回包行为 `[[100]]`，两行都取到 `100`。
- 错误位置：两套实现都报 `rowIndex: 2`、`code: DIV_BY_ZERO`、`path: ['then','left']`，且都**不返回任何行值**。批引擎 `ok: false` 且 `values === undefined`。

`node scripts/replay.mjs` 的退出码为 0 当且仅当全部检查通过。

## 测试与验证范围

`tests/*.test.mjs` 使用内置 `node:test`，共 49 项，分五个文件：

| 文件 | 覆盖 |
| --- | --- |
| `tests/engine.test.mjs` | 键编码（空参数、缺失 vs NULL、类型区分、重复参数名）；正常路径（0/1/多行、NULL 传播）；IF 剪枝与未选分支含多行；缺失 vs NULL 相关键；乱序/逆序/打乱回包；最早错误与“不再求值更后行”；读视图缓存与跨批次复用；runner 报错与非法回包；空批；单行内两个子查询合并为一批 |
| `tests/edge.test.mjs` | 空参数跨行合并；空参数/缺失/NULL 三键并存于同一批；未选分支含多行与显式错误均不发作；**先挂起、解析后才发现的行 0 错误反超行 1 本地错误**；第三行多行错误定位到原行号；乱序/打乱回包保序；混合批与参考解释器一致 |
| `tests/reference.test.mjs` | 批引擎 vs 逐行参考解释器的差分测试：值（含顺序与重复）、错误（码/行号/路径）、请求负载集合、发起次数上界；跨读视图交错；多次 `await` 下 requestId 映射 |
| `tests/readview.test.mjs` | 同一读视图跨批次复用的统计：新发请求只记本批行号，复用条目按批次分列行号，不跨批串号 |
| `tests/replay.test.mjs` | 题目要求回放的独立断言；并逐条检查回放的全部 `checks` |

**真实执行结果**：上述 49 项均在本机实际运行并通过（`node --test tests/*.test.mjs` → `pass 49 / fail 0`），`node scripts/replay.mjs` → 退出码 0。日志/结论均来自真实测试输出，未用预置输出替代。

验证覆盖的**边界与交错**场景（均真实执行）：

- 0 行 / 1 行 / 多行；NULL 参与算术的传播。
- 空参数、缺失键、NULL 键在同一批内互相区分且分别合并。
- 未选中分支含多行结果、含显式 `error` 节点——均不发查询、不影响选中分支。
- 回包按到达、逆序、随机打乱三种顺序。
- 同一批内先挂起后失败、两行同时失败、后行先失败等错误先后关系。
- 同一读视图跨批次复用、不同读视图不共享缓存、数据在读视图之间变化。
- 执行器抛错、非法回包（未知/重复/缺失 requestId，多列行，`rows` 与 `error` 冲突或都缺）。
- 空批。

**明确未覆盖 / 未执行**：

- 未针对任何真实数据库、真实网络或真实账户做验证（本轮不允许，也不需要）。
- 未测试超大规模（百万行）性能或内存表现；模块关注语义正确性，批次规模仅覆盖到数十行。
- 未提供 SQL 解析器；`sql` 是不透明字符串，由执行器自行解释。
- 未涉及子查询内部再嵌套子查询的跨层合并（求值树对每个 `scalar` 节点独立合并，未做跨层重写）。

## 目录结构

```
src/
  index.js        公开入口
  errors.js       EvalError 与错误码
  key.js          合并键编码（空参/缺失/NULL/类型）
  interpreter.js  行内续延式求值器（Suspension）
  engine.js       ReadView + 批调度（合并、乱序映射、最早错误）
  reference.js    逐行参考解释器（oracle）
  fixtures.js     本地合成表 + 记录型执行器
  scenarios.js    回放场景（脚本与测试共用）
tests/
  engine.test.mjs
  edge.test.mjs
  reference.test.mjs
  readview.test.mjs
  replay.test.mjs
scripts/
  replay.mjs      回放入口
```
