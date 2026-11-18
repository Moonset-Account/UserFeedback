# modaudit — 特权服务模块加载来源离线审查库

离线核对特权服务的模块最终会从哪个目录加载。纯标准库实现，无网络、无外部依赖。

## 实际入口

- 库入口：`audit.go` 中 `func Audit(in Input) []ConfigReport`（包 `modaudit`）。
- 测试入口：`audit_test.go`，使用 Go 内置 `testing`，命令 `go test ./...`。
- 构建约束：`go.mod` 声明 `go 1.20`，已在 Go 1.20.2 上验证。

## 输入模型（`Input`）

- `Service`：服务清单，`Approved` 给出各模块名批准放行的摘要清单。
- `Callers`：调用方模块及其依赖（解析入口，自身不参与合规检查）。
- `Configs`：多种启动配置，各自给出按启动顺序排列的候选目录。
- `Trusted`：目录信任快照；`Dirs`：目录内容快照（模块名/版本/摘要/依赖/追加目录）；
  `Unverified`：尚未核实的目录；`Compat`：兼容性快照（需求方 → 依赖 → 兼容版本）。

## 审查语义

1. 解析某模块的依赖时，有效候选目录 = 链上各模块经 `AddDirs` 追加的目录
   （近者优先，仅对追加者自身及后代生效）+ 启动配置目录（按配置顺序）。
2. 按顺序扫描，**首个兼容**模块文件即为选中项；它必须来自可信目录，且摘要命中
   该服务的批准清单。搜索后续位置的合规副本不能作为放行依据。
3. 按配置分别查完传递依赖（深度优先、按依赖声明顺序），报告**首个**有风险的
   来源链（`Finding.Chain`，从调用方到风险来源），风险原因包括
   `untrusted_dir`、`digest_not_approved`、`unresolved`。
4. 若选中位置之前存在尚未核实、且足以改变选择的候选目录，结果标为
   `pending_evidence`（待补证）；已核实数据下的风险链仍一并给出。

## 验证范围（`go test ./...`，全部真实断言）

- 可接受结果：可信目录 + 获批摘要 → `ok`（`TestAcceptableResolution`）。
- 应被阻止：首个兼容模块来自不可信目录（`TestFirstCompatibleFromUntrustedDir_LaterCompliantCopyIgnored`）
  或摘要未获批（`TestFirstCompatibleDigestNotApproved`）；后续位置的合规副本不能放行。
- 边界/交错：
  - 兼容性快照决定“首个兼容”，早期不兼容副本被跳过（`TestIncompatibleEarlierCopySkipped`）。
  - 两种启动配置对同一依赖选中不同文件，一合规一风险（`TestTwoConfigsSelectDifferentFiles`）。
  - 追加目录仅对追加者及后代生效，兄弟模块不可见（`TestScopedDirsOnlyForSelfAndDescendants`）。
  - 传递依赖查完后给出首个风险来源链 app→mid→leaf（`TestTransitiveFirstRiskChain`）。
  - 未核实早期候选 → 待补证（`TestPendingEvidenceWhenUnverifiedEarlierCandidate`）；
    未核实目录在选中位置之后 → 不触发（`TestUnverifiedDirAfterSelectionDoesNotPending`）。
  - 风险判定遇更早未核实候选 → 整体待补证、风险链仍给出（`TestPendingOverridesRiskVerdict`）。
  - 无兼容模块 → `unresolved`；叠加未核实目录 → 待补证（`TestUnresolvedDependency`）。
