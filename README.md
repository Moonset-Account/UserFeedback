# auditlog — 取证镜像二进制审计日志提取库

从设备取证镜像的分片中重组带边界标记、长度、序列号和校验值的二进制审计记录，
输出校验有效记录的原始字节区间，并对受损区间与冲突记录留下显式证据。

## 记录格式（调用方提供）

```
Magic | Length | Seq | Payload | Checksum
```

- `Magic`：记录起始边界标记（`Format.Magic`）。
- `Length`：载荷长度，大端序，`LengthSize` 字节宽（1/2/4/8）。
- `Seq`：记录序列号，大端序，`SeqSize` 字节宽。
- `Checksum`：`Checksum` 函数对 `Length‖Seq‖Payload` 计算结果的低
  `ChecksumSize` 字节（大端序）。库内置 `CRC32Checksum`（CRC-32/IEEE）。
- `MaxPayload`：长度字段的合理性上限，超过即视为受损记录。

## 实际入口

```go
res, err := auditlog.Extract(shards []auditlog.Shard, f auditlog.Format)
```

- 分片按调用方给定顺序拼接为逻辑流读取，**记录可跨分片**；每条有效记录通过
  `Record.Segments` 给出逐分片的原始字节区间（`ShardID` + `Offset` + `Length`），
  并附逻辑流中的 `Offset`/`Length`。
- **受损恢复**：长度字段受损（超出 `MaxPayload`、记录越界）或校验不成立时，
  该记录被拒绝，扫描前进到下一处校验成立的记录恢复读取；残缺区间以
  `Result.Gaps`（`Gap.Offset/Length/Segments/Reason`）保留证据位置。
  全零填充区不计为证据。
- **冲突**：重叠分片中同序列号但内容不同的有效记录**分别保留**，置
  `Record.Conflict = true` 并赋予相同的 `Record.ConflictGroup`；
  内容完全一致的重复记录保留但不标记冲突。

## 验证范围

`extract_test.go` 使用 Go 内置 `testing` 编写验收用例，全部为真实断言：

| 用例 | 覆盖点 |
| --- | --- |
| `TestExtractValidRecordsAcrossShards` | 可接受结果：跨分片记录重组、逐分片字节区间精确断言、无间隙 |
| `TestCorruptLengthRecovery` | 应被拒绝的结果：长度字段受损记录被拒、在下一有效记录处恢复、残缺区间证据位置 |
| `TestConflictingSequences` | 重叠分片同序列不同内容：两条均保留并标记冲突、同冲突组 |
| `TestIdenticalDuplicatesNotConflict` | 边界语义：内容一致的重复不标记冲突 |
| `TestInterleavedDamageBoundaryAndConflict` | 交错场景：受损记录夹在有效记录之间 + 记录在校验字段内跨分片 + 冲突副本，三者同时断言 |
| `TestInvalidFormatRejected` | 非法调用方格式（空标记、非法字段宽度）被拒绝 |

运行验证：

```sh
go test ./...
```

## 范围说明

本库仅做本地防御性解析：读取调用方提供的分片字节，不做任何目标探测、
不联网、不写入工作区之外的位置。
