# 网关客户端地址解析模块

为入口限流提供可信的客户端地址。渠道同时存在直连与经两层反向代理的请求，本模块按
"从连接对端逐跳向前核对" 的规则解析转发链，给出客户端地址及所使用的证据段。

## 实际入口

- 模块入口：`index.mjs`（ re-export 自 `src/resolve-client.mjs` 与 `src/ip.mjs`）
- 核心函数：`resolveClientAddress({ remoteAddress, forwardedFor, entry })`
- 运行环境：Node.js 24.15.0（ESM，`"type": "module"`）
- 测试入口：`npm test`，即 `node --test tests/*.test.mjs`

## 解析规则

输入：

- `remoteAddress`：实际连接对端地址（必需，非法则拒绝）
- `forwardedFor`：请求声明的转发链（X-Forwarded-For 原始值，可缺省表示直连）
- `entry`：入口配置 `{ name, trustedProxies: [IP/CIDR], maxHops, onInvalid: 'reject'|'fallback' }`

判定顺序（前者优先）：

1. 连接对端不是合法 IP → `rejected`，原因 `invalid-peer-address`。
2. 未声明转发链 → 直连，`clientAddress = remoteAddress`，证据 `source: 'connection'`。
3. 链格式错误（空段、非法 IP）→ 按 `onInvalid`：`reject` 拒绝（原因
   `malformed-forwarded-chain`）或 `fallback` 回退到连接对端。
4. 链跳数超过 `maxHops` → 按 `onInvalid` 拒绝（原因 `hop-limit-exceeded`）或回退。
5. 连接对端不在 `trustedProxies` 内 → 该链不是经可信入口声明的，不参与判断，
   使用连接对端（证据原因 `peer-not-trusted-chain-ignored`）。
6. 从连接对端开始，沿链尾向链首逐跳核对可信性，停在首个不受信的跳作为客户端地址；
   整条链均受信时取链首声明。

输出：`{ status: 'resolved'|'rejected', clientAddress, evidence }`。`evidence` 说明使用了
哪段证据：直连/回退时 `source: 'connection'` 并带原因；走转发链时给出 `declaredChain`、
`trustedHops`（已核实受信的跳，含连接对端）、`untrustedHop`（首个不受信的跳）与
`usedSegment`（实际采信的链段）。

## 验证范围（tests/）

`tests/resolve-client.test.mjs`（14 例）与 `tests/ip.test.mjs`（6 例）均使用 `node:test`
+ `node:assert/strict` 真实断言，覆盖：

- **可接受结果**：两层可信代理链解析出真实客户端；直连取连接对端；链中混有不受信的跳时
  停在首个不受信处；整链受信取链首；IPv6 链；CIDR 子网边界地址。
- **应被阻止/拒绝的结果**：链格式错误（reject 策略）、跳数超限、连接对端非法。
- **边界/交错场景**：跳数恰好等于 `maxHops`（接受）与多一跳（拒绝）；不受信对端声明的链
  被忽略；`fallback` 策略下格式错误与超跳数均回退到连接对端并注明证据；格式错误与超跳数
  同时出现时格式错误优先判定。

IP/CIDR 工具（`src/ip.mjs`）单独验证：IPv4 严格解析（含前导零拒绝）、IPv6 压缩与嵌入
IPv4 形式、IPv4/IPv6 CIDR 匹配与跨版本不匹配。

## 使用示例

```js
import { resolveClientAddress } from './index.mjs';

const result = resolveClientAddress({
  remoteAddress: '10.0.0.2',
  forwardedFor: '203.0.113.7, 172.16.5.4',
  entry: {
    name: 'edge',
    trustedProxies: ['10.0.0.0/8', '172.16.0.0/12'],
    maxHops: 4,
    onInvalid: 'reject',
  },
});
// result.clientAddress === '203.0.113.7'
// result.evidence.trustedHops === ['10.0.0.2', '172.16.5.4']
```
