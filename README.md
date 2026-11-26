# dnssecpreflight — DNSSEC 离线换签预检器

一个无第三方依赖的 Go 1.20 库，用来在更换 DNSSEC 签名算法前，按 32 位 RRSIG
时间轴找出每种解析器策略、每个阶段、每个 RRset 的最早验证空档，并指出缺少的
具体算法/密钥签名能力。

本库不执行网络访问、不读取真实凭据、不安装依赖，也不内置“信任即通过”的加密
判断。签名是否成立完全由调用方提供的 `Verifier` 回调核对；测试使用本地合成数据
和 Go 标准库的 Ed25519 验证。

## 公开 API

核心入口：

```go
func dnssecpreflight.Preflight(req Request) (*Report, error)
```

主要输入类型：

- `Request`：RRset、DNSKEY、RRSIG、解析器策略、验证回调、32 位参考时间和可选分析窗口。
- `RRSet{ID, Canonical}`：ID 用于关联 RRSIG，`Canonical` 是已规范化的 RRset 字节。
- `DNSKEY{ID, Tag, Algorithm, PublicKey, ...}`：公钥候选；`ID` 是策略信任标识，留空时按输入顺序生成 `key-N`。
- `RRSIG{ID, RRSetID, Algorithm, KeyTag, Inception, Expiration, Signature, ...}`。
- `Verifier func(VerifyInput) error`：外部签名核对回调；`nil` 错误表示验签成功。
- `BoolVerifier(func(key DNSKEY, signedData, signature []byte) bool)`：布尔回调适配器。
- `Resolver{Name, Stages}` 与 `Stage{Name, Trust, Requirement}`。
- `TrustGrant{KeyID, PublicKey, Algorithm}`：受信任的“密钥 + 算法”组合；`KeyID: "*"` 可显式允许任意匹配密钥。
- `Requirement`：
  - `AnyAlgorithmRequirement(algs...)`：择一有效签名即可；不传算法表示任意受信任能力。
  - `AllAlgorithmsRequirement(algs...)`：每个指定算法都必须有一份当前有效且受信任的签名。

主要输出类型：

- `Report.Pairs`：每个“解析器 × RRset”的报告。
- `Report.Checks`：每条 RRSIG 对所有同标签、同算法候选 DNSKEY 的回调验签记录。
- `Report.EarliestIntervals()`：扁平化返回每个“解析器 × RRset × 阶段”的最早失验区间。
- `PairReport.Stage(name)`、`Report.Pair(resolver, rrset)`：查找结果。
- `StageReport.Segments`：原子时间段；活动签名集合和策略结果在段内不变。
- `StageReport.InvalidIntervals` / `FirstInvalid`：合并后的连续失验区间。
- `Segment.AlgorithmStatuses`、`Segment.Missing` 和 `InvalidInterval.Missing`：定位缺失能力及原因。

时间与 DNSSEC 字节辅助 API：

- `Timestamp`（`uint32`）、`Interval{Start, End int64}`。
- `UnwrapTimestamp`、`UnwrapWindow`、`Interval.Circular`。
- `BuildSignedData`：按 RFC 4034 拼接 RRSIG RDATA（不含 Signature）、规范 Signer Name 和规范 RRset。
- `EncodeNameWire`：编码小写、非压缩 DNS 名称。

## 关键判定语义

### 1. Key tag 只用于筛选候选

RRSIG 的 `KeyTag` 和 `Algorithm` 共同筛选 DNSKEY。对于所有相同标签且算法匹配的
公钥，预检器都会调用一次 `Verifier`，即使前面的候选已经成功，也继续核对其余候选，
并把每个候选的结果放入 `Report.Checks[].Candidates`。

因此标签冲突不会让第二个不同公钥被漏掉。

### 2. 新钥验签成功不等于旧解析器接受新算法

一条签名只有同时满足以下条件才是该阶段的可用能力：

1. 当前分析时刻位于其有效期；
2. 至少一个同标签/同算法 DNSKEY 被回调确认签名；
3. 成功候选匹配该阶段的某条 `TrustGrant`，即密钥身份/公钥和算法组合均受信任。

新算法密钥即使验签成功，如果旧解析器阶段没有对应的密钥/算法授权，其状态仍是
`Verified=true, Usable=false`。对于 `AllAlgorithms` 策略，报告明确列出缺失算法；
缺失原因包括：

- `no-active-signature`：该算法在本段没有处于有效期的 RRSIG；
- `no-eligible-dnskey`：没有按标签和算法匹配到 DNSKEY；
- `verification-failed`：候选密钥均被回调拒绝；
- `untrusted-capability`：签名可由某密钥验证，但没有受信任的密钥/算法组合；
- `no-trusted-capability`：阶段本身没有任何信任授权。

### 3. OR 与 AND 策略

- `any`：存在任意可用受信任签名即通过；若列出算法，则必须是其中之一。
- `all-algorithms`：每个指定算法都至少有一份当前可用签名才通过。

这覆盖“有的解析器任一算法即可”和“有的解析器要求旧算法与新算法各有一份签名”。

### 4. 32 位时间、半圈限制和边界

RRSIG 的 `Inception`、`Expiration` 是 32 位序列号时间。输入窗口必须严格短于
`2^31`；恰好半圈或更长会返回错误，避免错误展开。

内部使用展开后的 int64 半开区间 `[Start, End)`。DNSSEC 的 Expiration 秒本身仍然
有效，因此：

```text
Inception=100, Expiration=200  => [100, 201)
```

即第 200 秒有效，第 201 秒失验。这样旧签名 `Expiration=200`、新签名
`Inception=201` 时两段刚好连续，不会误报空档；若新签名从 202 开始，则只失验
第 201 秒，输出 `[201,202)`。

`ReferenceTime` 用于把跨越 `uint32` 尾部/零值的窗口放到同一条展开时间轴上。
`Interval.Circular()` 可重新输出原始 32 位边界和 `WrapsZero` 标记。

### 5. 分析范围

- 未设置 `Request.Horizon` 时，每个 RRset 的时间轴是其全部 RRSIG 窗口的最小起点
  到最大排他终点；这适合找给定签名之间的交错空档。
- 设置 `Request.Horizon` 后，结果裁剪到该展开区间，因此也能输出最后一份签名过期后
  到规划窗口结束之间的失验尾部。
- 没有任何 RRSIG 且未设置 `Horizon` 时，时间轴为零长度；要分析“完全无签名”的
  有限规划窗口，请显式提供 `Horizon`。

## 最小示例

```go
package main

import (
    "fmt"
    dp "dnssecpreflight"
)

func main() {
    rrset := dp.RRSet{ID: "www.example.", Canonical: []byte("canonical-rrset")}
    oldKey := dp.DNSKEY{ID: "old", Tag: 100, Algorithm: dp.AlgRSASHA1, PublicKey: []byte("old-public")}
    newKey := dp.DNSKEY{ID: "new", Tag: 200, Algorithm: dp.AlgECDSAP256SHA256, PublicKey: []byte("new-public")}
    oldSig := dp.RRSIG{
        RRSetID: rrset.ID, Algorithm: dp.AlgRSASHA1, KeyTag: oldKey.Tag,
        Inception: 100, Expiration: 200, Signature: []byte("valid-for-old"),
    }
    newSig := dp.RRSIG{
        RRSetID: rrset.ID, Algorithm: dp.AlgECDSAP256SHA256, KeyTag: newKey.Tag,
        Inception: 150, Expiration: 250, Signature: []byte("valid-for-new"),
    }

    req := dp.Request{
        RRsets:        []dp.RRSet{rrset},
        Keys:          []dp.DNSKEY{oldKey, newKey},
        ReferenceTime: 0,
        Verifier: dp.BoolVerifier(func(key dp.DNSKEY, _ []byte, signature []byte) bool {
            // 示例用签名字符串模拟；生产代码应在此调用真实 DNSSEC 算法。
            return string(signature) == "valid-for-"+key.ID
        }),
        Signatures: []dp.RRSIG{oldSig, newSig},
        Resolvers: []dp.Resolver{{
            Name: "strict-dual-stack",
            Stages: []dp.Stage{{
                Name: "double-dnskey-and-rrsig",
                Trust: []dp.TrustGrant{
                    {KeyID: oldKey.ID, Algorithm: dp.AlgRSASHA1},
                    {KeyID: newKey.ID, Algorithm: dp.AlgECDSAP256SHA256},
                },
                Requirement: dp.AllAlgorithmsRequirement(dp.AlgRSASHA1, dp.AlgECDSAP256SHA256),
            }},
        }},
    }

    report, err := dp.Preflight(req)
    if err != nil {
        panic(err)
    }
    for _, gap := range report.EarliestIntervals() {
        fmt.Printf("%s %s %s: first invalid [%d,%d] inclusive-end=%d missing=%v\n",
            gap.ResolverName, gap.RRSetID, gap.StageName,
            gap.Start, gap.End, gap.EndInclusive, gap.MissingAlgorithms)
    }
}
```

## 运行方法

要求 Go 1.20 或兼容的 Go 1.20.2 工具链；没有第三方依赖。

```sh
go test ./...
go test ./... -count=1 -cover
```

格式化检查：

```sh
gofmt -l .
```

## 已用本地合成数据验证的范围

当前测试全部基于工作区内构造的数据，未联网、未访问真实目标、未读取凭据，也未安装
依赖。测试中的“签名”多数是按候选 ID 返回的合成核对结果；另有一个用 Go 标准库
`crypto/ed25519` 生成并真实验签的端到端回调测试。

已覆盖并由断言核对的场景：

1. **相同 key tag 冲突**：两个同标签、同算法公钥都会传给回调；即使 `dup-b` 成功，
   `dup-a` 仍被尝试，`SignatureCheck.Candidates` 正确记录一成一败。
2. **旧解析器不接受新算法**：ECDSA 新钥显示 `Verified=true` 但对旧阶段
   `Usable=false`；最早失验区间为 `[201,251)`，缺失算法明确为 RSA/SHA-1（5）。
3. **任一算法策略**：旧算法 `[100,201)` 与新算法 `[150,251)` 重叠后覆盖完整时间轴，
   断言不存在失验区间。
4. **指定算法全部有效策略**：早期缺少算法 13 的区间为 `[100,150)`，重叠期
   `[150,201)` 通过，RSA 过期后的区间为 `[201,251)` 且缺少算法 5。
5. **可定位的失败原因**：同标签 ECDSA 公钥可验但不受信任时，缺失能力分类为
   `untrusted-capability`；回调拒绝时分类为 `verification-failed`。
6. **换签阶段**：`old-only`、`dual`、`new-only` 三类阶段分别得到预期的最早区间。
7. **32 位回绕**：`fffffff0..0000000a` 展开为 `[-16,11)`，并正确标记
   `WrapsZero`；跨零空档 `0b..0c` 展开为 `[11,13)`。
8. **半圈限制**：恰好 `2^31` 和更长窗口返回错误，`2^31-1` 的较短窗口通过。
9. **DNSSEC 包含式边界**：Expiration=200 时第 200 秒仍在 `[100,201)` 内，第 201
   秒才失验。
10. **交错窗口**：三个首尾交错窗口保持连续有效；前一签名 Expiration=40、后一签名
    Inception=42 时，准确得到一秒空档 `[41,42)`。
11. **显式 `Horizon`**：分析窗口延伸到最后一张签名过期之后时，输出失验尾部
    `[201,300)`。
12. **真实标准库签名回调**：Ed25519 合成密钥对和签名经 `ed25519.Verify` 成功。
13. **RFC 4034 签名输入构造**：断言 `BuildSignedData` 输出的固定 RRSIG 字段、小写
    Signer Name wire encoding 和 canonical RRset 后缀；并断言回调收到该字节串。
14. **输入防御**：缺少 `Verifier`、RRSIG 引用未知 RRset、窗口达到/超过半圈均返回
    错误。

最后一次实际复核执行 `go test ./... -count=1 -cover` 的结果为：

```text
ok  	dnssecpreflight	0.386s	coverage: 88.5% of statements
```

可使用相同命令重新生成最新证据；加 `-v` 可查看全部测试名和子测试结果。
`go test ./...` 也会运行 `ExamplePreflight` 并校验其 `Output:` 注释。耗时会随机器波动。
