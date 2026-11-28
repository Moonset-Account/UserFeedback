# 跨版本分段字节流帧归并器

边缘采集器在固件切换期间需要继续读取同一路串口遥测。本模块面向**分段到达的
字节流**做帧归并：新旧两版帧头共享同前缀定界符，但校验和、对齐与可选字段不
同；版本标识可能由后续控制包才确认，因此归并器在确认前保留**多个可证实的边
界假设**，直到能唯一解帧再释放。拒绝的帧只做局部恢复，已发布帧永不回退。

仅使用 Python 3.11 标准库，无第三方依赖，不访问网络或真实设备，全部测试数据
均为本地合成。

## 目录结构

```
frame_merger/
  __init__.py    # 公开 API 再导出
  catalog.py     # 帧目录：定界符、版本规格、校验和、控制包解析、多版本评估
  codec.py       # 线格式编码辅助与跨版本同前缀样例构造
  merger.py      # 归并状态机：推迟确认、局部恢复、有限缓冲、flush 推断
tests/
  test_catalog_codec.py  # 校验向量、编解码往返、控制包、同前缀样例自证
  test_merger.py         # 已确认解析、分块等价、局部恢复、缓冲与 flush 边界
  test_deferred.py       # 推迟确认、多边界并存、跨版本交错专题
```

## 线格式与帧目录

数据帧两版本共享前导定界符 `AA 55` 与 1 字节长度字段：

| 版本 | 定界 | 校验和 | 可选字段 | 对齐 | 整帧长度（LEN=n） |
| --- | --- | --- | --- | --- | --- |
| V1 | `AA 55` | 8 位累加和 `sum8(LEN+PAYLOAD)` | 无 | 无 | `3+n+1` |
| V2 | `AA 55` | CRC-8（多项式 0x07，初值 0） | 尾部 2 字节小端时间戳 | 4 字节零填充 | 对齐到 4 |

版本由独立控制包宣布，使用不会与数据定界符混淆的标记 `C3 9C`：

```
C3 9C | VERSION(1) | sum8(VERSION)(1)
```

测点载荷两版本一致：每测点 3 字节 `POINT_ID(1) + VALUE(2, 大端有符号)`；
载荷长度不是 3 的倍数时，残段通过 `FrameEvent.payload_remainder` 与
`malformed_payload` 断续显式报告，不静默丢弃。

## 公开 API

### `frame_merger.catalog`

- `FrameCatalog.default()`：返回 V1/V2 默认目录。
- `FrameCatalog.evaluate_all(buf, offset)`：在同一标记处按所有版本尝试解帧，
  返回 `{version: ParseOutcome}`，用于保留多个可证实边界。
- `FrameCatalog.parse_control_packet(buf, offset)`：返回
  `(status, version, end_offset)`，`status` 为 `"ok""/"incomplete""/"rejected"`。
- `VersionSpec.try_parse(buf, offset, absolute=False)`：单版本尝试解析，返回
  `ParseOutcome(status, end_offset, measurements, timestamp, reject_reason,
  payload_remainder)`。
- `sum8(data)`、`crc8(data)`：两种校验和原语。
- 常量：`FRAME_MARKER = b"\xaa\x55"`、`CONTROL_MARKER = b"\xc3\x9c"`、
  `INCOMPLETE`、`REJECTED`。

### `frame_merger.merger`

- `FrameMerger(catalog=FrameCatalog.default(), max_buffer=4096)`
- `push(data) -> list[events]`：喂入任意长度、任意帧边界的字节段。
- `flush() -> list[events]`：声明流结束，处理尾部并报告残余断续。
- `reset()`：清空全部状态（含已发布序号）。
- 只读属性：`confirmed_version`、`buffered_bytes`、`pending_hypotheses`
  （未确认期保留的 `{标记偏移: {版本: 帧尾偏移}}`，深拷贝）。

事件类型（均为不可变 dataclass）：

- `FrameEvent(sequence, version, measurements, timestamp,
  payload_remainder, deferred)`：发布即终态，序号单调递增、永不回退。
- `VersionEvent(version, previous, deferred)`：版本（重新）确认。
- `GapEvent(reason, bytes_skipped, detail)`：断续。

断续原因（`DiscontinuityReason`）：

| 常量 | 触发场景 |
| --- | --- |
| `resync_skipped` | 定界符前噪声、坏控制包、坏帧局部恢复、无主尾部字节 |
| `checksum_mismatch` | 已确认模式下帧校验失败（恢复点优先取目录声明帧尾） |
| `malformed_payload` | 帧有效但测点载荷存在凑不满 3 字节的残段 |
| `buffer_overflow` | 未确认积压超过 `max_buffer`，丢弃早期未发布字节 |
| `truncated` | flush 时尾部字节不足以成帧 |
| `truncated_ambiguous` | flush 时多版本边界仍并存，拒绝猜测 |
| `flush_inferred` | flush 时仅单版本证据，按推断版本释放（区别于控制包确认） |

### `frame_merger.codec`

- `encode_measurement(point_id, value)`、`encode_frame(spec, payload,
  timestamp=0)`、`build_control_packet(version)`：合成线格式。
- `build_ambiguous_v1_v2(catalog, point_id=0x10, value=-250)`：构造**跨版本
  同前缀样例**，同一字节串同时通过 V1 与 V2 解析（V1 帧尾 7 字节、V2 帧尾
  12 字节），构造后带自证断言。

## 关键语义

1. **推迟确认**：未确认期不在任何候选边界上提交输出；每个数据定界符都按目录
   评估全部版本，通过形式约束的边界累积进 `pending_hypotheses`。候选字节整体
   保留（只移动指纹守卫、不删除数据），控制包到达后对整个缓冲按唯一版本重新
   解析并一次性释放，相关 `FrameEvent.deferred=True`。
2. **重评估正确性**：长版本帧更短版本更长，逐字节喂入时常出现“短版本已成帧、
   长版本仍缺字节”的中间态。归并器以“标记到缓冲末尾”的字节指纹做守卫：同一
   轮排空内指纹不变则跳过防死循环，新字节到达使指纹变化后自动重新评估，保证
   两个版本的边界最终都能登记。
3. **局部恢复**：校验/对齐失败时，若目录声明帧尾仍在缓冲内则按声明长度恢复
   （分块与整段喂入恢复点一致）；声明帧尾超出缓冲才退化为定界符扫描；两者都
   不可证实则等待更多字节，绝不提前吞掉下一帧开头。
4. **有限缓冲不阻塞恢复**：`max_buffer` 只约束未确认积压；完整控制包优先于容
   量兜底解析，保证超限积压后链路仍可被迟到的控制包恢复。缓冲中只存放未发布
   字节，因此已发布帧在结构上不可能被回退。
5. **flush 不猜测**：多版本证据并存时发 `truncated_ambiguous`；仅单版本证据时
   推断释放并显式发 `flush_inferred` 标记。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围

全部基于本地合成数据，50 个测试用例覆盖：

- **正常场景**：V1/V2 单帧与多帧、测点负值与边界值、V2 时间戳与 4 字节对齐、
  载荷残段报告、前导噪声跳过、定界符字节内嵌载荷不误重同步。
- **分段/交错**：1/2/3/5/7/13/64 字节等多种分片与整段喂入结果等价；控制包跨
  分片到达；帧跨确认边界（前缀在确认前、帧尾在确认后）；20 组固定随机种子的
  随机分片含坏帧对照。
- **推迟确认（题面指定的跨版本同前缀样例）**：确认前两个可证实边界并存且零
  输出；逐字节分片后分别由 V1/V2 控制包确认，释放为对应版本（V1 无时间戳、
  V2 带时间戳）；多帧双版本积压一次性释放；V1 确认后 V2 专属尾部按
  `resync_skipped` 记账；deferred 标记在后续常规流量上自动复位。
- **局部恢复**：校验失败/非零填充/坏控制包/未知版本控制包均只影响局部，相邻
  已发布帧的序号与载荷保持不变。
- **缓冲边界**：未确认溢出只丢最早未发布字节并保留最后定界符；溢出后标记与
  完整帧幸存并可被控制包恢复；已确认大流（200 帧、`max_buffer=32`）不误触发
  溢出；flush 的 `truncated`、`truncated_ambiguous`、`flush_inferred` 三种
  尾部路径及 flush 后归并器复用。
- **校验向量**：CRC-8 对标准串 `123456789` 输出 `0xF4`；sum8 回绕；逐字节
  CRC 与整段计算一致。

## 范围外说明

- 本模块只实现题面规定的防御性帧判定与恢复，不包含任何利用、入侵或规避载荷，
  也不包含真实串口/网络 IO。
- 未实现自定义版本目录的线格式协商扩展点之外的协议特性（如重传请求、带内流
  控）；`FrameCatalog` 可通过传入自定义 `VersionSpec` 字典扩展新版本。
