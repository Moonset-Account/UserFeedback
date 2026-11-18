# certprecheck — 边缘节点证书轮换预检模块

## 功能

边缘节点轮换证书时，预检不同客户端能否在各个上线阶段完成身份校验。

输入：

- 候选节点的证书链（`Node`，叶子证书在前，中间证书随后）
- 服务域名（`domain`）
- 客户端信任根（`Client.Roots`，`Required` 标记必需客户端）
- 分批切换时刻（`Stage`，含阶段时间与该阶段在线节点列表）

行为：

- 在每个阶段按该阶段指定时间 `Stage.At`，对每个必需客户端 × 该阶段每类在线节点，
  用 `crypto/x509` 真实验证证书链（信任根、中间证书、有效期）与服务域名。
- 滚动阶段新旧节点并存时，只有每个必需客户端对其可能访问的每类节点都验证通过，
  才可推进到下一阶段。
- 输出首个阻塞阶段（`Report.BlockingStage`）及具体客户端、节点与失败原因
  （`Report.Failures`：尚未生效 / 已过期 / 域名不匹配 / 未受信任的根等）。

## 实际入口

- 模块：`certprecheck`（`go.mod`，Go 1.20）
- 核心实现：`precheck.go`
  - `Precheck(domain string, nodes []Node, clients []Client, stages []Stage) Report`
  - `verifyNode` / `translateVerifyError`（内部）

## 验证范围（`precheck_test.go`，Go 内置 testing，本地合成证书夹具）

| 用例 | 覆盖 |
| --- | --- |
| `TestPrecheckAllStagesPass` | 可接受结果：含新旧并存的滚动阶段，全部阶段通过 |
| `TestPrecheckBlockedByUntrustedRoot` | 应被阻止：必需客户端不信任新根，阻塞于新节点上线阶段 |
| `TestPrecheckBlockedByHostnameMismatch` | 应被阻止：证书域名与服务域名不匹配 |
| `TestPrecheckValidityBoundary` | 边界：恰在 NotBefore/NotAfter 通过，前后一秒分别拒绝 |
| `TestPrecheckRollingStagePartialFailure` | 交错：滚动阶段旧节点通过、新节点尚未生效；非必需客户端不阻塞 |
| `TestPrecheckStopsAtFirstBlockingStage` | 交错：报告停在首个阻塞阶段，不泄漏后续阶段 |

## 运行

```sh
go test ./...
```
