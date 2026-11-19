# encupload — 加密上传控制器（合成存储后端）

Go 1.20.2 实现的“分段上传 → 显式选版 → 单密钥合成 → 按桶政策发布”控制器。
全部实现只依赖标准库，无第三方依赖，无网络访问。

## 入口

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| `encupload.NewController()` | `core.go` | 创建控制器（逻辑时钟、密钥代际、桶、会话） |
| `(*Controller).NewKeyGeneration(gen)` | `core.go` | 安装密钥代际，代际必须递增；新代际自动获准 |
| `(*Controller).RevokeKeyGeneration(gen)` | `core.go` | 撤销代际；未知代际返回 `ErrKeyGenerationUnknown`，重复撤销幂等 |
| `(*Controller).CreateBucket(name)` | `core.go` | 建桶（默认无限制政策） |
| `(*Controller).CreateSession(bucket)` | `core.go` | 开上传会话，返回 `sess-N` |
| `(*Controller).UploadSegment(session, number, data)` | `core.go` | 追加一个新版本，返回 `Receipt` |
| `(*Controller).UploadBatch(session, map[int][]byte)` | `core.go` | 原子批量追加：全成功或全不写 |
| `(*Controller).Complete(Completion)` | `completion.go` | 按显式选版立即合成并发布 |
| `(*Controller).CompleteWithReceipts([]Receipt)` | `completion.go` | 上述入口的回执形式别名 |
| `(*Controller).SetPolicy(bucket, *Policy)` | `core.go` | 在当前逻辑时刻安装政策 |
| `(*Controller).SchedulePolicy(bucket, *Policy, at)` | `core.go` | 在逻辑时刻 `at` 安装政策（不得回填过去） |
| `(*Controller).ScheduleCompletion(Completion, at)` | `completion.go` | 在逻辑时刻 `at` 提交完成请求（不得回填过去） |
| `(*Controller).RunUntil(at)` / `Tick()` | `core.go` | 推进逻辑时钟并按 (时刻, 提交序) 执行到期操作 |
| `(*Controller).Object(bucket)` | `completion.go` | 读者入口，返回深拷贝；未发布时 `ErrObjectNotFound` |
| `(*Controller).Session(id)` / `SegmentVersions(sid, n)` | `core.go` | 只读状态，用于观察失败尝试与版本表 |
| `encupload.SealSegment` / `OpenSegment` / `KeyFingerprint` | `core.go` | 合成后端的加解密/指纹辅助，便于夹具与断言 |

`Receipt` 字段：`SessionID`、`Number`、`Version`、`Digest`（明文 SHA-256 hex）、
`KeyGeneration`（后端实际使用的代际）。`Completion.Selections` 就是一组
`Receipt`，因此选版是“钉住回执”，不是“取当前第 N 段”。

## 题面交叉约束 → 实现 → 验收用例

| 约束 | 实现位置 | 验收用例 |
| --- | --- | --- |
| 回执给出会话、段号、该段版本、内容摘要、后端实际密钥代际 | `core.go` `receiptFor` / `UploadSegment` | `TestCompleteHappyPath`（逐字段断言） |
| 完成请求显式列出选中的段版本 | `Completion.Selections []Receipt` | `TestReuploadDoesNotReplaceListedVersion` |
| 同一段号重传不能替换已列明版本 | 版本只追加（`segments[n] = append(...)`），回执比对 `Version/Digest/KeyGeneration` | `TestReuploadDoesNotReplaceListedVersion`、`TestConcurrentUploadsKeepVersionsDistinct` |
| 所有选中段须属于同一会话 | `resolveSessionLocked` + 逐条 `SessionID` 校验 | `TestCompletionMembershipAndCoverage`（`ErrForeignSession`） |
| 使用同一把密钥 | `buildObjectLocked` 中 `keyGen` 全等检查 | `TestSingleAuthorizedKeyGeneration`（`ErrMixedKeyGenerations`） |
| 该密钥仍获准 | `c.authorized[gen]` 检查 | `TestRevokedGenerationBlocksCompletionUntilReencrypted`（`ErrKeyRevoked`） |
| 与当前目标桶政策吻合 | `Policy.deny`（代际钉住 / 段数上下界 / 明文字节上限） | `TestBucketPolicyBoundaries`（含恰好等于上限的可接受边界、超一个字节的拒绝） |
| 政策变更与完成请求同一时刻，只观察到一个符合提交次序的结果 | 逻辑时钟 + `policyEffectiveAt(at, seq)` 以提交序打破平局；立即路径在同一把锁内完成判定 | `TestPolicyAndCompletionAtSameInstant`（三种次序）、`TestConcurrentPolicyAndCompletionIsSingleOutcome`（200 轮竞态） |
| 失败后仍可选取重新加密的段再次完成 | 失败不改动任何可见状态，仅记 `failedAttempts`/`lastErr`；版本/会话可继续追加 | `TestRevokedGenerationBlocksCompletionUntilReencrypted`、`TestReaderNeverSeesPartialObject` |
| 读者始终只看到完整对象 | 组装在锁内完成，最后 `ssn.published = obj`、`bucket.object = obj` 一次赋值；读路径返回深拷贝 | `TestReaderNeverSeesPartialObject`（并发读者）、`TestObjectAndReceiptCopiesAreIsolated` |
| 存储被篡改/摘要不符不放行 | 合成时从密文解密并校验 HMAC 与摘要 | `TestTamperedStorageIsRejected`、`TestDigestMismatchIsRejected`、`TestSegmentEncryptionRoundTrip` |

## 语义要点

- **版本只追加**：`UploadSegment(sid, n, ...)` 永远产生 `len(versions)+1`，从不覆盖。
  被列进完成请求的回执永远指向同一份字节。
- **回执即承诺**：完成请求必须原样回传 `Receipt`（含 `Digest` 与 `KeyGeneration`）。
  只改版本号不改回执 → `ErrReceiptMismatch`；伪造代际 → 同样拒绝。
- **一次会话一次发布**：跨会话 → `ErrForeignSession`；漏段/多段 → `ErrIncompleteComposition`；
  重复段号 → `ErrDuplicateSegment`；幽灵版本 → `ErrUnknownVersion`；
  已发布的会话再上传或再完成 → `ErrSessionCompleted`。
- **政策时间不可回填**：`SetPolicy` 以当前时刻安装；`SchedulePolicy` /
  `ScheduleCompletion` 只接受 `>= Now()`，避免用已失效的政策为既有对象背书。
- **同刻平局**：时刻相同则按提交序（`seq`）判定。先提交政策 → 完成被政策拒绝；
  先提交完成 → 完成按旧政策提交成功，后到的政策不追溯。
  立即路径（`Complete` / `SetPolicy`）在同一把互斥锁内完成“取时钟 + 判定 + 落库”，
  因此并发下也只有上述两种终态之一。
- **失败无副作用**：任何拒绝都发生在最后两次赋值之前，读者看到的状态与失败前一致。

## 验证范围与运行方式

```sh
go test ./...          # 全部验收用例
go test -race -count=3 ./...   # 并发用例在竞态检测器下重复运行
```

已实际执行（Go 1.20.2 darwin/arm64）：

- `go test -count=1 ./...` → `ok  example.com/encupload`
- `go test -race -count=3 ./...` → `ok  example.com/encupload`
- `gofmt -l .` → 无输出

测试文件：

| 文件 | 覆盖 |
| --- | --- |
| `controller_test.go` | 完整生命周期、选版钉定、会话/覆盖交叉约束、单密钥与撤销后重新加密、政策边界、同刻次序、批次原子性、读者隔离、参数校验、提交后重传/提交后改政策的交错 |
| `crypto_test.go` | 密文往返、段号/代际绑定、篡改与截断检测、摘要不符拒绝 |
| `concurrency_test.go` | 政策变更 vs 完成的 200 轮竞态（只允许两种一致终态）、并发上传版本互不覆盖 |
| `example_test.go` | 可执行文档示例（含期望输出断言） |

共 21 个顶层测试/示例函数（另含 5 个子测试、2 个带输出断言的示例），全部通过
（`--- PASS` 与 `ok  example.com/encupload`）。

验收用例同时包含：可接受结果（`TestCompleteHappyPath`、`TestBucketPolicyBoundaries`
的恰好等于上限边界、`TestPolicyAndCompletionAtSameInstant/completion_first_commits`），
应被拒绝的结果（外来会话、混代际、撤销代际、政策超限、重复/缺失/虚构段、伪造回执），
以及影响两者的边界与交错场景（同刻政策与完成、字节上限 ±1、撤销后重新加密再完成、
完成请求提交后同段号重传）。

## 工具限制说明

`go vet ./...` 在当前会话中被权限拒绝执行，因此未单独运行；`go test` 默认会执行
`go vet` 的测试子集（atomic、bool、buildtags、errorsas、ifaceassert、nilfunc、
printf、stringintconv 等），故上述 `go test` 结果已包含该范围的检查。除该项外，
上述所有命令均已实际执行。

## 非目标

- 合成后端仅用于本地验证：密钥由代际确定性派生，密文为 HMAC-SHA256 密钥流 + HMAC 标签，
  不具备生产级密码学语义（无 KMS、无密钥轮换审计、无持久化）。
- 不做网络传输、不做真实对象存储对接；全部产物留在本地工作区。
