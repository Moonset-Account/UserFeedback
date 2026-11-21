# 散列槽迁移路由模型（ASK / MOVED / TRYAGAIN）

单进程内存实现，模拟键值集群把一个散列槽从源节点 **A** 逐键搬迁到目标节点 **B**
期间的路由规则。语义对齐 Redis Cluster 的 MIGRATING / IMPORTING 状态机与
ASK / MOVED 重定向约定，但不包含任何真实网络、集群或第三方依赖：仅使用
Node.js 24.15.0 标准库与 ECMAScript modules。

## 要解决的问题

客户端缓存的槽主可能仍是 A，而槽正在搬迁。本模型保证：

1. **逐键原子搬迁**：每次只搬一个键；B 导入成功后 A 才删除源键，失败则键原样留在 A。
2. **搬迁期间的归属规则**：
   - A 只服务**尚留在 A** 的键；
   - 其余键（从未存在的新键、已搬走的键、搬走后又在 B 删除的键）的读和新建都归 B；
3. **一次性重定向 ASK**：发到 A 的操作若全归 B，A 返回 ASK。客户端**只重试这一次**，
   重试时向 A 索要一次性 ASK 指示并出示给 B，**不更新本地槽表**。
4. **多键读 TRYAGAIN**：同槽多键若在 A、B 两侧都有归属，返回暂缓（TRYAGAIN），
   **不返回任何部分值**；全部在一侧时才整组服务或整组 ASK。
5. **B 的一次性准入**：B 在 IMPORTING 状态不主动服务，普通请求只凭一次性 ASK 指示
   放行一次；迁入请求（`importKey`）只凭一次性迁入许可（ImportGrant）放行一次。
   票据用后即焚，复用、错槽、错节点一律拒绝。
6. **永久重定向 MOVED**：最后一个源键搬完且**显式切换槽主**后，A 对该槽的一切请求
   返回 MOVED，客户端据此**更新缓存槽表**，后续请求直发新槽主 B。
7. **可区分的调用语义**：结果信封携带路由轨迹，调用者能明确区分
   「只重试一次（ASK，槽表不变）」与「改槽表（MOVED，永久更新）」。

## 目录结构

```
src/
  hash.mjs      散列槽计算（FNV-1a 32 位，16384 槽）与同槽键生成工具
  protocol.mjs  路由信号（AskRedirect/MovedRedirect/TryAgain）与一次性票据
  node.mjs      节点状态机：owner / migrating / importing / foreign
  cluster.mjs   集群协调器：槽表、逐键原子搬迁编排、显式槽主切换
  client.mjs    带槽表缓存的智能客户端：ASK 一次性通道、MOVED 自愈、TRYAGAIN 上抛
tests/          node:test 测试（4 个文件，40 个用例），无任何第三方依赖
```

## 公开 API

### `hash.mjs`

| 导出 | 说明 |
| --- | --- |
| `SLOT_COUNT` | 常量 `16384` |
| `keySlot(key): number` | 计算键的散列槽，确定性纯函数 |
| `findKeyForSlot(slot, prefix?): string` | 生成一个落在指定槽的键 |
| `keysForSlot(slot, count): string[]` | 生成 `count` 个互不相同的同槽键 |

### `protocol.mjs`

- 路由信号（以 throw 形式从 `node.handle` 返回）：
  - `AskRedirect(slot, targetNodeId)` — 一次性重定向
  - `MovedRedirect(slot, targetNodeId)` — 永久重定向
  - `TryAgain(slot, localKeys, remoteKeys)` — 多键读暂缓，无部分值
- 一次性指示：
  - `AskTicket` — A 签发，B 凭它放行一次普通请求
  - `ImportGrant` — B 签发，B 凭它放行一次 `importKey`
  - 基类 `OneTimeTicket` 的 `consume(slot, nodeId, kind)` 保证用后即焚
- 异常：`OneTimeTicketRejectedError`、`CrossSlotError`、`WrongSlotStateError`、`ImportConflictError`

### `node.mjs` — `Node`

- 生命周期：`assignSlot(slot)`、`beginMigrating(slot, targetId)`、
  `beginImporting(slot, sourceId)`、`completeMigration(slot)`、`completeImport(slot)`
- 票据签发：`issueAskTicket(slot)`（仅 MIGRATING 的 A）、
  `issueImportGrant(slot)`（仅 IMPORTING 的 B）
- 请求入口：`handle({ op, slot, key?, keys?, value?, ticket? })`
  - `op`：`'get' | 'set' | 'delete' | 'mget' | 'importKey'`
  - 正常返回结果信封；路由信号以异常抛出
- 原子搬键：`exportKey(slot, key, importOnTarget)` — 目标导入成功才删源
- 可观测性：`stateOf(slot)`、`hasKey(key)`、`slotKeys(slot)`、`slotKeyCount(slot)`、
  `emigratedKeys(slot)`、`importedKeys(slot)`、`putLocal(key, value)`（仅合成数据）

### `cluster.mjs` — `Cluster`

- `addNode(node)`、`assignSlot(slot, nodeId)`、`ownerOf(slot)`、`slotTableSnapshot()`
- `seed(nodeId, entries)` — 合成数据本地写入
- 搬迁编排：
  - `beginMigration(slot, sourceId, targetId)` — A=MIGRATING、B=IMPORTING，全局槽表暂不变化
  - `migrateKey(slot, key)` — 申请一次性迁入许可 → B 导入 → A 删除，全程原子
  - `migrateRemainingKeys(slot)` — 逐键搬空该槽
  - `completeMigration(slot)` — 源键清空校验通过后显式切换槽主并更新全局槽表
- `migrationProgress(slot)` — 已搬键、源端剩余数

### `client.mjs` — `SmartClient`

- `get(key)` / `set(key, value)` / `remove(key)` / `multiGet(keys)`
- 每个成功结果信封带 `routing`：
  - `routing.redirectedOnce` — 本次走了 ASK 一次性通道（只重试一次，槽表未动）
  - `routing.slotTableUpdated` — 本次收到 MOVED 并更新了缓存槽表
  - `routing.attempts` — 每一跳的节点、结果（`ASK` / `MOVED` / `TRYAGAIN` / `ok`）与通道
- `multiGet` 跨槽抛 `CrossSlotError`；两侧有归属时 `TryAgain` 原样上抛（无部分值）
- `cachedOwner(slot)` / `cachedSlotTable()` / `stats`（ASK、MOVED、TRYAGAIN 计数）

## 复现步骤

要求 Node.js **24.15.0**（`node -v` 核对），无需安装任何依赖：

```bash
node -v
# v24.15.0

npm test
# 等价于：node --test tests/*.test.mjs
```

## 验证范围

40 个 `node:test` 用例，全部真实执行（无预置输出、无 mock 替代功能验证）：

- **正常场景**：槽主直连读写；完整搬迁生命周期（begin → 逐键 migrateKey → 显式切换）；
  切换后 B 无票据正常服务。
- **一次性 ASK**：搬迁中读已归 B 的键、新建键、删除键均走一次性通道；
  通道只生效一轮，下一次请求仍先发 A；槽表全程不变。
- **一次性准入**：B 拒绝无票据/错票据/复用票据；ASK 票据不能顶替迁入许可；
  错槽错节点票据被拒并作废；一张迁入许可只够一个键。
- **TRYAGAIN**：多键读全在 A 正常返回、全在 B 整组 ASK、两侧都有时 TRYAGAIN
  且错误对象上不存在任何值；搬迁推进后重试可成功。
- **MOVED**：显式切换后 A 永久重定向；客户端更新槽表、后续直发 B；
  持旧槽表快照启动的客户端可自愈。
- **边界场景**：空槽搬迁；源键未清空时拒绝切换；重复搬迁同一键被拒；
  未开始搬迁的编排调用被拒；源/目标相同被拒；非法槽号；跨槽 multiGet；空键列表。
- **原子性**：B 导入冲突或票据被拒时 A 不删键，值原样保留，迁移进度不记录该键。
- **交错场景**：每搬一个键就对全部键发起一轮客户端读，校验服务方与键的实时归属一致；
  「搬走又删除」的键始终归 B（A 持续 ASK，B 返回 miss）。
- **题面对照场景**：用同一张旧槽表读两组键——搬迁中得到 ASK（只重试一次、不改表），
  显式切换后得到 MOVED（更新槽表），两个布尔位可被调用者明确区分。

## 建模约定与非目标

- 单进程同步执行，「原子性」由「B 导入成功之前 A 不删除」的顺序保证；
  不模拟真实网络、超时、并发竞态与故障恢复。
- 所有数据均为本地合成数据；不访问网络目标、不读取凭据、不包含任何攻击/规避载荷。
- 哈希采用 FNV-1a 仅用于稳定分槽，不追求与 Redis 的 CRC16 字节级兼容。
