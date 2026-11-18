# 出站传输判定模块（relaydecision）

邮件中继的**出站传输判定引擎**。模块本身不发起任何网络连接，只消费四类输入并
为本地中继适配器返回单一的下一步指令：

| 输入 | 类型 | 说明 |
| --- | --- | --- |
| 目的域加密要求 | `DomainPolicy`（`TLSNone` / `TLSOpportunistic` / `TLSRequired`，带单调 `Version`） | 决定候选必须满足的加密等级 |
| 候选中继优先级 | `[]Relay`（`Priority` 升序，同名次按名称排序） | 候选列表按优先级排序后逐轮消费 |
| 会话握手结果 | `ReportHandshake(...)`（成功 / 身份校验失败 / 握手期无 TLS / 连接失败） | 由本地适配器回报，引擎只记录不探测 |
| 等待重试截止时间 | `EnqueueRequest.Deadline` | 硬边界，`now == deadline` 即终止 |

## 实际入口（包 `relaydecision`，文件 `relaydecision.go`）

- `NewEngine()` / `NewEngineWithClock(now, backoffBase, backoffMax)`：构造引擎；测试用注入时钟。
- `ApplyPolicy(DomainPolicy) error`：登记或替换域策略，版本号不允许回退。
- `Enqueue(EnqueueRequest) error`：入队一条传输并按优先级固化候选顺序。
- `Evaluate(id string) (Decision, error)`：**核心入口**。每次调用只返回一种结果：
  - `KindNext` + `Relay`：尝试该候选（`Reason` 区分首选 / 回落 / 策略重检 / 新重试轮次）；
  - `KindDefer` + `NotBefore`：暂缓（握手中、退避未到、本轮候选耗尽）；
  - `KindTerminate`：终止（已建立、截止时间到期、无任何可满足要求的候选）。
  - `Decision.Basis` 给出人类可读依据，`Reason` 为稳定机器码，`Attempts` 为历史副本。
- `ReportHandshake(id, outcome, detail) error`：回报当前在途候选的握手结果并推进游标。
- `PendingRechecks() []string`：列出策略版本已落后、尚未建立的传输。
- `Snapshot(id) (Transmission, error)`：返回与引擎状态隔离的快照。

## 关键判定规则（与题面交叉约束对应）

1. **要求加密的域，首选中继不支持加密 → 跳过并记录 `relay_lacks_required_tls`，
   下一候选仍按同一 `TLSRequired` 要求筛选**，任何候选上都不降级为明文。
2. **候选提供 TLS 但身份校验失败** → 记录 `identity_verification_failed`（含 detail），
   同一候选本轮不再重试，要求不降低，游标推进到下一候选。
3. **域策略收紧**（如 opportunistic → required，版本号递增）→ 队列中尚未建立的传输在
   下次 `Evaluate` 时从最高优先级候选重新选择、轮次归零、历史清空；若当时有在途握手，
   先返回 `handshake_in_flight`，待回报后再重检。
4. **重试**：一轮候选全部失败后按指数退避（30s 起、翻倍、10m 封顶）安排下一轮；
   下一轮起点若落在截止时间或之后，立即以 `retry_deadline_exceeded` 终止，不排不可达重试。
5. **机会加密**（`TLSOpportunistic`）/ 无要求（`TLSNone`）允许明文候选，
   握手期 TLS 不可用时记录 `plaintext_fallback_allowed`；该原因在 required 域永不出现。
6. 所有对外返回的切片与指针均为副本，适配器侧修改不污染引擎状态；引擎带互斥锁，可并发使用。

## 验证范围与运行方式

```
go test ./...            # 验收入口，全部真实断言
go test ./... -race -v   # 竞态检测 + 详细输出（已在 go1.20.2 darwin/arm64 通过）
```

测试文件 `relaydecision_test.go`（标准库 `testing`，无第三方依赖，注入虚拟时钟保证确定性），
15 个用例覆盖三类必备场景：

- **可接受结果**：required 域首选无 TLS 被拒后回落至满足要求的候选、身份失败后继续回落、
  最终握手成功并以 `transmission_established` 终止；opportunistic/none 域接受明文候选。
- **应被阻止/拒绝的结果**：所有候选均不支持 required TLS → `no_eligible_candidate` 终止且
  每个候选都有拒绝记录；握手中 TLS 能力缺失不降级；入队/状态机/策略版本回退等非法输入报错。
- **边界与交错场景**：
  - 策略收紧落在在途握手期间（先 defer，回报后重检并从队首选起）；
  - 退避窗口内重复评估、窗口开启后新轮次；
  - `now == deadline` 等值边界即终止；下一轮退避越过截止时间时立即终止；
  - 优先级同值按名称排序；退避序列在 10m 处封顶；
  - 返回值/快照与引擎状态隔离；并发评估无数据竞争。

## 范围与限制

- 纯本地判定库，不进行任何真实目标探测、连接或证书校验；握手结果由本地适配器以
  `ReportHandshake` 喂入，模块只做防御性校验、状态记录与决策。
- 无外部依赖，`go.mod` 仅声明模块名与 `go 1.20`。
