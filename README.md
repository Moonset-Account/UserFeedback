# 边缘缓存的条件重新验证

纯 Node.js（ECMAScript modules，零依赖）实现的边缘缓存，聚焦于带 `ETag` /
`Vary` 的条件重新验证、请求合并（coalescing）、独立等待截止以及上游失败时的
陈旧（stale）服务。

- 运行时：Node.js 24.15.0，仅使用标准库（`node:test`、`node:assert`）。
- 无任何外部依赖，无需 `npm install`。

## 目录结构

```
src/
  errors.js       错误类型（UpstreamError / TimeoutError / CacheError）
  vary.js         Vary 解析、首部规范化与变体索引键
  edge-cache.js   EdgeCache 核心实现
  index.js        公共导出
tests/
  helpers/
    clock.js             可控时钟与可注入的假定时器（不替换全局 setTimeout）
    controlled-origin.js 手动放行（gate）的确定性测试上游
  vary.test.mjs           Vary 工具单元测试
  cache-basic.test.mjs    命中、两路变体、合并与版本隔离
  revalidation.test.mjs   304 刷新、迟到 304、Vary 重建、并发 200
  failure-stale.test.mjs  5xx/网络错误、按等待者的陈旧判定与边界
  deadline.test.mjs       独立截止、超时陈旧回退、迟到响应不覆盖
  integration.test.mjs    题面综合验收场景
  edge-cases.test.mjs     Vary: *、Cache-Control、首部大小写
```

## 公开 API

### `new EdgeCache(options)`

| 选项 | 类型 | 说明 |
| --- | --- | --- |
| `origin` | `(req) => Promise<response>` | 必填。上游函数，接收 `{ url, headers }`，返回 `{ status, headers, body }` |
| `now` | `() => number` | 可选，可注入时钟，默认 `Date.now` |
| `setTimeout` / `clearTimeout` | 函数 | 可选，可注入调度器，默认全局实现 |

`origin` 收到的 `headers` 为 `[name, value]` 数组（name 已小写）。条件请求会
自动附带 `if-none-match: <ETag>`。

### `cache.fetch(options) -> Promise<result>`

| 选项 | 说明 |
| --- | --- |
| `url` | 必填 |
| `headers` | 普通对象 / `Map` / `Headers` / `[name, value]` 数组 |
| `forceRefresh` | 跳过缓存查找，发起无条件抓取并存储新的 200 |
| `deadlineMs` | 该等待者自己的截止时间（毫秒），到点独立结算 |
| `maxStaleMs` | 允许的额外陈旧时间；`true` 表示不限，但**不会超过实体自身陈旧窗口** |

`result` 字段：`status`、`headers`、`body`、`fromCache`、`revalidated`、
`stale`、`etag`、`variantKey`、`servedAt`。

### 检查方法（测试/可观测）

- `getStats()`：`hits / misses / revalidations / coalesced / fullFetches /
  notModified / superseded / staleServed / timeouts / upstreamFailures /
  varyRebuilds` 计数快照。
- `variantCount(url)`、`getVaryFields(url)`、`getEntry(url, headers)`、
  `inflightCount()`。

### 缓存实体字段

每个缓存实体携带 `etag`、`varyFields`（解析后的 Vary 字段）、`originTime`
（来源时间）、`freshMs`（新鲜寿命）与 `staleMs`（可用陈旧时限）。

新鲜/陈旧窗口可通过响应头 `x-cache-fresh-ms` / `x-cache-stale-ms` 显式给出，
缺省时回退到 `Cache-Control: max-age` / `stale-while-revalidate`。

### 错误

- `UpstreamError`（`code` 区分 `UPSTREAM_5XX`、`UPSTREAM_NON_2XX`、
  `STALE_304` 等）：上游失败或 304 无法应用，且没有可服务的陈旧实体。
- `TimeoutError`（`code = DEADLINE_EXCEEDED`）：等待者自己的截止到期且无
  陈旧实体可回退。

## 关键语义

1. **合并条件**：只有选中同一变体（variant key 相同）**且**验证同一实体版本
   （ETag 相同）的请求才共享一次上游操作；不同 ETag 或不同变体各自发起调用。
   冷缓存的无条件抓取没有实体版本，因此不参与合并。
2. **独立截止**：每个等待者持有自己的 `deadlineMs`。某个等待者超时后按其自身
   陈旧许可回退（返回陈旧实体或拒绝），并从操作中摘除；共享上游调用与其他等待
   者不受影响。
3. **304 只刷新发起时指定的实体**：若期间已有更新的 200（不同 ETag）落到该
   变体，迟到的 304 不会把旧实体重新变新，等待者拿到当前更新实体。
4. **304 携带新 Vary**：用**发起请求**的首部重建变体索引，删除所有旧索引别
   名；若目标键上已存在更新（不同 ETag）的实体，该实体保持可用。
5. **失败时的陈旧服务**：5xx 或网络错误时，按每个等待者各自的 `maxStaleMs`
   判定能否返回旧实体；有效上限为
   `freshMs + min(staleMs, maxStaleMs)`，**绝不延长实体自身有效期**。

## 复现步骤

```bash
node --version          # 期望 v24.15.0
node --test tests/*.test.mjs
# 或
npm test
```

所有上游与时间均由测试替身驱动（`controlled-origin.js` 的手动 gate 与
`clock.js` 的可注入定时器），不访问真实网络，也不使用真实定时器。

## 验证范围（真实断言，非演示预置输出）

共 29 个 `node:test` 用例，覆盖：

- 正常路径：冷未命中存储、新鲜命中不触达上游、304 刷新并更新来源时间。
- 两路读取：带/不带同一请求首部选中不同变体，验证 `variantKey`、正文与后续
  命中；`Vary: *` 下响应不可被后续请求复用。
- 并发交错：同变体同 ETag 合并为一次调用；并发 200 与迟到 304 的胜负顺序；
  不同 ETag 不合并；304 携带新 Vary 时的索引重建与目标键占位保留。
- 故障恢复（验收项）：5xx、非 2xx、网络 reject 下按等待者陈旧窗口分别成功或
  拒绝；陈旧服务不修改 `originTime`；窗口耗尽后即使上游失败也必须拒绝。
- 边界（验收项）：age 恰好等于新鲜寿命仍算新鲜；age 恰好等于陈旧窗口边界仍可
  服务、超出 1ms 即拒绝；缺失首部与空首部区分；首部大小写不敏感。
- 并发截止（验收项）：同一合并操作上两个等待者，短截止者先超时回退陈旧结果，
  长截止者仍拿到真实 304；超时后到达的 200 仍会写入缓存供后续命中，但不会二次
  结算已过期的等待者；冷抓取截止到期后迟到响应照常入库。

未覆盖项：真实 HTTP 栈与网络 I/O（按要求仅使用本地合成数据）、持久化与容量
淘汰策略（本实现为内存索引，不在题目范围内）。
