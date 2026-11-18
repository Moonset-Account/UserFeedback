# auditsample — 高风险变更独立审计抽样模块

面向独立审计场景的抽样模块：先封存版本化候选集，再接受审计员随机种子，按覆盖下限与配额确定性生成样本；候选更正开启新批次，原批次可完整重放。

## 实际入口

模块根：`go.mod`（module `auditsample`，Go 1.20）。实现位于 `auditsample.go`，包名 `auditsample`。

| 入口 | 作用 |
| --- | --- |
| `NewRegistry()` | 创建批次登记处（追加式，不覆盖历史批次） |
| `Registry.Seal(candidates)` | 封存候选集为新版本批次，记录 SHA-256 摘要；入参切片被拷贝，调用方后续修改不影响已封存批次 |
| `Registry.Correct(prev, candidates)` | 候选更正：封存为下一版本批次，原批次保留 |
| `Registry.Batch(version)` | 按版本取回已封存批次（返回拷贝） |
| `Draw(batch, seed, rules)` | 纯函数抽样：由（候选集、种子、规则）确定性生成样本 |
| `Verify(sample, batch)` | 校验样本：版本一致、候选集摘要未被静默替换、同种子同规则重放结果一致 |

## 规则语义（`Rules`）

- `Quota`：样本总量上限。覆盖下限满足后按洗牌顺序补足至配额；去重后候选不足配额时样本即为全部去重候选。
- `ControlMinimum` / `BizLineMinimum`：每类控制项、每条业务线的覆盖下限。
- 重复的变更编号（`ChangeID`）算一个样本单元，首次封存出现者生效。
- 同一项变更可同时满足一个控制项下限和一个业务线下限（贪心优先选取"双覆盖"候选，同分按洗牌顺序，保证确定性）。
- 配额无解或候选不足时，`Draw` 返回 `*UncoverableError`，逐条列出无法覆盖的条件（类别/业务线、要求值、缺口、原因 `quota_exceeded` / `insufficient_candidates`），不返回样本。

## 防静默替换与重放

- 封存时对候选集规范化编码计算 SHA-256；`Verify` 会**重新计算**所给批次候选的摘要并与样本记录比对，即使伪造相同版本号与摘要字段也能检出替换。
- 抽样顺序由 `SHA-256(seed | 批次摘要)` 派生的 PRNG 洗牌决定，因此原候选 + 原种子 + 原规则必然重放出原样本；候选更正后的新批次摘要不同，旧样本无法对其通过校验。

## 验证范围

测试入口：`auditsample_test.go`（Go 内置 `testing`，外部测试包，全部真实断言）。

```sh
go test ./...
```

覆盖的交叉约束场景：

- **可接受结果**：`TestDrawAcceptableSample` — 配额充足时样本量等于配额，控制项×业务线交叉下限全部满足，同种子重放一致，20 个不同种子均满足约束；`TestDrawNoMinimums` — 无下限时纯随机抽样与零配额空样本。
- **应被拒绝的结果**：`TestDrawQuotaUnsolvableReportsConditions` — 配额 2 无法覆盖 3 控制项 + 2 业务线下限，返回恰好一条 `quota_exceeded` 条件（缺口 1）且无样本；`TestDrawInsufficientCandidates` — 控制项下限超出 distinct 候选数，返回 `insufficient_candidates`。
- **边界/交错场景**：`TestDrawBoundaryQuotaExactlyFitsDoubleCoverage` — 一项变更同时满足控制项与业务线下限，4 条下限由 2 个样本覆盖，配额等于该下界时恰可解、减 1 则必报无法覆盖；`TestDrawDuplicateChangeIDCountsOnce` — 重复变更编号只算一个样本且首次出现生效。
- **封存/重放/防篡改**：`TestVerifyDetectsSilentReplacement` — 调用方改入参切片不影响已封存批次；伪造同版本同摘要的替换批次被检出；登记处保存的封存副本仍可重放。`TestCorrectionOpensNewBatchOriginalReplays` — 更正开启版本 2，原批次+原种子+原规则重放原样本，原样本对新批次校验失败。

## 已知限制

- `go vet ./...` 在本次交付环境中未获执行授权，未运行；`gofmt -l` 无输出（格式合规），`go test ./...` 8 个用例全部通过。
- 覆盖选取为确定性贪心（优先双覆盖），非全局最优装箱；配额不足时按贪心顺序报告首先无法覆盖的条件。
