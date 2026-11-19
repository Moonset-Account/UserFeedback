# 认证挑战处理层（Auth Challenge Client）

内部 API 客户端的认证挑战处理层。传输层与凭据查询均为依赖注入，可整体替换；
本仓库不含任何真实网络代码，所有验收都基于本地合成替身完成。

## 实际入口

- 库入口：`src/index.js`
  - `AuthChallengeClient`（`src/auth-client.js`）：挑战/重定向状态机主体
  - `parseChallenge`（`src/challenge.js`）：`WWW-Authenticate` 解析
  - 内容源工厂（`src/body-source.js`）：
    `createReopenableSource` / `createOneShotSource` / `createEmptySource`
  - 错误类型（`src/errors.js`）：`ReconfirmRequiredError`、
    `ChallengeAlreadyNegotiatedError`、`CredentialMismatchError` 等
- 测试入口（真实断言，基于 `node:test` + `node:assert/strict`）：

  ```bash
  node --test tests/*.test.mjs
  ```

  `package.json` 的 `npm test` 与之等价。当前环境 Node v24.15.0 下
  27 个用例全部通过。

## 可替换的两个协作对象

1. 传输层 `transport(transportRequest) -> Promise<response>`
   - 输入含 `method / url / headers / body（已打开的异步可迭代对象或 null）/ attempt`
   - 输出 `{ status, headers, bytesSent, body? }`
   - `bytesSent` 表示传输在应答前**实际写入的请求体字节数**，重发安全判定依赖它
2. 凭据查询 `credentialProvider({ origin, realm, challenge, request }) -> Promise<credential|null>`
   - 凭据形如 `{ id, origin, realm, authorization }`
   - 客户端会再次校验返回凭据的 `origin` 与 `realm` 是否匹配当前请求，
     不匹配直接抛 `CredentialMismatchError`，且不会把错误身份发到线上

## 实现的交叉约束

### 1. 凭据必须匹配「源 + 认证域」，跨源跳转重新选择

- 401 挑战中的 `realm` 与请求当前 `origin` 共同作为凭据查询条件
- 301/302/303/307/308 跳转后，若 `Location` 解析出的源不同：
  - 旧 `Authorization` 立即删除，绝不带到新源
  - 已知 realm 时立即按新源重选；未知时先匿名，由新源自己的 401 触发选择
- 同源跳转保留已协商身份

### 2. 同一挑战编号只协商一次

- `WWW-Authenticate` 必须携带稳定的 `challenge_id`，否则判为
  `MalformedChallengeError`（避免无编号挑战造成死循环）
- 每个请求链维护一个已协商编号集合；重复编号第二次出现时抛
  `ChallengeAlreadyNegotiatedError`，不会发起第三次线上请求，也不会重复查凭据
- 协商记忆按请求链隔离，新的 `send` 可重新使用旧编号

### 3. 重发安全：未发字节可补凭据；已发字节需双重条件

- 传输确认 `bytesSent === 0`：直接附加凭据重试，不要求幂等键；
  零字节的一次性内容源也允许再次打开（没有任何字节可被重放）
- 传输确认 `bytesSent > 0` 时，只有同时满足以下两条才允许重发：
  1. 调用方提供**可重新打开的内容源**（`body.reopenable === true`，
     重试时重新 `open()`，旧迭代器不被复用）
  2. 调用方提供幂等键（`body.idempotencyKey`，自动放入
     `Idempotency-Key` 请求头），且服务端在挑战/跳转响应中回传
     **值完全相等**的确认头 `X-Idempotency-Acknowledged`
- 任一条件不满足，客户端停止并抛 `ReconfirmRequiredError`
  （`reconfirmRequired: true`，`details.reason` 区分
  `body-not-reopenable` 与 `idempotency-key-not-acknowledged`），
  内容源不会被重开、线上不会出现第二次请求
- 服务端一旦在本请求链内确认过幂等键，对同一可重开源的后续重放
  （例如跳转后又遇 401）持续授权，状态随请求链结束而失效
- 303 跳转按语义转为 GET 并丢弃请求体与幂等头，不触发重放

## 可观察性

`new AuthChallengeClient({ onEvent })` 可接收事件，完整事件同时保留在
`client.events`。关键事件：

| 事件 | 含义 |
| --- | --- |
| `request` / `response` | 每次线上尝试及其 `bytesSent`、所用身份 |
| `challenge` | 收到 401 及解析出的 `challengeId / realm` |
| `challenge-rejected` | 重复编号被拒绝 |
| `reconfirm-required` | 已发字节但双重条件不满足，停止并要求重新确认 |
| `body-reopen-authorized` | 条件满足，允许重开内容源（含 `viaPriorAuthorization`） |
| `credential-selected` | 实际选用的身份 `identity` |
| `redirect` / `origin-changed` | 跳转及跨源身份切换（含 `previousIdentity`） |

## 替身如何复现「先确认一个字节再给挑战」

测试传输（`tests/helpers/fakes.mjs` 的 `createScriptedTransport`）在应答前
先排空请求体并把 `bytesSent` 收敛为 `Math.min(1, 实际字节数)`：
有请求体时先确认写入 1 个字节，再按脚本返回 401/307/200。每次调用都记录
请求头、请求体文本与 `bytesSent`，因此可以直接观察：

- 客户端是停止并要求重新确认（`ReconfirmRequiredError` + 仅 1 次线上调用 +
  内容源 `openCount === 1`），还是重开内容源重试（`openCount` 递增、
  两次请求体文本完整一致）；
- 第二次实际使用的身份（`transport.calls[1].headers.authorization` 与
  最终响应的 `identity` 字段）。

## 验收范围（tests/，27 个用例）

- `challenge-parser.test.mjs`：挑战头解析、引号转义、缺编号/空头拒绝、
  两类内容源的打开语义
- `body-safety.test.mjs`（题面核心交叉约束）：
  - 可接受：1 字节后挑战，可重开源 + 被确认的幂等键 → 重开重试且换新身份
  - 被阻止：一次性内容源 / 缺确认头 / 确认值不匹配 → `ReconfirmRequiredError`
  - 边界与交错：零字节（空体、空一次性源）免幂等直接补凭据；已授权链上
    后续挑战的判定
- `negotiation.test.mjs`：同编号只协商一次、不同编号各自一次、
  协商记忆按请求链隔离、畸形挑战不产生循环
- `credentials-origin.test.mjs`：origin+realm 双重匹配、错误源/错误域凭据被
  客户端拦下、provider 返回 null、跨源 307 丢弃旧身份并按新源重选（含无
  realm 先匿名再被挑战的路径）、跨源一次性源停止、303 丢体转 GET、
  同源跳转保留身份

## 边界与限制（如实说明）

- 本层只定义传输/凭据契约与状态机，不内置 HTTP 实现；接入真实客户端时
  由适配器把真实流包装成内容源、把真实响应头/已写字节数映射到契约字段
- 传输上报的 `bytesSent` 是安全判定的信任边界；适配器必须如实上报
- 挑战解析支持 RFC 7235 常见的参数化与 token68 形式，取首个 challenge
- 不做任何真实目标探测、攻击或载荷生成，全部流量都是本地脚本化替身
