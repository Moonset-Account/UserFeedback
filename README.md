# bootmeasure — 受保护工作负载测量判定模块

面向受保护工作负载的测量启动判定库（Go 1.20.2，仅依赖标准库）。模块消费验真层产出的
有序测量记录，对照主机批准的两套完整组件顺序（正常启动 `normal`、升级后的恢复启动
`recovery`），输出显式判定状态，并对每个可用结论附上所依据的基线版本。

## 实际入口

包路径（`go.mod` 模块名）：`bootmeasure`，源码位于仓库根目录。

- `NewDecider(baselines []Baseline, store NodeStateStore) (*Decider, error)`
  构造判定器，校验基线集合（版本唯一非空、有序列类型、每步含组件名与摘要）并强制要求
  提供节点状态存储。
- `(*Decider).Evaluate(rec Record) Decision`
  唯一判定入口，对单条记录依次执行：输入校验 → 全基线整体匹配 → 代次核对 →
  按节点回退保护 → 成功时提交节点代次。
- `NewMemoryNodeStateStore()` / `NodeStateStore` 接口
  节点已确认最高固件代次的存储抽象，内置并发安全的内存实现；生产环境可替换为持久化实现。
- 关键类型：`Baseline`（批准序列与基线版本）、`Step`（含 `Optional` 标记）、
  `Measurement`、`Record`、`Decision`（`Status` + `BaselineVersion` + `Reason`）。

本仓库为库工程，不提供可执行 `main`；验收入口为测试：`go test ./...`。

## 判定规则与题面约束的对应

1. **两套批准序列，整体匹配**：每条记录必须被**一条且仅一条**完整基线解释。匹配按
   测量顺序与基线步骤锁步推进，任何测量都必须消费一个基线位置；两条基线的局部匹配
   不能拼接成接受结论（`TestSplicedSequencesRejected`，含同代次拼接与跨代次拼接）。
2. **允许可选步骤**：`Step.Optional` 的步骤可在声明中缺省；若出现则仍须位于基线位置、
   摘要匹配。可选步骤迟到、重复声明均不被接受（`TestOptionalStepInterleaving`）。
3. **按节点阻止代次回退**：节点一旦在新代次被确认，再出示旧代次基线即返回
   `rollback_blocked`，且不修改已确认的高水位；旧序列对其他节点仍保留有效
   （`TestRollbackBlockedAfterNewGeneration` 同时验证同代次另一节点仍可接受）。
4. **缺测与顺序不符是不同状态**：流提前结束、必需步骤从未测量 →
   `missing_measurement`；跳过必需步骤后又出现后续组件、可选步骤错位 →
   `order_mismatch`；摘要不符 → `digest_mismatch`；组件不属于任何基线 →
   `no_baseline_match`（`TestMissingVsOrderMismatch`、`TestDigestMismatch`）。
5. **每个可用结论附基线版本**：`accepted`、`generation_mismatch`、`rollback_blocked`
   均在 `Decision.BaselineVersion` 给出唯一基线版本；无法唯一归因（无匹配/多基线歧义）
   时该字段留空，避免虚构依据（`TestGenerationMismatch`、`TestAmbiguousBaseline`）。

## 全部判定状态

| 状态 | 含义 | 是否附基线版本 |
| --- | --- | --- |
| `accepted` | 完整匹配唯一基线且通过代次/回退检查 | 是 |
| `missing_measurement` | 必需组件从未被测量 | 否 |
| `order_mismatch` | 组件出现位置早于基线要求（含可选步骤错位） | 否 |
| `digest_mismatch` | 组件存在但测量摘要与基线不符 | 否 |
| `no_baseline_match` | 组件不属于任何基线，无基线可解释 | 否 |
| `generation_mismatch` | 序列匹配但基线代次与记录声称代次不一致 | 是 |
| `rollback_blocked` | 节点已确认更高代次，旧基线被按节点阻止 | 是 |
| `ambiguous_baseline` | 多条基线同时完整匹配，无法唯一归因 | 否 |
| `invalid_input` | 记录本身畸形（无节点 ID、无测量、空组件/摘要） | 否 |
| `internal_error` | 节点状态存储故障，未提交任何状态 | 否 |

## 验证范围与运行方式

```sh
go test ./...            # 验收测试（全部为真实断言，无占位用例）
go test -race ./...      # 竞态检测
go test -cover ./...     # 语句覆盖率（当前 92.4%）
gofmt -l .               # 格式检查（无输出即干净）
```

`decider_test.go` 使用本地合成夹具（SHA-256 派生摘要，无外部依赖、无网络访问），覆盖：

- **可接受结果**：完整正常序列（含/不含可选步骤）、恢复序列、成功后的代次提交。
- **应被阻止/拒绝的结果**：两套序列拼接、跨代次拼接、节点回退、摘要篡改、
  代次谎报、多基线歧义、畸形输入、重复/迟到可选步骤。
- **边界与交错场景**：缺测与顺序不符的分界、未知组件与错位组件的分界、
  全新节点（高水位为 0）边界、存储单调性（`ErrRollbackAttempt`）、
  同节点新旧代次记录 32 对 goroutine 并发交错下的一致性与高水位单调。

## 设计边界

- 模块只做判定与本地状态维护，不执行实际目标探测、攻击或载荷生成。
- 摘要比较使用 `crypto/subtle.ConstantTimeCompare`，避免长度/前缀时序泄漏。
- 内存存储仅用于单进程与测试；多实例部署需实现 `NodeStateStore` 的持久化版本。
