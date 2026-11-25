# sampling — 终端代理采样许可模块

面向共享终端上的远程诊断场景：客服发起诊断时，终端上可能仍有他人的后台进程。
本模块根据**进程实例的启动用户与登录编号**、**终端前台切换事件**、**用户已同意的
资料类型与时间区间**，把一段采样计划解析为：每个进程在哪些子区间可采哪些字段。

纯本地库，无外部依赖，不访问网络或任何真实目标；所有验证均使用合成数据。

## 许可规则（防御性判断）

1. **机器级同意**（`ScopeMachine`）只放行设备健康字段（`device.*`)，绝不解锁进程级数据。
2. **进程级字段**(`process.*`）需要**实际启动者**的用户级同意（`ScopeUser`,
   `Subject == Process.StartUser`)；同意可选地绑定登录编号（`LoginID`)，只覆盖该会话的进程。
3. **撤回**(`Revocation`）使计划从撤回时刻起收窄：可按同意 ID 撤回单条，或按用户
   撤回其授予的全部同意（不影响他人授予的同意）。
4. **前台切换**(`ForegroundSwitch`）作为输入被接收，但**不改变后台进程的归属**:
   后台进程仍归原启动者，新前台用户的同意不会覆盖它，原启动者的同意在切换后依然有效。

## 公开 API

| 名称 | 说明 |
| --- | --- |
| `BuildPlan(procs, switches, consents, revocations, plan) Result` | 核心入口：解析采样计划 |
| `Consent` / `Scope`(`ScopeMachine`、`ScopeUser`) | 同意记录及其范围 |
| `Revocation` | 撤回事件（`ConsentID` 指定单条；为空则按 `By` 撤回该用户全部授予） |
| `Process` | 进程实例：`PID`、`StartUser`、`LoginID` |
| `ForegroundSwitch` | 前台用户切换事件（不参与归属判定） |
| `Interval`(`Empty`、`Intersect`) | 半开区间 `[Start, End)` |
| `Result` / `ProcessPlan` / `FieldWindow` | 输出：机器级窗口 + 每进程的子区间与字段集 |
| `DataField`、`IsMachineField` | 字段类型与机器级判定（`device.*` 前缀） |

内置字段常量：`FieldDeviceHealth`、`FieldProcessCPU`、`FieldProcessMemory`、`FieldProcessCmdline`。

### 用法示例

```go
res := sampling.BuildPlan(procs, switches, consents, revocations,
    sampling.Interval{Start: planStart, End: planEnd})
for _, w := range res.Machine { /* 设备健康可采子区间 */ }
for _, p := range res.Processes {
    for _, w := range p.Windows { /* 进程 p.PID 在 w.Interval 内可采 w.Fields */ }
}
```

## 运行方法

需要 Go 1.20（开发验证使用 go1.20.2):

```sh
go test ./...        # 运行全部测试
go test ./... -v     # 查看逐条用例
```

## 验证范围（均已实测通过，13 个用例）

- **正常场景**：机器级同意覆盖设备健康；启动者同意覆盖进程字段子区间
  （`TestBuildPlanNormalScenario`)。
- **边界**：同意区间被计划窗口两端裁剪（`TestConsentClippedToPlanWindow`);
  撤回时刻恰在同意起点（同意完全失效）与恰在终点（无影响）
  （`TestRevocationBoundaries`)；同意完全在计划外、零长计划窗口、无任何同意
  （`TestEmptyOutcomes`)。
- **撤回收窄**：计划中途撤回，进程与机器窗口分别从撤回时刻截断
  （`TestRevocationNarrowsPlan`)；按用户撤回只影响其本人授予的同意
  （`TestRevocationByUserOnlyAffectsOwnGrants`)。
- **前台切换**：切换后后台进程保留原归属——新前台用户的同意不覆盖它，原启动者
  同意继续有效（`TestForegroundSwitchPreservesOwnership`)；机器级同意跨切换
  保持有效，且机器/用户同意均不能越权放行对方层级的字段
  （`TestMachineConsentAcrossSwitchAndFieldScope`)。
- **登录编号**：绑定会话的同意只覆盖同会话进程（`TestLoginSessionScoping`)。
- **交错情况**：两条区间交叠、字段不同、撤回时刻不同的同意产生精确的分段
  （`TestInterleavedConsentsAndRevocations`)；相邻同字段同意合并为单一窗口
  （`TestAdjacentConsentsMerge`)。
- **归属防护**：其他用户的同意绝不覆盖本进程（`TestOtherUserConsentNotApplicable`);
  字段层级判定（`TestIsMachineField`)。

所有测试数据均为本地合成；未读取任何凭据，未联网访问真实目标，未安装外部依赖。
