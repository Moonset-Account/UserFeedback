# hibersnapshot — 休眠镜像快照生成模块

面向多用户主机的休眠镜像（hibernation image）快照生成练习模块。全部数据来自
本地合成的页样本，不进行任何真实目标探测、访问或攻击行为。

## 模型与约束

本地样本 `SampleStore` 维护三类状态：

- **物理页**（`Page`）：页帧号 PFN、内容字节、标签（`LabelNormal` 可导出 /
  `LabelNoImage` 禁止进入镜像）。
- **映射引用**（`Mapping`）：进程 PID + 虚拟地址 VA → 物理页 PFN，并带
  `Confidential` 保密引用标记。
- **世代号**（generation）：任何映射集合变化（新增、`Remap`、删除）都会使世代号
  单调递增；相同内容的重复写入不会推进世代号。

模块实现题面中的三条交叉约束：

1. **保密即整页脱敏**：两个进程共享同一物理页时，只要任一引用要求保密
   （`Confidential=true`），或页面本身带 `LabelNoImage` 标签，镜像中就不导出该页
   的任何真实字节（`Entry.Content == nil`、`Entry.Redacted == true`）。
2. **统一重新初始化标记**：脱敏页的每个可恢复映射都进入恢复计划，且同一 PFN 的
   所有引用共享同一个确定性 `ReinitKey`（基于 PFN 的稳定派生，跨重试与跨次构建
   保持一致）。
3. **一致的快照边界**：每次构建先固定世代号并复制不可变样本，组装后再次核对世代
   号；期间发生任何映射变化即判定边界失效，**整次尝试丢弃并重试**，因此镜像绝不
   可能同时保存旧引用与新页身份。重试预算耗尽时返回
   `RetryBudgetExhaustedError`（包装最后一次 `SnapshotInconsistentError`），而不
   是返回半成品镜像。

## 实际入口

本模块为库包（无 `main` 函数），实际编程入口：

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| `NewSampleStore` / `AddPage` / `AddMapping` / `Remap` / `RemoveMapping` | `snapshot.go` | 构建并变更本地页样本 |
| `(*SampleStore).Build(opts ...BuildOption)` | `builder.go` | 快照生成主入口，带世代边界校验与重试，`WithMaxAttempts` 可调预算 |
| `(*Image).RecoveryPlan()` | `snapshot.go` | 恢复入口：返回需要重建的映射及其共享重新初始化标记 |
| 验收测试 | `snapshot_test.go` | 使用 Go 内置 `testing`，全部为真实断言 |

## 验证范围

运行方式（Go 1.20.2）：

```sh
go test -v ./...        # 详细模式
go test -race ./...     # 竞态检测
go test -count=1 ./...  # 禁用测试缓存
```

7 个验收用例覆盖三类要求的场景：

**可接受结果**
- `TestBuildAcceptableSharedExport`：普通共享页导出真实内容、无需恢复；并断言镜像
  持有防御性拷贝，事后篡改样本不影响已生成镜像。

**应被阻止 / 拒绝的结果**
- `TestBuildConfidentialSharedPageRedacted`：共享页任一引用保密 → 整页脱敏、真实
  字节不出现在任何条目中、两个引用共享同一 `ReinitKey` 且都进入恢复计划，标记跨
  构建稳定。
- `TestBuildNoImageLabelRedacted`：仅靠 `LabelNoImage` 标签同样触发脱敏与恢复。
- `TestBuildRetryBudgetExhausted`：每次尝试期间映射都变化 → 拒绝出图，返回
  `RetryBudgetExhaustedError`，且可 `errors.As` 解包到边界不一致错误。
- `TestStoreGuardsUnknownTargets`：指向不存在物理页的映射/重映射被拒绝；恒等
  `Remap` 不推进世代号。

**边界与交错场景（同时影响上述两类结果）**
- `TestBuildRetriesAcrossRemapBoundary`：通过 `OnBuildAttempt` 钩子在首次尝试的边
  界固定之后、核对之前插入一次 `Remap`；断言首次尝试被丢弃、重试后镜像只含新页
  身份，旧 PFN 与旧内容均不存在。
- `TestRemapMidBuildWithConfidentiality`：交错重映射与保密约束叠加——重映射后加入
  一个含保密引用的共享页，重试镜像中旧身份消失、新身份脱敏且两个引用共享同一标
  记。

## 目录结构

```
go.mod              module hibersnapshot (go 1.20)
snapshot.go         样本存储、类型定义、世代号与重新初始化标记派生
builder.go          Build 主流程：边界固定 → 组装 → 边界核对 → 重试
snapshot_test.go    全部验收测试（真实断言，无空测试）
README.md
```

## 范围声明

仅使用当前工作区内的合成夹具进行防御性设计与验证；不包含、也不执行任何真实目标
探测、漏洞利用或攻击载荷生成。
