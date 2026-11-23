# federated-query-executor

一个联邦查询执行库：若干服务各自只实现同一实体的一部分字段，库负责把一棵
带别名的查询规划成可终止的取键序列、按调用费用与批上限选出最低费用计划、
合批去重后把乱序结果散回各响应位置、按非空层次传播单项错误，并在带修订号
的实体键与共同可读修订向量之上支持超时存档、恢复重排与迟到回执判定。

- 运行环境：Node.js **24.15.0**（`package.json` 的 `engines` 已锁定）
- 仅使用 Node 标准库与 ECMAScript modules，**零依赖**
- 测试：`node:test`，全部针对本地合成数据，不访问任何真实网络目标

## 复现步骤

```bash
node --version   # 应为 v24.15.0
node --test      # 或 npm test，自动发现 tests/*.test.mjs
```

## 公开 API（`src/index.js`）

### `defineSchema(def)`

声明服务字段图：

```js
const schema = defineSchema({
  entities: {
    User: {
      key: ['id'],                       // 实体身份键（默认 ['id']）
      services: {
        profiles: { key: ['id'],           provides: ['name', 'region'], cost: 4, batchLimit: 2 },
        contacts: { key: ['id', 'region'], provides: ['email'],          cost: 2, batchLimit: 50 },
        social:   { key: ['id', 'region'], provides: ['friends'],        cost: 1, batchLimit: 10 },
      },
      links: { friends: { target: 'User', many: true } },
    },
  },
  coReadable: [{ profiles: 2, contacts: 2, social: 2 }],   // 可选：共同可读修订向量
});
```

- 每个服务声明**复合提取键** `key`、提供的字段 `provides`、单次调用费用
  `cost` 与批上限 `batchLimit`；同一字段允许多个服务作为备选提供方。
- 链接字段（下游键列）必须出现在某个服务的 `provides` 中并在 `links` 声明。

### `planEntityFetch(entity, { available, needed, count })`

纯规划器。对字段可用集合做 Dijkstra 搜索，保证终止且每个服务至多生效一次：

- 成功：`{ ok: true, stages: [{ service, provides, cost }], totalCost }`，
  费用模型为 `ceil(count / batchLimit) * cost` 的总和最小值；
- 失败：`{ ok: false, blocked: { unreachableNeeded, chain, missing } }`，
  其中 `chain` 是互相依赖且无起始键时的**阻塞字段链**（闭合环），
  `missing` 是没有任何服务提供的键字段。

### `executeQuery({ schema, query, reads, coReadable?, checkpoint?, timeoutPolicy? })`

执行一棵带别名的查询树。查询节点：

```js
{
  name: 'me', type: 'User',
  key: { id: '1' },                 // 或 list: true + keys: [...]
  select: [
    { field: 'name', alias: 'n', nonNull: true },
    { field: 'friends', list: true, nonNull: false, itemNonNull: false,
      node: { type: 'User', select: [ ... ] } },
  ],
}
```

`reads[service](keys, ctx)` 是由调用方（测试）驱动的批量读取回调：

- 入参为投影到服务键的复合键数组；返回项**允许乱序**：
  `{ key, fields, revision? }` 或 `{ key, error: { message } }`；
- 抛出 `TimeoutError` 表示整批超时；
- 链接字段值可以是裸键 `{ id: '7' }`，或携带组成键修订的
  `{ key: { id: '7' }, keyRevision: 1 }`。

返回 `{ done, data, errors, stats, vectors, checkpoint }`：

- `stats.reads`：实际发出的每一批（服务、键、数量）——读取次数与取键
  来源的核验依据；`stats.plans` 记录每个实体选中的阶段序列；
  `stats.reused` / `stats.dropped` 记录恢复时存档实体的取舍；
- `errors[].path` 使用别名与列表下标（如 `['me', 'friends', 0, 'email']`）；
- `vectors`：每个实体响应位置实际提交的修订读集；
- `checkpoint`：可序列化的部分状态（已取记录 + 超时待办批次），
  作为下次调用的 `checkpoint` 入参即可恢复。

错误传播：单项错误按声明的非空层次向上置空到最近可空位置
（`nonNull` 字段 → 所在对象；`itemNonNull` 列表 → 整个列表字段；
列表/对象本身可空则在该处置空），其余分支继续。

修订语义：一个响应位置拼接的所有读取（记录修订 + 入站键约束）必须被
某个当前共同可读向量**精确覆盖**（按服务精确匹配，而非支配关系），否则
该分支报 `stitch conflict`——不会拼出任何单一快照中从未存在过的实体。
恢复时，存档记录只有在修订仍共同可读时才被重用，其余字段自动重新规划。

`timeoutPolicy`：`'error'`（默认）把超时批次转为逐项错误继续执行；
`'checkpoint'` 在首个超时批次处中止并返回 `done: false` 与存档。

### `assessLateReceipt({ receipt, branches, coReadable })`

判定一次迟到的旧批回执（如先前超时批次的原始响应）能否用于各个别名
分支。`branches` 形如 `{ 'me.bf': vectors['me.bf'], ... }`。输出每个别名
的 `{ usable, reason, evidence }`，evidence 携带参与比较的修订向量，
可解释"同一回执为何能用于别名 A 而不能用于别名 B"。

### 其他导出

`isCoReadable`、`mergeVectors`（修订向量运算）、`TimeoutError`、`StitchError`。

## 验证范围（全部真实执行，无预置输出）

| 文件 | 覆盖场景 |
| --- | --- |
| `tests/planner.test.mjs` | 经第三个服务的取键链规划；费用×批上限的最低费用计划选择（count=1 选 retail、count=12 选 bulk）；互相依赖无起始键的阻塞字段链；无提供方的缺失根键 |
| `tests/executor.test.mjs` | 合批去重 + 乱序响应散回各响应位置（含重复 id）；复合键来源（contacts 键携带 profiles 的 region）；可空字段单项错误与原始路径；非空字段向上传播；非空项列表整列置空而兄弟分支继续；阻塞字段链作为字段错误暴露且不发出任何读取 |
| `tests/interleaving.test.mjs` | **交错验收场景**：同一实体（User 7）同时出现在可空列表 `friends` 与非空嵌套列表 `buddy.cohorts`；核对实际读取次数（7 在 contacts/profiles 各只读一次）、取键来源（social/contacts 的 region 均来自 profiles）、所有受影响响应路径（两条错误路径 + 两处置空形态） |
| `tests/revisions.test.mjs` | 批次超时 → 部分状态存档 → 在仍共同可读的修订上重用可证有效实体（profiles 零重读）并重排其余取键；修订失效的存档被丢弃重取；成功但携带过期组成键的批次使分支报错而查询继续；同一父字段需拼接两个不兼容修订时整分支按空值层次报错；一次迟到旧批回执对两个别名的可用性证据 |

边界场景（空计划零费用、重复键去重、批上限切批、服务未返回某键转为逐项
错误）均包含在上述测试内。未执行的验证：无——上表所有测试均真实运行。
