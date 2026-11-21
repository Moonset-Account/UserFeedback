# 双向双工字节流中继（duplex-byte-relay）

本地基础设施组件：在两条双工字节流之间做双向中继，带**分方向待写缓冲上限**、
背压暂停、半关闭排空与硬断开上报。纯本地实现，无网络连接、无外部目标、无真实负载。

## 入口

- 核心模块：`src/duplex-relay.js`
  - 命名导出 `DuplexRelay`（同时为默认导出）
- 合成流夹具：`tests/helpers/controllable-duplex.js`
  - `ControllableDuplex`：读写步长、发送缓冲容量、硬断开时机均可控制
  - `EventClock`：全局单调事件序号，用于核对写结束发生的次序
- 验收测试：`tests/duplex-relay.test.mjs`（`node:test` + `node:assert/strict`，真实断言）
- 手动驱动示例：`tests/helpers/manual-drive.mjs`

## 运行

```bash
node --test "tests/**/*.test.mjs"
# 或（package.json 中配置的等价入口）
npm test
# 手动观察一次完整中继
node tests/helpers/manual-drive.mjs
```

环境：Node.js >= 20（本机核验版本 v24.15.0），零第三方依赖。

## 流接口约定（net.Socket 的本地子集）

| 成员 | 语义 |
| --- | --- |
| `on('data', cb)` / `on('end', cb)` / `on('drain', cb)` / `on('error', cb)` | 字节、半关闭、可写恢复、硬断开事件 |
| `write(chunk) -> number` | 返回本次被底层接受的字节数；`0` 表示完全阻塞，之后须发 `'drain'`；`0 < n < chunk.length` 为部分接受 |
| `end()` | 向对端传递写结束（半关闭） |
| `pause()` / `resume()` | 源端流控 |

## 行为与交叉约束

1. **两个方向独立中继**：`aToB`（a 读 → b 写）与 `bToA`（b 读 → a 写）各持独立队列与上限，
   构造参数 `aToBLimit` / `bToALimit`（默认 65536，必须为正整数）。
2. **分方向背压**：某方向待写字节达到上限即 `pause()` 对应源端；目的端 `'drain'`
   排空到 0 后才 `resume()`。一个方向阻塞不影响另一方向继续转发。
3. **半关闭**：源端 `'end'` 到达后不立即转发，等该方向已接收字节**全部写出**，
   再调用目的端 `end()`。
4. **部分写入**：`write` 返回值小于块长时保留剩余字节继续尝试；返回 0 则等 `'drain'`；
   返回非法值或同步抛错按硬断开处理。
5. **硬断开**：任一端 `'error'`（含“正要恢复写入”时 drain 改报 error）立即结束整个中继、
   拆除全部监听，并在结算报告中给出**两个方向各自尚未写出的字节数**。
6. **可观测性**：每方向统计 `bytesRead / bytesWritten / peakBufferedBytes /
   bufferedBytes / unwrittenAtEnd` 等；`EventClock` 记录所有事件的全局序号。

### 结算报告

`relay.run()` 返回的 Promise  resolve 为：

```js
{
  clean: true | false,         // 两方向均干净结束为 true
  failure: null | { message, direction, error },
  directions: {
    aToB: { unwrittenAtEnd, peakBufferedBytes, bytesRead, bytesWritten, ... },
    bToA: { ... },
  },
}
```

硬断开同时触发 `'terminate'` 事件并携带同一份报告。

## 验收范围（7 个用例）

| 类别 | 用例 | 关键断言 |
| --- | --- | --- |
| 可接受结果 | 客户端先半关闭、服务端后回大段响应 | 两端内容逐字节一致；写结束次序 b 早于 a 且晚于最后一个字节；峰值不越界；发生过阻塞与 drain |
| 交错/隔离 | 单方向背压 | 阻塞方向源端被暂停，另一方向全程直达、不暂停 |
| 应被拒绝 | 恢复写入时硬断开 | `clean=false` 且定位方向；两边未写字节精确、满足 `written + unwritten = read`；监听全拆除；终止后不再转发 |
| 硬断开边界 | 无排队字节即时报错 | 未写计数为 0，仍以失败结算 |
| 边界 | `readStep=1` 峰值与半关闭等待 | 峰值精确等于上限；写结束必须在最后一次 drain 之后；交错方向独立峰值 |
| 记账 | 部分写入 | 观测到 `accept < wanted`，内容不丢不重、字节守恒 |
| 防御 | 非法 write 返回值 / 同步抛错 | 以失败结算而非失控 |

说明：待写缓冲在达到上限的当次读取可能有不超过 `readStep - 1` 的过冲
（一个已读出的块不可拆分回退），测试对过冲上界做断言；`readStep=1` 时峰值严格等于上限。
