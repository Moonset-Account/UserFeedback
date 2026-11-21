# ipfrag — IPv4 按出口 MTU 分片模型

纯标准库实现的 IPv4 转发分片模块（Go 1.20）。输入是一份合法数据报或中间分片的首部、载荷和出口 MTU，输出为按序的一片或多片子报。

## 行为规则

- **放得下**：`首部 + 载荷 ≤ MTU` 时原样返回单个数据报（不改动任何字段）。
- **禁止分片**：超过 MTU 且 DF 置位时，返回 `*NeedsFragmentationError`，其中携带出口 MTU 供调用方回报。
- **允许拆分**：
  - 子片首部随各自保留的选项重新对齐到 4 字节（EOL 填充）。
  - 原报文偏移为零的首片保留所有选项；其余子片只带可复制（copied 位）选项。
  - 按各子片实际首部长度尽量装载；除原报文最后一片外，每片载荷为 8 的倍数。
  - 子片偏移接着输入片在原报文中的位置累加；最后一个子片继承输入的 MF 标志。
  - 每片首部的 `Checksum` 字段已填好，可用 `ValidChecksum` 对线格式验证。
- **MTU 太小**：连一个合法分片（首部 + 至少一个 8 字节载荷块，或首部本身）都装不下时，整次调用失败，返回 `*MTUTooSmallError`。
- **输入校验**：`TotalLength` 与首部+载荷不一致、非末片载荷非 8 的倍数、未知标志位、偏移越界等均报错，不产生输出。

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `Fragment(h *Header, payload []byte, mtu int) ([]Datagram, error)` | 核心分片入口 |
| `Header` / `Datagram` / `Option` | 首部、数据报、选项模型；`FragmentOffset` 以 8 字节为单位 |
| `FlagDF` / `FlagMF` | 首部标志位常量 |
| `Option.Copied() bool` | 选项是否带可复制标志 |
| `HeaderLen(opts []Option) (int, error)` | 选项对齐到 4 字节后的首部长度 |
| `(*Header).Marshal() ([]byte, error)` | 序列化首部并计算校验和 |
| `ValidChecksum(b []byte) bool` | 验证序列化首部的校验和 |
| `NeedsFragmentationError` / `MTUTooSmallError` | 两类错误，均可用 `errors.As` 判定；前者携带出口 MTU |

## 复现步骤

```sh
go test ./...        # 运行全部功能测试
go test ./... -v     # 查看逐条用例
go vet ./...         # 静态检查（可选）
```

环境：Go 1.20.2，无第三方依赖，全部数据为本地合成。

## 验证范围（`ipfrag_test.go`，全部真实断言）

- **正常**：`TestBasicFragmentation`（片长/偏移/MF/ID/载荷重组）、`TestOptionCopyRules`（首片全选项、后续仅可复制选项、4 字节对齐、按实际首部分配载荷）、`TestChecksumFieldMatchesMarshal`（逐片校验和可验证且与字段一致）。
- **边界**：`TestFitsReturnedAsIs`（放得下原样返回）、`TestExactFitBoundary`（恰好等于 MTU 与超出 1 字节）、`TestDFSetReturnsMTU`（DF 置位回报出口 MTU）、`TestMTUTooSmall`（首部长于 MTU / 载荷容量为 0 / 不足一个 8 字节块）、`TestEmptyPayloadHeaderExceedsMTU`、`TestInvalidInputRejected`。
- **交错**：`TestIntermediateFragment`（输入为中间分片：子片偏移续接、全部只带可复制选项、末子片继承 MF=1）、`TestLastChildInheritsClearedMF`（输入为末片时末子片清 MF）。

未执行的性能/模糊测试不在本次验证范围内；以上结果以 `go test ./...` 实际输出为准。
