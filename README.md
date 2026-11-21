# 双栈竞速拨号器（Dual-Stack Race Dialer）

纯 Node.js 标准库实现的 IPv6/IPv4 双栈竞速拨号器。时钟与拨号端均由调用者
注入，因此行为完全可确定性测试；不安装任何第三方依赖。

- 运行时要求：Node.js **24.15.0**（仅使用标准库与 ECMAScript modules）
- 模块体系：ESM（`"type": "module"`）
- 测试框架：内置 `node:test` + `node:assert/strict`

## 竞速规则（题面语义的实现映射）

1. **IPv6 起步、族间交替、一族用尽再取另一族**
   `AddressSelector` 维护两个有序数组的独立游标：两族都有候选时先取 IPv6，
   之后交替；某族耗尽后按输入顺序排空另一族。族内地址严格保持调用方给定顺序。
2. **首个地址立即拨号**：`raceDial` 同步返回前即发起第一次拨号（逻辑时刻 t0）。
3. **每隔 D 毫秒至多再开一个**：无赢家且仍有候选时，定时器按 `delayMs`
   重排；每个定时拍最多新增一路拨号。
4. **并发最多两路**：定时拍触发时若两个槽位均被占用，该拍跳过（不拨号），
   并从当前时刻重新计时。
5. **失败立即补位、定时重新计时**：某路失败腾出空位时，若还有候选则立即拨号，
   并以该起拨时刻为起点重新计时下一次定时拨号。
6. **同刻先结果后定时**：定时器回调先进入微任务队列，已返回的拨号结果
   （Promise 反应）在同一逻辑时刻先于定时动作被处理；所有状态转移由一把
   串行化锁（`locked` 链）保证原子且与时钟回调顺序无关。
7. **首个成功者胜出；同批成功取起拨较早者**：成功按起拨序号（sequence）
   比较，定稿（finalize）延后一个微任务，使同一批中晚到的更早起拨成功仍可
   接管。
8. **其余尝试一律取消；取消后才成功的连接必须关闭**：胜出后对其他在途尝试
   触发 `AbortController.abort()`；定稿之后才观察到的成功连接走
   `closeQuietly` 关闭，绝不返回给调用方。`close()` 抛错也被吞掉，不影响结果。
9. **全部失败可按起拨次序查看原因**：以 `AllAddressesFailedError` 拒绝，
   `failures` 数组按起拨序号排序，每项含 `sequence / address / family /
   dialedAt / error`。

## 目录结构

```
src/
  address-selector.js   # IPv6 优先、族间交替、单族排空的候选选择器
  race-dial.js          # 竞速拨号器主体 + AllAddressesFailedError
tests/
  address-selector.test.mjs   # 选择器单元测试（6 个）
  race-dial.test.mjs          # 拨号器正常/边界/交错场景测试（21 个）
  helpers/
    fake-clock.js             # 手动推进的确定性假时钟 + 微任务排空工具
    fake-dial.js              # 可控的假拨号器（延迟结算/忽略 abort 等）
```

## 公开 API

### `raceDial(options) -> Promise<DialResult>`

| 选项 | 类型 | 说明 |
| --- | --- | --- |
| `ipv6` | `string[]` | 有序 IPv6 候选，默认 `[]` |
| `ipv4` | `string[]` | 有序 IPv4 候选，默认 `[]` |
| `delayMs` | `number` | D，定时拨号间隔，非负有限数，必填 |
| `dial` | `(address, family, signal) => Promise<connection>` | 拨号函数，必填 |
| `clock` | `{ now, setTimeout, clearTimeout }` | 可注入时钟，缺省使用真实 `Date.now/setTimeout/clearTimeout` |

- `family` 取值为 `'ipv6' | 'ipv4'`；`signal` 是标准 `AbortSignal`，
  拨号方应在其 `abort` 后中止底层连接尝试。
- `connection` 约定提供 `close(): void | Promise`（用于关闭取消后才成功的
  连接）；缺失或抛错均不会影响竞速结果。

成功兑现值 `DialResult`：

```ts
{
  address: string,    // 胜出地址
  family: 'ipv6' | 'ipv4',
  connection: object, // dial 返回的连接对象（原样返回，未被关闭）
  dialedAt: number,   // 该路拨号发起时 clock.now() 的值
}
```

全部失败时以 `AllAddressesFailedError` 拒绝：

```ts
class AllAddressesFailedError extends Error {
  failures: Array<{
    sequence: number,                 // 起拨次序，从 0 开始
    address: string,
    family: 'ipv6' | 'ipv4',
    dialedAt: number,
    error: unknown,                   // dial 拒绝原因或同步抛出值
  }>;
}
```

参数非法（`delayMs` 为负/非有限值、`dial` 非函数、`clock` 接口不完整）时
同步抛出 `TypeError`；两个候选数组均为空时返回一个以
`AllAddressesFailedError`（`failures: []`）拒绝的 Promise。

### `new AddressSelector(ipv6, ipv4)`

- `next()`：返回 `{ address, family, index }` 或 `null`（耗尽）
- `hasNext()` / `remaining`：是否还有候选 / 剩余数量

## 复现步骤

```bash
node -v   # 应为 v24.15.0
node --test 'tests/**/*.test.mjs'
```

也可使用 `npm test`（脚本内容与上一条命令相同；本环境不执行 `npm install`，
没有任何依赖需要安装）。

## 验证范围

全部 27 个测试均由上述命令真实执行通过（2026-11-22，Node v24.15.0，
darwin/arm64），无跳过、无待办。场景分组：

### 正常场景（5）
- 首个地址 t0 立即拨号，未到 D 不开新拨号；首个成功即胜出
- 首个成功取消其余在途尝试并清空定时器，胜出连接不被关闭
- 无赢家时按 D 间隔至多新增一路；满槽定时拍跳过并重新计时
- 失败立即补位，且下一次定时从补位起拨时刻重新计时（含旧时刻不触发断言）
- 并发任何时刻不超过两路；失败替换立即发生

### 边界场景（12）
- 全部地址失败：拒绝通知乱序到达时，`failures` 仍按起拨次序排列
- 空候选列表立即以空 `failures` 拒绝，且不发起任何拨号
- 同批成功但反应乱序：起拨较早（序号较小）者胜出，被取代连接被关闭
- 取消后才成功的连接被关闭且绝不返回（使用忽略 abort 的自定义拨号模拟）
- 定稿之后的迟到拒绝不改变已兑现结果
- `delayMs = 0`：仍按"每拍一路、上限两路、失败立即补位"排空全部候选
- `dial` 同步抛错按单路失败处理并立即补位
- 同一逻辑时刻：先处理已返回结果再处理到期定时器（含失败与定时器同刻、
  成功与定时器同刻两个子情形）
- 非法参数同步抛 `TypeError`（负/NaN 间隔、非函数 dial、空 clock）
- 连接 `close()` 拒绝不影响竞速结果
- 不注入时钟时基于真实 `setTimeout` 的端到端冒烟测试

### 交错场景（4）
- 真实拨号序列遵循 IPv6→IPv4 交替，IPv4 耗尽后按序排空 IPv6
- 连续失败 + 两路上限下的交替选择与全量失败次序
- 仅 IPv6 / 仅 IPv4 输入按输入顺序消费
- IPv6 候选先后失败后由 IPv4 胜出，保留完整起拨元数据并取消其余尝试

### 设计约束与取舍
- **确定性**：假时钟只在显式 `advance()` 时推进；到期定时器在同一次推进内
  派发完毕（含派发中新产生的到期定时器），模拟真实定时器"延迟已过即触发"。
- **结果先于定时**：定时回调统一经微任务进入串行化锁，不依赖具体时钟实现
  的回调排列方式。
- **无网络、无外部依赖**：所有连接均为测试内合成对象，不访问真实网络目标、
  不读取凭据、不生成任何攻击/规避载荷。
