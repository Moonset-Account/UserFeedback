# Pipelined HTTP/1.1 Response Reader

一个可增量喂字节的 HTTP/1.1 响应读取器。调用方先在同一条 keep-alive
连接上登记按顺序发出的请求方法（HEAD / GET），随后把到达的响应字节任意
切分后喂给读取器。读取器在**最终响应**完整时通过回调返回结构化结果；
连接提前关闭时，已完成的响应全部保留，并通过 `unfinished` 指出未完成的
是哪一个登记请求。

仅使用 Node.js 标准库（`node:buffer`）与 ECMAScript modules，无第三方
依赖，要求 Node.js 24.15.0。

## 目录结构

```
src/http-response-reader.mjs   读取器实现（单文件，纯标准库）
tests/http-response-reader.test.mjs  正常 / 边界 / 交错场景测试
tests/fuzz.test.mjs                  确定性随机切分与随机报文模糊测试
package.json
```

## 公开 API

### `new HttpResponseReader(options?)`

- `options.onResponse(response)`：每个**最终响应**完成时按请求顺序调用一次。
- `options.onInterim(response)`：每个 1xx（100 / 103）临时响应到达时调用；
  临时响应**不消耗**请求，请求下标保持不变。

### `reader.registerRequest(method) -> number`

在响应字节到达前登记请求方法（内部统一大写化），返回请求下标。
`end()` 之后再登记会抛错。

### `reader.push(chunk)`

喂入 `Buffer` / `Uint8Array` 字节。可在任意位置切分：状态行、头部行、
十六进制 chunk 长度行、CRLF 行尾、chunk 数据中间均可跨多次喂入。
`end()` 之后调用会抛错；空 chunk 被忽略。

### `reader.end()`

声明连接结束。流干净结束（所有登记请求都已拿到完整最终响应且无残留字节）
时正常返回；存在未完成响应时抛出带请求下标的错误，已完成的响应不受影响。

### `reader.unfinished -> { index, method } | null`

提前关闭后指向未完成的登记请求：

- 最终响应已开始（状态行已解析）但未完成：指向该请求；
- 一个响应字节都没收到：指向下一个待处理的登记请求；
- 干净关闭：`null`。

### 其他只读属性

- `reader.requests`：登记方法的副本数组。
- `reader.completedCount`：已完成的最终响应数量（即已消耗的请求数）。

### `findHeader(headers, name) -> string | undefined`（具名导出）

对 `[name, value]` 形式的头部数组做大小写不敏感查找，返回第一个匹配值；
响应头与 chunked 尾字段都可使用。

### 响应对象结构

```ts
{
  requestIndex: number,            // 对应登记请求的下标
  method: string,                  // 登记的请求方法（大写）
  statusCode: number,              // 如 200 / 204 / 304
  reasonPhrase: string,            // 原因短语，可为空字符串
  headers: Array<[string, string]>,// 保留原始大小写与重复头部
  body: Uint8Array,                // 解码后的正文（定长或 chunked）
  trailers: Array<[string, string]>,// 仅 chunked：尾字段，与头部分开
  interim: Response[],             // 属于本请求的 1xx 临时响应列表
}
```

## 关键语义

1. **临时响应（100 / 103）**：通过 `onInterim` 交给当前请求，请求不被
   消耗；同一请求的多个临时响应累计在最终结果的 `interim` 字段中。
2. **HEAD / 204 / 304**：在头部区块结束的空 CRLF 处立即完成，即使携带
   正的 `Content-Length` 也不读取任何正文字节，后续响应不会错位。
3. **定长正文**：按 `Content-Length` 精确读取，`0` 长度在头部结束即完成。
4. **chunked 正文**：解析十六进制长度（容忍 `;` 后的 chunk 扩展），拼接
   各 chunk 数据作为 `body`，尾字段单独放入 `trailers`，不混入 `headers`。
5. **优先级**：存在 `Transfer-Encoding: chunked` 时按 chunked 处理，
   否则按 `Content-Length` 处理；两者都没有时按空正文处理（输入均为
   合法报文，不会出现需要靠连接关闭界定正文的场景）。
6. **提前关闭**：`end()` 抛错指明未完成请求（下标 + 方法），之前通过
   `onResponse` 交付的响应全部保留。

## 复现步骤

```bash
node --version            # 期望 v24.15.0
node --test "tests/*.test.mjs"
```

（`package.json` 中 `npm test` 为同一命令；本环境的权限策略拦截了
`npm`，故验证使用等价的 `node --test` 直接运行。）

## 验证范围

全部 28 个 `node:test` 用例均实际执行并通过（无跳过），覆盖：

- 正常路径：定长 GET、chunked GET（含多 chunk、尾字段、chunk 扩展、
  空 chunked 正文）、HEAD、204、304；
- 临时响应：100 单临时响应、多个 103 后接 chunked 最终响应，验证请求
  不被消耗且临时响应挂接到最终结果；
- 交错流水线：HEAD→GET→GET 混合定长/chunked 成帧，验证响应与请求严格
  对齐、回调顺序与请求下标一致；
- 切分边界：整包喂入、逐字节喂入、确定性随机切分（1–7 / 1–9 字节），
  以及针对状态行、CRLF、十六进制长度行的定点切断；
- 头部边界：重复头部保留、大小写不敏感查找、OWS 空白裁剪、原因短语可缺省；
- 提前关闭：定长正文截断、头部截断、chunked 正文截断、零字节到达、
  临时响应截断——均验证已完成响应保留且 `unfinished` 指向正确请求；
- 用法错误：重复 `end()`、`end()` 后 `push()` / 登记、响应多于登记请求、
  畸形状态行、畸形 chunk 长度。
- 模糊测试：固定种子生成 60 条随机合法报文流（随机方法、状态码、成帧、
  0–2 个临时响应、可选尾字段），随机切分喂入并与参考模型逐字段比对；
  另有 25 组流在整包 / 随机切片 / 逐字节三种喂法下做结果等价性比对。

## 范围外与假设

- 输入限定为题面所述的合法报文；畸形输入会尽早抛错，不做宽容恢复。
- 不做 HTTP/1.0 无长度靠连接关闭界定正文的处理（题面输入不包含该形式）。
- 不处理 `Transfer-Encoding` 的非 chunked 组合编码（如 gzip），仅识别
  令牌列表中的 `chunked`。
- 不发起任何网络请求、不读取真实账户或凭据；所有报文均由测试代码本地
  合成，头部与正文使用可打印的本地生成字节。
- 回调为同步调用，不要在回调中再次 `push()`（非重入设计）。
