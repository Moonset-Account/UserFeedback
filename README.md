# tcprx — 已建立 TCP 连接的接收重组模块

## 功能

实现单方向 TCP 接收端的序号重组逻辑：

- 构造时由调用者给出**首个待收序号**和小于半个序号空间（< 2^31）的**固定接收窗**；
  全部序号比较采用模 2^32 的序列号算术，天然支持回绕。
- 输入片段 `Segment{Seq, Data, FIN}`：32 位起始序号、字节串、可选 FIN；
  重传的重叠字节内容一致。
- 落在接收窗 `[next, next+win)` 之外的部分被裁去（含窗外的 FIN 标志）。
- 连续前缀一旦补齐即交付上层；乱序片段在窗内缓冲。
- FIN 只有在排在它前面的字节全部交出后才生效，并消耗一个序号；
  流结束后（`Ended()`）后续输入一律忽略，结束事件只发生一次。
- 每次 `Accept` 输出 `Output{Data, Ack, Fin}`：新交出的字节、
  累计确认号（下一期望序号）、以及本次是否消费了 FIN。

## 实际入口

- 源码：`reasm.go`（包 `tcprx`）
  - `tcprx.New(firstSeq uint32, window uint32) *Reassembler`
  - `(*Reassembler).Accept(seg Segment) Output`
  - `(*Reassembler).Ended() bool` / `(*Reassembler).Ack() uint32`
- 测试：`reasm_test.go`，使用 Go 内置 `testing`，全部真实断言。
- 验证命令：`go test ./...`（Go 1.20.2 实测通过；`go vet` 本次未获执行权限）。

## 验证范围（交叉约束覆盖）

| 用例 | 覆盖点 |
| --- | --- |
| `TestInOrderDeliveryAccepted` | 可接受结果：按序交付、ACK 前进、FIN 消耗一个序号 |
| `TestOutOfWindowRejected` | 应被阻止：窗外（左/右）片段丢弃、越窗部分裁剪、窗外 FIN 不生效 |
| `TestWrapAroundFinArrivesBeforeFirstHalf` | 边界/交错：期望序号 = 0xFFFFFFFE（回绕点前两字节），带 FIN 的后半段先到只缓冲不结束，补前半段后交付 `abcd`、ACK 越过零点变为 3、FIN 只结束一次，后续重传被忽略 |
| `TestFinWaitsForPrecedingBytes` | FIN 先于前序字节到达时挂起，缺口补齐后才生效 |
| `TestOverlappingRetransmission` | 乱序缓冲 + 内容一致的重叠重传拼出连续前缀 |
| `TestEmptySegmentIsNoOp` | 空片段不改变状态 |

## 范围说明

纯本地基础设施组件：仅处理合成输入，无网络连接、无外部目标、
无任何探测/攻击行为；所有产物保留在本目录。
