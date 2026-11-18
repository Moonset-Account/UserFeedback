# 网关准入模块（gateway-admission）

普通表单与文档上传共用的请求流准入模块：按路由声明同时跟踪**上传字节**与**解码输出字节**，文档先流入待定区，整份请求读完并通过限额后才对下游可见。

## 实际入口

- 源码：`src/admission.js`，导出 `createAdmissionGateway({ routes })`、`AdmissionError`、`CODES`。
- 测试：`tests/admission.test.mjs`（`node:test`，全部真实断言）。
- 运行测试（Node.js 24.15.0）：

  ```sh
  node --test "tests/**/*.test.mjs"
  ```

  或 `npm test`（`package.json` 中同一命令；本环境未批准 npm 调用，已直接执行上述 node 命令验证，7/7 通过）。

## 用法概要

```js
import { createAdmissionGateway } from './src/admission.js';

const gw = createAdmissionGateway({
  routes: {
    doc: {
      maxUploadBytes: 16,     // 原始请求流字节上限
      maxDecodedBytes: 32,    // 解码输出字节上限
      decoder: () => myDecoder,          // 可替换解码器工厂，每会话独立实例
      sink: { async write(id, buffers) { /* 下游写入，可选 */ } },
    },
  },
});

const s = gw.admit('doc', 'request-id');
s.write(chunk);          // 分块写入；上传/解码字节同步记账，超限即抛 AdmissionError
await s.end();           // finalize 解码 → 写下游 → 全部通过才 commit 为可见
// 或 s.abort(reason);   // 客户端取消：清理待定状态
```

- `gw.getVisible(requestId)`：仅 commit 后返回文档；待定期间一律 `undefined`。
- `gw.isPending(requestId)`：待定区观测。
- 失败（超限 / 解码错误 / 下游写失败 / abort）后，同一 `requestId` 可重新 `admit` 重试；在途中的相同 `requestId` 会被 `DUPLICATE_REQUEST` 拒绝。
- 每个会话独立记账，并行请求互不占预算。

## 验证范围（tests/admission.test.mjs）

| 用例 | 覆盖的交叉约束 |
| --- | --- |
| 共用入口 + 待定区可见性 | 表单与文档走同一 `admit` 入口；`end` 前 `getVisible` 为 `undefined`，commit 后可见，字节统计正确 |
| 上传字节超限 | 超过 `maxUploadBytes` 即抛 `UPLOAD_LIMIT`，待定状态被清理 |
| 解码晚些超限 | `finalize` 阶段才超 `maxDecodedBytes`，抛 `DECODED_LIMIT`，清理待定区并调用解码器 `destroy()` 释放资源 |
| 限额边界 | 恰好等于限额通过；多一字节被拒 |
| 客户端取消 + 重试 | `abort` 清理待定状态，会话关闭后写入抛 `SESSION_CLOSED`；同一 `requestId` 重试成功 |
| 下游写失败 + 并行隔离 | sink 写失败抛 `DOWNSTREAM_FAILURE`（保留 `cause`）并清理待定；失败请求可重试，并行请求的预算与提交不受影响 |
| 在途冲突 / 未知路由 | `DUPLICATE_REQUEST`、`UNKNOWN_ROUTE` |

未覆盖：真实网络 socket、真实压缩解码器（测试使用本地合成的直通/展开解码器夹具）。
