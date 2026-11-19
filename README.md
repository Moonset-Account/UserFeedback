# 飞书顾问资料授权模块（Go 1.20.2）

顾问对客户资料的读取/写入授权，实现于本地纯内存包 `feishu/authz`，无外部依赖。

## 实际入口

- 模块：`feishu/authz`（`go.mod` 声明 `module feishu/authz`，`go 1.20`）
- 实现：`authz/authz.go`
  - `authz.New(materials []Material, source Source) (*Engine, error)` — 构造引擎；`Source.Download` 模拟正文下载，返回错误即“下载失败”。
  - `(*Engine).Preview(ConsultantID) []CatalogEntry` — 预览目录，只返回元数据，不含正文。
  - `(*Engine).Open(ConsultantID, MaterialID) Outcome` — 打开资料（公开/保密）。
  - `(*Engine).Write(ConsultantID, ClientID) Outcome` — 写入前核对历史保密读取。
  - `(*Engine).Snapshot(ConsultantID) Snapshot` — 只读快照（行业承诺 + 读取历史）。
- 验收用例：`authz/authz_test.go`（Go 内置 `testing`，含计数下载源与启动栅栏式并发夹具）。

## 运行与验证

```sh
go test ./...
go test ./... -race -count=10
gofmt -l .        # 期望无输出
```

`go test ./...` 全部通过。测试入口为 `authz/authz_test.go` 中的 `Test*` 函数，均为真实断言（无 `t.Skip`、无空断言）。

## 规则与承诺点

1. **公开资料**：任何顾问随时可读，不产生任何承诺，也不进入写入核对历史。
2. **首次成功打开保密资料即是承诺点**：某行业第一次成功打开某客户的保密资料后，该顾问在该行业只能读该客户的保密资料；其他行业的保密资料仍按各自行业独立判定。
3. **预览不占选择**：`Preview` 只返回目录元数据（无正文），且不改变任何状态。
4. **失败下载不占选择**：`Source.Download` 出错时返回 `ReasonDownloadFailed`，不记录读取、不建立承诺；随后仍可自由选择该行业的第一家客户。
5. **并发首次打开**：同一顾问、同一行业的保密打开被串行化（按 `consultant+industry` 的引用计数互斥闸门），检查与承诺在同一临界区内完成。因此两个并发首次打开不同客户时，先到者下载并承诺，后到者在下载前即被拒绝（`ReasonIndustryCommitted`，`LimitedBy` 指向先到者的读取），不会拿到另一客户的正文；不同行业、不同顾问互不阻塞。
6. **写入核对全部历史保密读取**：目标客户必须与顾问此前成功读取过的每一份保密资料的客户一致，否则拒绝；`LimitedBy` 指向最早的那次冲突读取，`Conflicts` 按读取顺序列出全部冲突来源（同一份资料被重复读取只计一次，取最早一次）。
7. **可区分性**：被拒操作的 `LimitedBy` 给出具体是哪一次成功读取造成了限制；`NewConstraint` 非空当且仅当本次操作真的形成了新的行业承诺（重复打开同客户、预览、失败下载、任何写入均为 `nil`）。

## 测试覆盖矩阵

| 场景 | 用例 |
| --- | --- |
| 可接受：公开资料随时可读、且不占选择 | `TestPublicMaterialsAlwaysReadableWithoutCommitment` |
| 可接受：同客户重复打开、跨行业独立承诺 | `TestIndustryCommitmentRestrictsConfidentialReads` |
| 可接受：写入目标与全部历史读取一致 | `TestWriteChecksAllReads`（写入 A 段） |
| 应拒绝：同行业另一客户保密资料 | `TestIndustryCommitmentRestrictsConfidentialReads` |
| 应拒绝：下载失败，且失败后仍可选首家 | `TestPublicMaterialsAlwaysReadableWithoutCommitment`、`TestDownloadErrorIsReported` |
| 应拒绝：写入目标与任一历史来源不同 | `TestWriteChecksAllReads`、`TestWriteBoundaryAfterIndustryLock` |
| 边界：重复读取同一份资料不重复计入冲突 | `TestWriteConflictsDedupRepeatedReads` |
| 边界/交错：并发首次打开不同客户最多一个拿到正文 | `TestConcurrentFirstOpenDifferentClients` |
| 边界/交错：并发同客户均放行、只报告一次新承诺 | `TestConcurrentFirstOpenSameClient` |
| 边界/交错：并发跨行业互不阻塞 | `TestConcurrentFirstOpenDifferentIndustries` |
| 边界/交错：并发中一方下载失败不占行业 | `TestConcurrentFirstOpenWithFailedDownload` |
| 边界/交错：第二个行业的读取收紧写入规则 | `TestWriteBoundaryAfterIndustryLock` |
| 参数校验：空顾问/空客户/未知资料/重复目录项 | `TestInvalidInputs` |
| 顾问隔离：甲的承诺不影响乙 | `TestConsultantIsolation` |

## 状态与并发模型

- 每个顾问的状态（行业承诺 + 读取历史 + 自增读取 ID）由 `Engine.mu` 保护。
- 保密 `Open` 先获取 `(consultant, industry)` 闸门，再在 `Engine.mu` 内做策略预检：已承诺给其他客户则直接拒绝、不下载；否则在闸门持有期间下载正文，成功后在同一临界区记录读取并（首次时）建立行业承诺。闸门引用计数归零即回收，映射不会无界增长。
- 公开 `Open` 不经过闸门，不改变任何状态。
- `Grant.ID` 每顾问单调递增，用作 `LimitedBy` 与 `NewConstraint` 的稳定标识。

## 范围说明

仅使用本地合成夹具（内存目录与内存正文），不进行任何真实目标探测或网络访问；不读取账号配置或凭据，不写入工具权限配置。
