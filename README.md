# TLS 1.3 事件适配器 — 消息读取层

上层业务把已解密、按序、已认证的 TLS 1.3 应用数据交给本层，本层只回答一个问题：
**此刻哪条业务消息已经完整，可以交给业务处理？**

## 实际入口

| 用途 | 路径 |
| --- | --- |
| 公共入口（`package.json` 的 `main` / `exports`） | `src/index.mjs` |
| 读取层主体 | `src/tls-message-reader.mjs` |
| TLS 事件适配（事件名 → 方法调用） | `src/event-adapter.mjs` |
| 验收测试（`node:test`） | `tests/*.test.mjs` |

运行方式（Node 24.15.0）：

```sh
node --test "tests/*.test.mjs"   # 或 npm test
```

> 注意：`node --test tests/` 在 Node 24 会把 `tests` 当成模块路径而报
> `MODULE_NOT_FOUND`，必须用 glob 或文件名。`package.json` 里的 `test`
> 脚本已使用 glob。

## API 形状

```js
import { createTlsMessageReader, MESSAGE_KINDS } from './src/index.mjs';

const r = createTlsMessageReader();
r.declare({ kind: MESSAGE_KINDS.LENGTH, length: 5, token: 'header' });
r.declare({ kind: MESSAGE_KINDS.UNTIL_CLOSE, token: 'body' });

r.push(buf);              // 按序、已认证的应用数据
r.localStopWriting();     // 本地停止写入（半关闭）
r.peerCloseNotify();      // 对端 close_notify
r.disconnect(err);        // 底层断开

r.takeConfirmed();        // 取走自上次调用以来确认的消息
r.snapshot();             // 只读状态快照
```

事件适配器（`createTlsEventReader`）接受 `{ type, ... }` 形式的事件，
支持 `declare` / `data` / `peer-close-notify` / `local-stop-writing` /
`disconnect`；未知事件类型直接抛 `TypeError`，而不是静默丢弃。

## 语义决策

1. **两类消息**
   - `length`：声明长度的消息，收满声明字节数**立即**确认，不依赖 close_notify。
   - `untilClose`：占用该方向余下全部字节，**只在收到对端 close_notify 时**确认；
     声明它之后不允许再声明别的消息（`UNTIL_CLOSE_TERMINAL`），因为它必须吃到流尾。
2. **确认发生在真实的帧边界上**。由数据收满的消息不会被随后到达的 close_notify
   改标为 `completedBy: 'close_notify'`——同一 tick 内的事件顺序不应改变结论。
3. **close_notify 不制造字节**。声明长度但未收满（含一个字节都没收到）的 length
   消息在 close_notify 时是协议错误：记入 `truncated`，并以
   `INCOMPLETE_AT_CLOSE` 抛错，绝不当作已完成。
4. **底层断开不确认任何东西**。已确认的消息保留，正在进行中的消息记为
   `truncated`（`reason` 区分 `transport_disconnect` / `transport_error`）；
   断开后继续 `push` / `declare` 会以 `ALREADY_TRUNCATED` 拒绝。
5. **两个半关闭互相独立**
   - 本地停止写入：只结束本方向出口，接收与确认照常进行，不截断任何消息。
   - 对端 close_notify：只结束对端写入方向，`canRead()` 变 false，但
     `canWrite()` 仍为 true，业务可以继续完成并发出自己的回应。
6. **未声明的字节直接拒绝**（`UNEXPECTED_DATA`），不缓存等待“也许之后会声明”；
   越界字节之前已完整的那条消息仍然会先被确认。

## 验证范围

`node --test "tests/*.test.mjs"` 共 35 个 `node:test` 用例，全部执行真实断言。

| 文件 | 覆盖 |
| --- | --- |
| `tests/length-and-until-close.test.mjs` | 长度消息即时确认、任意切分重组、单块切出多条消息、零长度消息；untilClose 仅由 close_notify 确认、空 untilClose、与长度消息混排、不得后接声明 |
| `tests/disconnect-and-half-close.test.mjs` | 断开保留已确认 + 截断在途、绝不确认半条、untilClose 在途被截断、帧边界断开无截断、断开后拒绝数据/声明/close_notify、重复断开幂等；本地半关闭后继续读、本地半关闭不截断、对端半关闭后仍可回应、对端半关闭后拒绝数据 |
| `tests/boundaries-and-adapter.test.mjs` | 跨 push 精确边界、恰好收满无残留、差一字节不得由 close_notify 补齐、声明但零字节在 close_notify 时拒绝、完整+残缺混合只确认完整者、调用方复用 Buffer 不得污染已确认数据、自动 token 不与显式 token 冲突、未声明字节拒绝、重复 token 拒绝、`maxBuffered` 上限；事件适配器驱动、断开事件、未知事件拒绝 |

其中：
- **可接受结果**：长度消息收满即确认；untilClose 在 close_notify 时确认；
  两个半关闭各自不影响另一方向。
- **应被拒绝/阻止**：未声明字节、声明后的重复 token、untilClose 之后的新声明、
  长度消息不完整时的 close_notify、断开后的数据/声明。
- **影响两者的边界/交错场景**：差一字节 + close_notify、声明但零字节 + close_notify、
  恰好收满 + 紧随的 close_notify（不得改标）、跨 push 的精确帧边界、
  untilClose 在途时断开（截断而非确认）、本地半关闭在 untilClose 在途时（不得截断）。

## 数据边界

所有数据均为本地合成的 `Buffer` 夹具；不联网、不探测真实目标、不读写账号或凭据配置。
