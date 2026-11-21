# cri-log-decoder

容器运行时（CRI 风格）日志片段重组解码组件，供采集器调用。

运行时会把一行日志拆成多条带 `P`（partial）标记的字节片段，最后一条标 `F`（full）。
本组件把片段重新拼成逻辑行，并处理跨卷、双流交错、UTF-8 切分、断点恢复去重与
实例重启等交叉约束。

## 实际入口

- 组件源码：`src/log-decoder.mjs`（`package.json` 的 `main`/`exports` 指向它）
  - `new LogDecoder()`：新建解码器。
  - `decoder.ingest(record)`：摄入一条记录，返回本次产出的行数组。
  - `decoder.snapshot()` / `LogDecoder.restore(snapshot)`：导出/恢复进度
    （JSON 安全，可直接 `JSON.stringify` 落盘）。
  - `decoder.flushAll()`：停止时冲刷所有未完成半行为残缺记录。
  - `LogDecodeError`：非法输入异常类型，带 `code`。
- 测试入口：`npm test`（即 `node --test "tests/*.test.mjs"`，Node.js 24.15.0，
  全部使用 `node:test` + `node:assert` 真实断言）。

## 输入记录模型

```js
{
  container: 'web',        // 逻辑容器名
  instance:  'i-1',        // 实例代号（重启后更换）
  stream:    'stdout',     // 或 'stderr'
  volume:    0,            // 卷序号（按序进入）
  recordNo:  0,            // 卷内记录号（卷内全局编号，双流共享序列）
  tag:       'P',          // 'P' 片段 / 'F' 收尾
  bytes:     Uint8Array,   // 原始字节
}
```

## 产出行模型

```js
{
  container, instance, stream,
  text:    'hello 中 world',          // 解码后的行文本（剥掉末尾 \n/\r\n）
  partial: false,                     // true 表示残缺记录（实例重启/停止冲刷）
  start:   { volume, recordNo },      // 该行覆盖的首条记录位置
  end:     { volume, recordNo },      // 该行覆盖的末条记录位置
}
```

下游可从每条产出行的 `start`/`end` 查到它覆盖的首末记录位置。

## 行为约定（验证范围）

`tests/log-decoder.test.mjs` 覆盖以下交叉约束：

1. **可接受结果**：一行被拆成多条 `P` 片段并跨卷（卷 0 → 卷 1）拼行；多字节
   UTF-8 字符（「中」）被切到两条片段中仍正确解码；`stdout`/`stderr` 在同一卷内
   交错进入、各自独立成行；产出行的首末记录位置正确。
2. **应被阻止/拒绝的结果**：同卷记录号跳跃、卷序号跳跃抛
   `ERR_POSITION_GAP`；非法标记抛 `ERR_BAD_TAG`；非法流名抛 `ERR_BAD_STREAM`；
   被拒绝的记录不污染进度，合法后继仍可被接受。
3. **边界/交错场景**：半行保存快照 → JSON 往返恢复 → 上游重送已保存记录被
   静默去重 → 半行补完且只产出一次（收尾记录再重送也不二次产出）→ 恢复后可
   再次快照继续。同一容器换实例时，先把旧实例半行作为 `partial: true` 的残缺
   记录输出，再处理新实例首行，新实例位置序列重新计数。`flushAll()` 在停止时
   冲刷两条流各自的未完成半行，且每行末位置按本流最后一条片段计算。

## 位置与去重模型

- 卷序号 + 卷内记录号在**容器实例**维度全局有序（同卷内两条流共享编号序列），
  顺序校验与重送去重挂在实例级，拼行缓冲挂在流级。
- 已消费位置之前的重送记录静默跳过（幂等）；严格后继（同卷 `recordNo+1` 或
  下一卷 `recordNo 0`）之外的记录视为位置跳跃，拒绝并抛错。
