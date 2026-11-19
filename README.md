# SCRAM-SHA-256 / tls-exporter PLUS 双阶段认证库

统一登录端的两阶段认证实现，支持：

- **SCRAM-SHA-256**（RFC 5802 / RFC 7677，基础机制）
- **SCRAM-SHA-256-PLUS**（RFC 9266，在 TLS 1.3 上以 `tls-exporter` 绑定通道数据）
- 两个共享会话存储的工作节点并发处理同一条最终消息时，**至多创建一个登录会话**
- 原连接重送同一最终消息字节，**返回同一份回执**（幂等）
- 密码重置先于最终提交时，旧挑战**立即失效**
- 服务端按**实际交换的消息字节**核验客户端证明，并返回客户端能**独立密码学校验**的服务端签名

## 运行环境

| 项 | 值 |
| --- | --- |
| 运行时 | Node.js **24.15.0**（`engines.node >=24.15.0`） |
| 模块体系 | CommonJS（库）/ ESM（`node:test` 验收测试） |
| 依赖 | 仅 Node 内置 `node:crypto`，无第三方依赖 |

## 实际入口

- 库入口：`index.js`（`package.json#main`）
- 测试入口（已在本机验证可执行真实断言）：

```bash
node --test tests/basic.test.mjs tests/plus-binding.test.mjs tests/session-race.test.mjs
# 或
node --test 'tests/**/*.test.mjs'
```

> `package.json` 同时提供 `npm test` 脚本（`node --test tests/`）；本次核验环境未授权 `npm`，
> 因此以上面的 `node --test` 直接执行为准。

## 目录结构

```
index.js                 统一入口：参与者、存储、注册助手、常量与错误码
src/primitives.js        PBKDF2/HMAC-SHA-256/SHA-256/XOR/base64/恒定时间比较/随机量
src/messages.js          SCRAM 消息编解码、GS2 头拆分、机制与 tls-exporter 常量
src/errors.js            ScramError 与稳定错误码
src/connection.js        TlsConnectionAdapter：模拟 TLS 1.3 连接的 tls-exporter 导出
src/storage.js           AccountStore / ChallengeStore / IdempotencyStore / SessionStore
src/scram-client.js      客户端：构造消息、计算证明、独立核验服务端签名
src/scram-server.js      服务端工作节点：start() 阶段一、final() 阶段二
tests/helpers.js         固定盐、固定两端随机量、两条模拟连接等夹具
tests/basic.test.mjs     RFC 7677 已知答案向量与篡改/错密码拒绝
tests/plus-binding.test.mjs  两条模拟连接上的 PLUS 绑定核对与交错边界
tests/session-race.test.mjs  双节点并发、幂等重送、密码重置竞态
```

## 账户模型（不保存密码）

`AccountStore` 中每条账户记录只含：

| 字段 | 含义 |
| --- | --- |
| `salt` | 盐（Buffer） |
| `iterations` | PBKDF2 迭代数 |
| `storedKey` | `SHA-256(HMAC(SaltedPassword, "Client Key"))` |
| `serverKey` | `HMAC(SaltedPassword, "Server Key")` |
| `credentialVersion` | 凭据版本，密码重置时单调递增 |

明文密码与 `Client Key` 只在 `makeAccountRecord()`（注册/重置）的派生瞬间出现，**绝不入库**。

## 双阶段协议要点

### 阶段一：`ScramServer.start(clientFirstRaw, connection)`

1. 解析 GS2 头：`n,,` → 基础机制；`p=tls-exporter,` → PLUS 机制。
2. PLUS 必须提供连接适配器，且适配器必须能导出本次连接的绑定字节。
3. 加载账户并生成 `client-nonce + server-nonce` 完整 nonce，返回 `server-first-message`。
4. 冻结挑战上下文：实际收到的首消息字节、bare 串、发出的 server-first 字节、
   **凭据版本**、**原连接标识**、**tls-exporter 绑定字节**。

### 阶段二：`ScramServer.final(clientFinalRaw, connection)`

1. 以最终消息字节的 SHA-256 作为幂等键，命中则直接返回原回执（`replayed: true`）。
2. PLUS 硬性核对：提交连接标识必须等于阶段一冻结的原连接；
   连接实时导出的绑定字节必须与冻结值**恒定时间逐字节相等**。
3. 账户当前 `credentialVersion` 与挑战冻结版本不一致 → 判定为“重置先于提交”，拒绝。
4. 用**实际交换的原始字节**重建 `AuthMessage = client-first-bare,server-first,client-final-without-proof`，
   以 `ClientKey = Proof XOR HMAC(StoredKey, AuthMessage)` 恢复密钥并核对 `SHA-256(ClientKey)`。
5. 计算服务端签名 `HMAC(ServerKey, AuthMessage)` 放入 `server-final-message`；
   客户端用自己派生的 ServerKey 对同一 AuthMessage 独立重算比对，不依赖服务端的成功标志。
6. 通过共享 `IdempotencyStore.putIfAbsent` 的原子插入保证：并发双节点只有一个创建会话，
   另一个返回同一回执；`SessionStore` 中始终至多一条会话。

### 模拟连接适配器

`TlsConnectionAdapter` 对应真实部署中的 `SSL_export_keying_material`
（RFC 8446 / RFC 9266 的 `EXPORTER-Channel-Binding`）。测试夹具提供两条连接：

- `conn-A`：绑定字节 `exporter-binding-connection-A-0001`
- `conn-B`：绑定字节 `exporter-binding-connection-B-0002`

同一连接多次导出恒定，不同连接互不相同。

## 验证范围（17 个 `node:test` 用例，全部通过）

### 可接受结果（应成功）
- RFC 7677 §3 向量：PBKDF2/ClientKey/StoredKey/ServerKey 与规范公布值一致，消息逐字节可复现。
- 连接 A 上完整 PLUS 交换：双方证明通过，服务端签名可被客户端独立校验，仅建一个会话。
- 密码重置后，新密码可在新一轮交换成功。

### 应被阻止/拒绝的结果
- 篡改客户端证明、错误密码 → `INVALID_PROOF`，不产生会话。
- 篡改服务端签名 → 客户端独立校验失败。
- PLUS 最终消息换到另一条连接提交 → `CONNECTION_MISMATCH`；
  伪造连接标识但绑定字节不符 → `CHANNEL_BINDING_MISMATCH`。
- PLUS 缺少连接适配器（客户端/服务端两侧）→ 分别拒绝。
- 重置先于最终提交 → `STALE_CREDENTIAL_VERSION`，旧挑战作废。
- 同一挑战提交不同的最终消息 → `CHALLENGE_COMPLETED`。
- 未知账户 / 未知挑战 → 对应明确错误码。

### 边界与交错场景
- 同一条最终消息**并发**投递到两个共享存储的工作节点：恰好一个节点创建、
  另一个节点重放，`sessionId` 相同，会话总数恒为 1。
- 原连接重送同一字节：拿回原回执（同 sessionId、同服务端签名、同 conversationId）。
- 同字节跨连接重放走幂等返回；同挑战的不同字节换连接则被拒绝。
- 重置发生在已成功消息的重送之前：幂等优先，仍返回原回执而不会重新认证或新建会话。
- 重置后旧密码在新挑战上证明核验失败。

## 安全说明

- 所有密钥比较走 `crypto.timingSafeEqual`，长度不等直接判否。
- nonce 使用 `crypto.randomBytes`（生产路径）；测试通过注入生成器固定两端随机量以保证可复现。
- 库只做本地协议核验与状态管理，不含任何真实网络探测；TLS 导出以本地合成夹具模拟。
