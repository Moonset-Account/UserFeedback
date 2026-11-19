# advisory — 单向安全公告接收端

隔离网络里，接收方只能单向收到发送方推送的公告数据，无法请求重传。本模块是接收侧：
由调用方提供「发送区签名核验器」，模块负责按已核验清单把任意顺序到达的块组装成整份公告，
并在整份校验通过且版本更新时才发布。

Go 1.20.2，无第三方依赖。

## 入口

```go
import "advisory"

r, err := advisory.New(verifier)   // verifier 实现 advisory.Verifier；nil 直接报 ErrNoVerifier
```

| 方法 | 作用 |
| --- | --- |
| `New(v Verifier) (*Receiver, error)` | 创建接收器。必须提供来源核验器，否则整个接收器不可用。 |
| `(*Receiver).OfferManifest(region []byte) (Manifest, error)` | 先核验 `region` 的发送区签名，通过后才解析清单。失败不改任何状态。 |
| `(*Receiver).SubmitChunk(gi, ci int, data []byte) error` | 提交第 `gi` 组第 `ci` 块，任意顺序。长度或摘要不符返回 `ErrChunkDigestMismatch`，按缺块处理，不覆盖已存好块。 |
| `(*Receiver).SubmitParity(gi int, data []byte) error` | 提交第 `gi` 组的异或校验块。长度须等于该组最长块，且摘要须与清单一致。 |
| `(*Receiver).Status() Status` | 快照：各组缺失块、是否可修复、`MissingGroups`（可证明仍缺的组）、`Current`（当前展示的上一份完整公告）。 |
| `(*Receiver).Publish() (Bulletin, error)` | 组齐（完整或可单块修复）→ 组装 → 按声明长度截取 → 整份摘要核对 → 版本比较，全部通过才发布。 |
| `(*Receiver).Current() (Bulletin, bool)` | 已发布的完整公告。 |
| `(*Receiver).Reset()` | 丢弃待发布会话，保留已发布公告。 |

`Verifier` 是单方法接口；`advisory.VerifierFunc` 可把普通函数适配成核验器：

```go
r, _ := advisory.New(advisory.VerifierFunc(func(region []byte) error {
    return sendingKey.VerifyDetached(region, sig)
}))
```

## 清单格式

`OfferManifest` 的入参是发送方签名覆盖的**原始字节**。解析为严格 JSON（`DisallowUnknownFields`，
拒绝尾随数据）：

```json
{
  "version": 7,
  "length": 40,
  "bulletin": "<sha256 hex>",
  "groups": [
    {"chunks": [{"length": 5, "digest": "<hex>"}, {"length": 9, "digest": "<hex>"}],
     "parity": "<hex>"}
  ]
}
```

- `version`：公告版本，`uint64`。
- `length`：整份公告声明长度。
- `bulletin`：整份公告（截取到 `length` 后）的 SHA-256。
- `groups[].chunks[]`：每块的精确长度与 SHA-256。
- `groups[].parity`：该组异或校验块的 SHA-256。校验块长度 = 该组最长块长度；短块按右侧补零后参与异或。

摘要均为 64 位十六进制。大写输入会被规范化为小写，因此清单**指纹**（对规范化内容重新序列化后的
SHA-256）不受十六进制大小写影响：同一个公告重新签名成不同大小写仍是同一份清单，不会误判冲突。

## 约束与行为

1. **来源核验优先**：`OfferManifest` 第一件事是调用核验器。核验失败（含核验器自身报错）返回
   `ErrUnverified`，不解析、不建会话、不可能发布。`New(nil)` 返回 `ErrNoVerifier`。
2. **任意顺序收块**：`SubmitChunk`/`SubmitParity` 互不依赖顺序；`Receiver` 内部加锁，可并发提交。
3. **每组至多缺一块可恢复**：组内恰好缺一块且持有该组校验块时，用「右侧补零等长规则」重建：
   以校验块为初值，逐块异或（短块自然补零），截到目标块长度，再与清单中该块的摘要比对，
   通过才算可修复。缺两块及以上一律不可修复。
4. **错误摘要按缺块处理**：块或校验块长度/摘要不符时不落库，`Status` 中呈现为缺失；之后同块号的
   正确数据可正常接收。
5. **声明长度截取**：组装后可有多余尾部字节，发布前截到 `length`。
6. **整份核对才发布**：截取后的字节必须命中清单 `bulletin` 摘要，否则返回
   `ErrBulletinDigestMismatch` 并清空本会话已收数据（无法归因到某一组，只能整体重收）。
7. **同版本不同清单不混用**：同版本号但规范化内容不同的清单返回 `ErrManifestConflict`，状态不变；
   同版本同内容的重复下发视为重传，保留已收数据。已发布版本也会挡住同版本异内容（冲突）与
   不更新（`ErrStaleManifest`）的清单。
8. **版本必须更高**：`Publish` 仅当待发布版本严格高于当前公告时才发布；不满足返回 `ErrStaleManifest`。
9. **可证明未收齐时列出缺组**：`Publish` 返回 `*IncompleteError`（可用 `errors.Is(err, ErrIncomplete)`
   判定），其 `Missing` 为仍缺的组号；`Status.MissingGroups` 同步给出。此时 `Current` 保持上一份
   完整公告不变，调用方继续展示它。

`IncompleteError` 是「可证明」的：只有在既不完整也无法凭校验块修复时才列出该组。

## 验证范围

`go test ./...`（全部为真实断言，使用内置 `testing`）：

| 用例 | 覆盖点 |
| --- | --- |
| `TestPublishOutOfOrderWithParityRepair` | 可接受结果：乱序收块 + 单块异或修复（不等长、右补零、需截断）+ 声明长度截取；中途 `Publish` 报缺组。 |
| `TestFullDelivery` | 全部块直收，校验块仅作冗余。 |
| `TestTruncatesToDeclaredLength` | 尾部补齐字节不进入公告。 |
| `TestUnverifiedManifestCannotPublish` | 未签名 / 被篡改 / 他人签名 → `ErrUnverified`，零状态改变，无法发布。 |
| `TestCorruptDataCountsAsMissing` | 块与校验块摘要错误按缺失处理、不覆盖好数据、可重传补回。 |
| `TestParityRepairsCorruptChunk` | 交叉场景：错误摘要块等价于缺块，由校验块修复；缺的是最长块 / 短块两种边界。 |
| `TestParityCannotFakeChunk` | 伪造校验块被拒，不能凭空造块。 |
| `TestTwoMissingChunksAreNotRepairable` | 边界：缺一块可修、缺两块不可修；补一块后恢复可修。 |
| `TestSameVersionDifferentManifestConflicts` | 同版本不同清单冲突；已发布版本的同版本异内容也冲突；同内容重传为 stale。 |
| `TestSameVersionRetransmissionKeepsData` | 同版本同内容重传不丢已收数据。 |
| `TestOlderVersionRejected` | 旧版本被拒，展示不变，不产生待发布会话。 |
| `TestBulletinDigestMismatchBlocksPublish` | 整份摘要不符 → 不发布、保留上一份、数据清空后列为缺组。 |
| `TestIncompleteKeepsPreviousBulletin` | 未收齐时列缺组、继续展示上一份；补齐后正常发布。 |
| `TestMalformedManifestsRejected` | 空/非 JSON/尾随数据/未知字段/负版本/坏 hex/短摘要/声明长度过大/块长 0/无组。 |
| `TestUnknownIndicesRejected` | 越界组号、块号。 |
| `TestNilVerifierRefused` | 无核验器不可构造。 |
| `TestDigestCaseInsensitive` | 十六进制大小写规范化与指纹稳定性。 |
| `TestConcurrentSubmissions` | 并发乱序提交后仍能精确组装。 |

未覆盖：真实签名算法（核验器由调用方注入）、传输层、持久化。
