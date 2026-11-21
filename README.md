# 权威 DNS 应答线格式编码器

把权威 DNS 应答编码进调用方给定的**字节预算**。仅使用 Rust 1.95.0 与标准库，
无任何第三方依赖，支持完全离线构建与测试。

## 功能与约束

- 输入：查询 ID、一条可完整放入预算的问题、必须回答的**有序记录组**（Answer 区）
  与可选附加记录组（Additional 区）。
- 名字为合法小写 ASCII 域名（空字符串表示根名）；记录仅含 `A` 与目标为域名的
  `CNAME`；类别均为 `IN`。
- 按 RFC 1035 线格式写应答头、问题与资源记录：
  - 头初始只置 **QR**（`0x8000`）与 **AA**（`0x0400`）位，即 `0x8400`；
  - `QDCOUNT=1`、`NSCOUNT=0`，`ANCOUNT/ARCOUNT` 由实际提交的记录数解出。
- 名称压缩：
  - 尽量复用此前已写出名字的**最长后缀**；
  - 等长后缀取**最早可编码位置**（最小绝对偏移）；
  - 仅当确实能省字节（逐字成本严格大于 2 字节指针）才发指针；
  - 所有指针目标都在 14 位范围（`0..=0x3FFF`）内且向后指向。
- 字节预算与组语义：
  - 名称/记录先在临时缓冲完整编码，整块能容纳才原子提交，不留半截内容；
  - 必需组整体放不下 → 整组撤回、置 **TC**（`0x0002`）并立即结束，后续必需组不再尝试；
  - 附加组整体放不下 → 整组跳过，继续尝试后续附加组；
  - 后面的组可使用恰好剩下的空间；返回长度即最终字节数，无填充。
- 返回的计数、长度与名称指针全部可从最终字节用通用 DNS 解析逻辑解出。

## 公开 API

入口函数：

```rust
pub fn encode_authoritative_response(
    query_id: u16,
    question: &Question,
    required: &[RecordGroup],
    additional: &[RecordGroup],
    budget: usize,
) -> Result<EncodedResponse, EncodeError>
```

配套公开类型（均在 crate 根下）：

| 类型 | 说明 |
| --- | --- |
| `Question::new(qname, QueryType)` | 问题；`QueryType::{A, Cname}`，类别固定 IN |
| `ResourceRecord::new(name, ttl, Rdata)` | 资源记录，类别固定 IN |
| `Rdata::A(Ipv4Addr)` | A 记录 RDATA（4 字节地址） |
| `Rdata::Cname(String)` | CNAME 记录 RDATA（目标域名） |
| `RecordGroup::new(Vec<ResourceRecord>)` | 有序记录组，提交/撤回的最小单位 |
| `EncodedResponse` | 含 `bytes`、`truncated`、`question_count`、`answer_count`、`name_server_count`、`additional_count`，并提供 `len()/is_empty()/as_bytes()/is_truncated()` |
| `EncodeError` | `NameInvalid` / `BufferTooSmall { needed, budget }` / `CountOverflow` |

常量：`CLASS_IN=1`、`TYPE_A=1`、`TYPE_CNAME=5`。

### 最小用法

```rust
use dns_authoritative_encoder::*;
use std::net::Ipv4Addr;

let question = Question::new("host.example.com", QueryType::A);
let required = vec![RecordGroup::new(vec![ResourceRecord::new(
    "host.example.com",
    300,
    Rdata::A(Ipv4Addr::new(192, 0, 2, 5)),
)])];

let resp = encode_authoritative_response(0x1234, &question, &required, &[], 512)?;
assert_eq!(resp.answer_count, 1);
// resp.bytes 即完整线格式应答；应答名以双字节指针 C0 0C 复用问题名。
```

## 目录结构

```
Cargo.toml              # rust-version = "1.95.0"，无 [dependencies]
src/lib.rs              # 编码器实现 + 10 个单元测试（压缩字典/域名校验）
tests/wire_format.rs    # 14 个端到端测试，内含独立线格式解析器做真实解码校验
```

## 复现步骤

环境已安装 Rust/Cargo 1.95.0。在本目录执行：

```bash
cargo test --offline
```

离线模式不访问网络、不读取缓存索引以外的内容；本 crate 无依赖，因此可在空网络下构建。

## 验证范围（均以真实编码→真实解码为准）

集成测试自带一个独立的 DNS 线格式解析器（处理字面标签与 `11xxxxxx` 压缩指针、
校验指针向后、按 RDLENGTH 解析 A/CNAME RDATA），所有断言都作用在编码器的真实输出上，
不使用任何预置演示字节。覆盖场景：

正常场景
- A 应答整包往返：ID、标志位、四个计数、问题、类型/类/TTL/RDLENGTH/地址。
- 应答名以双字节指针复用问题名，并核对精确字节长度。
- CNAME 链：CNAME 目标与后续 A 记录逐级复用后缀，RDATA 长度与指针偏移逐项核对。
- 必需组与附加组跨区压缩，全部指针解析回正确名字。

边界场景
- 预算恰好等于报文长度时精确填满、无填充；少 1 字节时必需组整组撤回并置 TC，
  长度停在问题结束处。
- 预算小于 12 字节头、或放不下问题时返回 `BufferTooSmall { needed, budget }`。
- 根名（空字符串）编码为单个零标签；空记录组正常计数为 0。
- 压缩指针目标超出 14 位范围时该字典条目不可用，回退逐字编码。
- 非法域名（大写、空标签、前后导点、下划线、空格、超长标签/超长名字）被拒绝。

交错/组语义场景
- 最长后缀优先于较短字典条目；等长后缀选择最早可编码位置。
- 无共享后缀时完整逐字且不产生任何指针字节。
- 必需组失败会回滚并阻止后续必需组；附加组失败只跳过该组，后续组继续提交。
- 单标签后缀在能省至少 1 字节时仍使用指针。

单元测试（`src/lib.rs`）额外直接覆盖压缩字典的匹配/取舍规则与域名合法性判定。

## 未覆盖与限制

- 仅支持题面要求的 IN 类、A 与 CNAME 记录；不实现 NS/SOA/MX/OPT/EDNS、UDP/TCP 收发、
  重传或真实网络交互。
- 不做 DNSSEC、大小写保留（输入约定为小写）与国际化域名（IDN）处理。
- 所有测试均在本地用合成域名与合成地址运行，不访问任何真实网络目标。
