# CORS 预检结果缓存（可注入时钟）

内嵌浏览器场景下的 CORS-preflight 缓存实现，参照 Fetch 标准的
*CORS-preflight cache* 建模，纯 ESM、零依赖，使用 Node.js 内置测试运行器验证。

## 入口

| 模块 | 路径 | 说明 |
| --- | --- | --- |
| 预检缓存 | `src/preflight-cache.js` | 核心实现，导出 `PreflightCache`、`DEFAULT_MAX_AGE_SECONDS`、`WILDCARD` |
| 安全清单辅助 | `src/cors-safelist.js` | 判断哪些请求头属于 CORS-不安全头（需要预检），导出 `isCorsSafelistedHeader`、`selectUnsafeHeaders` |

运行环境：Node.js ≥ 20（本机核验版本 v24.15.0）。

## 用法

```js
import { PreflightCache } from './src/preflight-cache.js';
import { selectUnsafeHeaders } from './src/cors-safelist.js';

// 可注入时钟：函数、{ now() } 对象或固定数字；不传则用系统时钟
const clock = { t: 0, now() { return this.t; } };
const cache = new PreflightCache(clock);

// 登记一次预检响应许可
cache.store({
  partition: 'https://embedder.example',   // 顶层站点分区（也支持对象）
  origin: 'https://app.example',           // 请求源
  url: 'https://api.example/v1/data',      // 目标 URL（fragment 不参与匹配）
  credentials: false,                      // 是否带凭据申请
  maxAge: 10,                              // 秒，缺省 5
  allowMethods: ['PUT'],                   // 方法许可，'*' 为通配
  allowHeaders: ['x-custom', 'authorization'], // 头名大小写不敏感
});

// 实际请求前判定能否跳过预检
const decision = cache.check({
  partition: 'https://embedder.example',
  origin: 'https://app.example',
  url: 'https://api.example/v1/data#frag',
  credentials: false,
  method: 'PUT',
  headers: selectUnsafeHeaders([{ name: 'X-Custom', value: '1' }]),
});
// decision.ok === true 才能跳过预检；
// 否则 reason / missingMethods / missingHeaders 指出缺的是哪项许可。
```

`check()` 返回结构：

```ts
{
  ok: boolean,
  reason: 'no-entry' | 'credentials-mismatch' | 'missing-permission' | null,
  missingMethods: string[],   // 未被有效许可覆盖的方法
  missingHeaders: string[],   // 未被有效许可覆盖的头（小写）
  matchedEntry: object | null,// 命中的条目快照（含各字段独立过期时刻）
  checkedAt: number,
}
```

另有 `peek()`（只读查看条目）、`purgeExpired()`（清理全字段过期条目）、
`clear()`、`size` 维护接口。

## 已实现的题面约束

1. **三元组隔离**：每项许可按 `(顶层站点分区, 请求源, 目标 URL)` 存储，
   分区支持字符串或对象（按 JSON 内容区分）；URL 比较忽略 fragment。
2. **字段级独立过期**：方法与每个请求头分别登记过期时刻；
   同一三元组重复 `store()` 只更新本次列出的字段，未列出字段保持原时刻。
3. **全覆盖才放行**：方法与全部待检查头同时有效时 `ok=true`，
   任一缺失/过期都阻止预检跳过，并在结果中逐项指出。
4. **凭据门槛**：带凭据请求只能命中 `credentials=true` 的条目；
   无凭据申请得到的通配许可（即使含显式字段）不能覆盖带凭据请求，
   此时 `reason='credentials-mismatch'`。
5. **Authorization 特例**：通配头 `*` 永远不能覆盖 `Authorization`，
   无凭据请求也不例外；必须存在显式、未过期的 Authorization 许可。
6. **头名大小写等价**：登记与查询统一折叠为小写并去空白。
7. **命中不续期**：`check()` 与 `peek()` 只读不写，任何缓存命中都不延长
   方法/头的过期时刻；到期判定为严格比较（`expiresAt > now`），
   因此 `maxAge: 0` 写入即过期。

## 验证范围与运行方式

```bash
npm test                 # 等价于 node --test tests/*.test.mjs
node --test tests/*.test.mjs
```

测试文件（全部使用 `node:test` + `node:assert/strict`，每个用例含真实断言）：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/preflight-cache.test.mjs` | 时钟注入与入参校验；可接受命中；方法/头缺失与过期的阻止结果及缺项定位；`maxAge=0`、到期前 1ms/到期当刻的边界；头名大小写；通配与凭据约束（含 Authorization 特例）；分区/源/URL 隔离与双站点缺项对比；命中不续期与交错 TTL；维护接口 |
| `tests/cors-safelist.test.mjs` | 安全清单头名、content-type 本质类型、range 形态、Authorization 等非清单头识别；小写折叠/去重/保序 |
| `tests/integration.test.mjs` | 两个模块串联的端到端场景：一个可接受结果、一个被凭据规则阻止的结果、一个时钟推进下短 TTL 字段先到期的交错边界 |

三类必测场景均有覆盖：

- **可接受结果**：方法与全部不安全头在有效期内 → `ok=true`；
- **应被阻止/拒绝**：无条目、缺方法、缺头、凭据不匹配、Authorization 仅有通配；
- **边界/交错**：`maxAge=0`、毫秒级到期临界、同条目字段分批次不同 TTL、
  双分区各缺不同许可、命中不改变原始过期时刻。

## 范围说明

- 本实现只做本地缓存判定逻辑与合成夹具测试，不发起任何真实网络请求、
  不做目标探测；通配与 Authorization 规则按 Fetch 标准 CORS-check 语义实现。
- `cors-safelist.js` 覆盖名称与取值形态判定，标准中按字节长度
  （128/1024 字节等）的限制未强制，缓存层本身不依赖该辅助模块，
  调用方也可以自行传入待检查头列表。
