# 推理服务编译缓存层

为推理服务实现的编译缓存：编译器通过**可控回调**异步返回 `{ plan, occupiedBytes, guard }`，
缓存层负责形状守卫匹配、相同请求合并、固定/淘汰、模型版本切换与通用执行回退。

- 运行时：Node.js **24.15.0**，仅使用标准库，ECMAScript modules（`"type": "module"`）
- 零依赖，不访问网络，不读取凭据，所有数据均为测试内合成数据
- 测试：内置 `node:test` + `node:assert/strict`

## 目录结构

```
src/
  guards.mjs    形状守卫：维度范围、维间相等/整除、布局与数据类型
  compiler.mjs  可控编译器回调（DeferredCompiler / HandlerCompiler）
  cache.mjs     CompilationCache：合并、固定、LRU 淘汰、版本切换、通用回退
tests/
  guards.test.mjs
  compiler.test.mjs
  cache.test.mjs
```

## 复现步骤

```bash
node --version        # 必须为 v24.15.0
node --test           # 自动发现 tests/*.test.mjs，当前 37/37 通过
# 或显式指定：
node --test tests/guards.test.mjs tests/compiler.test.mjs tests/cache.test.mjs
```

不需要 `npm install`（无第三方依赖）。`package.json` 中另提供 `npm test` 脚本，内容与上面的
显式命令等价。

## 公开 API

### `src/guards.mjs`

- `normalizeShape(shape)`：校验维度均为正整数，返回冻结副本。
- `normalizeGuard(guard, rank)`：校验并冻结守卫。守卫字段：
  - `dims: [{ min, max }, ...]`，每维闭区间；缺省为 `[1, MAX_SAFE_INTEGER]`
  - `equal: [[a, b], ...]`：要求 `shape[a] === shape[b]`
  - `divisible: [{ numerator, denominator }, ...]`：要求
    `shape[numerator] % shape[denominator] === 0`
  - `layout` / `dtype`：字符串，给出时必须与请求完全相等；缺省不约束
- `guardMatches(guard, shape, request) -> boolean`：**全部**子句通过才返回 `true`。
  只比较各维最大值是不够的——两个维度可以各自落在区间内，却违反维间相等或整除条件
  （该反例在 `tests/guards.test.mjs` 与 `tests/cache.test.mjs` 中均有断言）。
- `requestKey(request)`：请求的稳定序列化键，用于合并“完全相同”的并发请求。

### `src/compiler.mjs`

- `new DeferredCompiler()`：记录每次调用（`calls`、`callCount`、`pending`），
  返回挂起的 Promise，由测试通过 `resolveNext(result)` / `rejectNext(error)` 控制完成时机；
  辅助方法 `next()`、`unresolved()`。
- `new HandlerCompiler(fn)`：每次调用执行 `fn(request)` 的简单适配器。
- `makeResult(request, overrides?)`：构造编译结果，默认生成与请求同形状的精确守卫和唯一 plan。

编译器回调签名：

```js
async function compile(request) {
  return { plan, occupiedBytes, guard }; // occupiedBytes 为非负整数
}
```

### `src/cache.mjs`

```js
const cache = new CompilationCache({
  compile,            // 必需，受控编译器回调
  maxBytes,           // 必需，正整数，占用字节总预算
  modelVersion = "v1",
  onGeneric,          // 可选，通用回退观测回调 ({ request, reason, hasPlan }) => void
});
```

- `await cache.acquire(request)` → handle：
  - `request = { modelVersion, shape: number[], layout?, dtype? }`
  - 命中或新编译入缓存：
    `{ status: "cached", cached: true, plan, entryId, reason: null, release() }`
  - 显式通用执行途径：
    `{ status: "generic", cached: false, plan, entryId: null, reason, release() }`
    （`plan` 是可一次性使用的编译结果，或 `null`；`release()` 为空操作）
  - 编译器失败或返回非法结果：Promise reject，不保留任何状态，可直接重试
  - `reason` 取值（导出为 `GENERIC_REASONS`）：
    - `guard-mismatch`：编译产物守卫不覆盖该请求
    - `stale-version`：编译期间模型版本已切换
    - `capacity`：淘汰所有未固定条目后仍放不下
- `cache.release` 通过 handle 上的 `release()` 完成；重复调用幂等。
- `cache.setModelVersion(version)`：切换当前模型版本。
- `cache.snapshot()`：只读诊断（版本、占用、条目、in-flight 数、编译/命中/合并/淘汰/
  通用回退计数及各条目 pins）。

## 关键语义（与题面要求逐条对应）

1. **复用条件**：模型版本一致 **且** 守卫的范围、相等、整除、布局、dtype 全部通过。
   查找按 LRU 时间戳在所有匹配条目中选最近使用者，从不做“只比各维最大值”的判断。
2. **相同请求合并**：完全相同（版本/形状/布局/dtype）的并发 `acquire` 只触发一次编译器调用；
   每个等待者拿到独立 handle 与独立 pin。编译完成后逐等待者复核守卫，
   **新计划只返回给确实被它覆盖的请求**；未覆盖者走 `generic / guard-mismatch`，且结果不入缓存。
3. **版本切换**：
   - 切换时立即清除未固定的旧版本条目；仍被持有（pin > 0）的旧条目保留并继续计入占用，
     直到最后一个 `release()` 才消失；
   - 已经开始的旧版本编译完成后**绝不加入新缓存**，等待者拿到 `generic / stale-version`
     的一次性 plan，持有到自己释放为止；
   - 新版本请求与旧 in-flight 请求键不同，不会错误合并；携带旧版本号的新请求直接拒绝。
4. **容量与淘汰**：仅在未固定（pin = 0）的当前版本条目之间按 LRU 淘汰；
   先在不修改缓存的前提下计算是否放得下，放不下（包括条目本身超过总预算）时
   **明确返回 `generic / capacity`**，不会先部分破坏缓存。
5. **固定**：每个 cached handle 对应一个 pin；固定中的条目免疫淘汰，
   调用方在释放前始终持有原计划。

## 验证范围与验收映射

`node:test` 共 37 个用例，全部真实执行（无预置/录制输出替代）。

| 题面关注点 | 对应用例（tests/cache.test.mjs，除注明外） |
| --- | --- |
| 正常复用、不重复编译 | compiled plan is cached and reused |
| 守卫范围边界（含闭区间端点） | guard ranges allow reuse；guards.test.mjs 范围用例 |
| 不能只比各维最大值（相等反例） | equality guard cannot be bypassed；guards 相等用例 |
| 整除守卫反例 | divisibility guard rejects non-multiple shapes |
| 布局/数据类型 | layout and dtype are part of the guard |
| 相同请求合并 + 独立 handle/pin | identical concurrent requests are merged |
| 不同请求不合并、不盲目共享计划 | non-identical concurrent requests compile separately |
| 新计划只服务被覆盖请求 | a newly compiled plan serves only requests its guard covers；coalesced group whose produced guard covers nobody |
| 故障恢复（失败后重试重新编译） | a coalesced compilation that rejects … retries compile fresh；malformed compiler results |
| LRU 淘汰 / 精确放得下 | LRU unpinned entries are evicted；an entry fitting exactly after eviction |
| 固定条目不可淘汰 | pinned entries cannot be evicted |
| 超预算走通用途径且不污染缓存 | a result larger than the whole budget |
| 版本切换清缓存、旧编译不入新缓存 | version switch drops unpinned old entries |
| 已开始的调用持有原计划直到释放 | pinned old-version entries survive the switch |
| **两次并发编译、其中一次换版本后才返回**（核对计划、编译次数、占用） | interleaving: concurrent compilation finishing after a version switch… |
| 换版本时整组合并请求的 fan-out | interleaving: a coalesced group switched mid-flight |
| 重叠守卫的 MRU 选择 | overlapping guards resolve to the most recently used matching entry |
| 幂等 release / 通用 handle 空操作 | double release is idempotent |
| 输入校验 | constructor and request validation；guards.test.mjs 校验用例 |

交错验收用例中的具体核对点：两次不同形状的 v1 并发编译产生 2 次编译器调用；
第一次正常入缓存（40 字节）；切换到 v2 后新请求发起第 3 次编译；
第二次 v1 编译在切换后完成时返回 `generic / stale-version` 且其 60 字节计划从不入缓存
（占用保持 40）；v2 编译完成后占用变为 40 + 25 = 65，编译总次数恰为 3；
释放后旧版本条目消失，仅留 v2 条目。

## 未覆盖 / 边界说明

- 不涉及真实编译器、真实网络或进程级资源；编译器行为完全由注入回调模拟。
- 守卫在每次匹配时做全量检查，未引入索引/分片等性能优化结构。
- `setModelVersion` 只影响之后的决策；进行中的编译按其发起时的版本归属处理，
  不存在“编译中途改写归属版本”的语义。
