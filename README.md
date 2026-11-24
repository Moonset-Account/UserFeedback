# 身份感知的结构补丁引擎

零依赖、ECMAScript modules 的 JSON 文档事务引擎。数组元素具有隐藏且唯一的身份；路径下标在每笔事务编译时绑定到当时的元素身份。支持旧值/缺失测试、数组片段移动、删除、替换、插入、基于实际前像的逆补丁，以及两笔事务/补丁的符号合成。

运行环境已按题目使用 **Node.js v24.15.0** 验证。无需安装依赖。

## 复现与验证

```bash
node --version          # 应为 v24.15.0
node --test tests/*.test.mjs
# 或
npm test
```

当前测试共 27 个 `node:test` 用例，覆盖正常路径、边界、冲突恢复、同间隙交错、跨数组移动、逆操作（含 JSON 往返后的身份恢复）、固定合成补丁复用，以及 500 组随机双事务等价语料和 300 组逆操作语料。

## 公开 API

```js
import {
  // 事务
  compileTransaction,
  runTransaction,
  applyTransaction, // runTransaction 的别名
  applyPatch,
  tryApplyPatch,
  invertPatch,
  runSequential,

  // 合成
  composeTransactions,
  composePatches,
  planComposition,
  executeComposed,
  applyComposedPatch,

  // 身份与路径
  materialize,
  cloneDocument,
  getIdentities,
  getIdentityAt,
  findIdentityPath,
  parsePath,
  formatPath,
} from './src/index.mjs';
```

### 返回约定

`runTransaction`、`runSequential`、`tryApplyPatch`、`executeComposed`、`applyComposedPatch` 返回结果对象：

```ts
type Result<T> =
  | { ok: true; doc: T; patch?: Patch; composition?: BoundComposition }
  | { ok: false; error: { code: string; message: string; details: object }; doc?: unknown };
```

成功应用补丁的底层函数 `applyPatch(doc, patch)` 直接返回新文档；测试或补丁条件失败时抛出 `TransactionError`。

引擎不会修改文档的可见 JSON 内容。编译时可能会就地初始化不可枚举的隐藏身份向量；应用操作始终在深拷贝上提交，因此冲突或测试失败不会产生半笔写入。

## 操作格式

路径使用 JSON Pointer，例如 `/users/0/name`、`/items/-`；也接受路径段数组和便利的点/下标字符串。

```js
const tx = {
  id: 'optional',
  operations: [
    { op: 'test',       path: '/version', value: 3 },
    { op: 'testAbsent', path: '/deletedAt' },
    { op: 'remove',     path: '/users/0' },
    { op: 'replace',    path: '/title', value: 'new title' },
    { op: 'insert',     path: '/tags/-', value: 'new-tag' },

    // 移动一个或多个连续数组身份；count 默认为 1。
    { op: 'move', from: '/items/0', count: 2, to: '/items/4' },

    // JSON Patch 风格的单元素移动也支持：
    { op: 'move', from: '/items/0', path: '/items/3' },
  ],
};
```

别名：`delete`/`del` 对应 `remove`，`add` 对应 `insert`，`assert` 对应 `test`。缺失测试也可写为 `{ op:'test', path:'/x', absent:true }` 或 `{ op:'test', path:'/x', state:'missing' }`。

`null` 是普通值；测试 `value:null` 与测试缺失严格不同。

## 身份与事务规则

- 每个数组都有一个不可枚举的 symbol 身份向量；`JSON.stringify(doc)` 和 `Object.keys` 不会显示身份。
- `getIdentities(doc, '/items')` 返回该数组当前的隐藏身份顺序；`getIdentityAt(doc, '/items/2')` 返回单个身份。
- 事务编译期间，`/items/2` 绑定到开始快照中该下标的身份。之后即使移动使它的物理下标改变，同一事务内的读、改、删仍跟随该身份。
- 第二笔事务在第一笔提交后的快照上重新绑定下标，因此移动到 `/items/2` 的元素会被第二笔的 `/items/2` 正确引用。
- 所有 `test` 都针对该事务的开始快照求值；同一事务后面的替换不会改变前面/同级测试看到的旧值。
- 两笔普通写入若共享同一个绑定的数组元素身份（常见情况是写到同一元素或其属性），整笔失败。
- 一笔删除/替换/移动若覆盖某祖先，而另一笔写入位于该祖先之下，整笔失败。
- 重叠的移动源片段失败。
- 多个插入或移动落到同一个开始间隙时不视为冲突；它们按操作顺序排列。移动与插入共享同一间隙时也按操作顺序交错。

## 逆补丁

`invertPatch(actualBefore, patch)` 会在内部基于真实前像预演补丁，并在每个操作前捕获：

- 被删值及其全部嵌套隐藏身份；
- 被替换的旧值；
- 插入产生的身份；
- 移动前的身份顺序和恢复下标。

```js
const run = runTransaction(doc, tx);
const inverse = invertPatch(doc, run.patch);
const restored = applyPatch(run.doc, inverse);
```

逆补丁恢复值、缺失键、`null`、数组身份和顺序。测试用 `identityMap()` 递归比较所有数组的身份向量。

## 两笔补丁的符号合成

推荐从初始文档生成一个可复用的、绑定好的合成补丁：

```js
import {
  compileTransaction, applyPatch, composePatches,
  planComposition, applyComposedPatch, invertPatch,
} from './src/index.mjs';

const p1 = compileTransaction(base, tx1);
const d1 = applyPatch(base, p1);
const p2 = compileTransaction(d1, tx2);

// 三种等价入口：
const composedA = composePatches(p1, p2, { base });
const composedB = planComposition(base, tx1, tx2);

// 直接用于另一个满足 initialTests / arrayLength 条件的起始文档：
const result = applyComposedPatch(otherBase, composedA);

// 每个新起始文档重新做符号规划，并只提交一次净补丁：
const oneCommit = executeComposed(yetAnotherBase, tx1, tx2);
```

`planComposition` / `composePatches(..., { base })` 返回：

```js
{
  ok: true,
  initialTests,   // 唯一会在新初始快照上执行的路径/长度条件
  derivedTests,   // 第二笔测试如何由第一笔写入/新增/移动兑现的符号记录
  indexRules,     // 第二笔下标 -> 初始路径/创建元素的条件化映射
  references,
  netPatch,       // 一次应用的净编辑
}
```

可执行的 `composed.netPatch.tests` 只包含：

- 第一笔原有初始路径测试；
- 第二笔中未被第一笔改写或仅被移动的测试，翻译回初始路径；
- `exists`、`absent`、`arrayLength` 等初始快照结构条件。

第二笔对第一笔结果的测试按来源表达：

1. **仅移动过的旧对象**：映射回初始路径，并由 `indexRules` / `arrayTransform` 跟踪身份。
2. **第一笔替换出的常量**：记录为 `static-write-obligation`，最终值直接折叠进净替换。
3. **第一笔新增对象**：记录为 `created-object-obligation`，第二笔的修改/测试折叠进最终插入常量。
4. **移动造成的缺失**：记录为条件化索引/数组变换，而不是错误地要求初始路径缺失。

净数组变换使用初始下标引用：

```js
{ kind: 'initial',      index: 1 }              // 保留初始元素身份和当前值
{ kind: 'initialReset', index: 0, value: ... }  // 保留身份，但值被第一笔整体替换
{ kind: 'new',          value: ... }             // 第一笔新增、经第二笔修改后的最终常量
```

绑定合成对象不保存 `baseDoc`，可序列化结果中也不包含示例隐藏 ID 或整份前像；未改变的子树只通过初始下标/路径引用。固定合成补丁适用于通过其 `initialTests` 与 `arrayLength` 条件的其他起始文档。

## 指定的“同值不同身份”验证

测试 `tests/composition.test.mjs` 和 `tests/identity-transactions.test.mjs` 构造了两个可见 JSON 完全相同的数组：

```js
const a = { items: ['same', 'same', 'same'] };
const b = { items: ['same', 'same', 'same'] };
```

它们的隐藏 ID 不同。第一笔把下标 `0` 移到末尾并提交；第二笔在新快照上测试并替换 `/items/2`。两个文档都修改各自的初始身份，结果均为 `['same','same','moved']`，且末尾身份分别等于各文档原来的第一个身份，证明引擎不是按可见值猜测元素。

## 验证范围

- `tests/identity-transactions.test.mjs`：隐藏性、唯一性、快照读、身份绑定、同事务/跨事务移动、同身份冲突、删除祖先冲突、缺失与 `null`、数组边界。
- `tests/interleaving.test.mjs`：同间隙“移动/插入”两种操作顺序及第二笔事务的重新绑定。
- `tests/inverse.test.mjs`：删除、移动、嵌套身份、缺失键、显式 `null` 的恢复。
- `tests/composition.test.mjs`：移动引用、新增对象引用、第一笔替换常量的二次测试、固定合成补丁在候选文档集合上的成功集合/结果等价、无示例前像泄漏、合成补丁逆操作。
- `tests/equivalence-fuzz.test.mjs`：500 组种子随机双事务，比较顺序执行与 `executeComposed` 的成功集合和深度结果；另用 300 组随机事务检验逆操作。

本项目只使用 Node.js 标准库和本地合成数据，不访问网络、账户、真实目标或凭据。
