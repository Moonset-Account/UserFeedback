# capacitycontroller

基于**实例计数差（代次/生命周期）**与**忙碌时间占比**的容量伸缩判定器，纯 Go 1.20.2
标准库实现，无外部依赖。

## 判定规则

1. **代次隔离**：每条观测携带 `Instance / Gen / 区间 [Start,End) / Rev` 及区间内
   `Requests / Busy` 增量。重启产生新代次，旧代次的观测若覆盖新代次在线时间，
   会被标记为 `contradiction`，不能填补新代次缺口。
2. **窗口核验**：窗口长度固定（默认 10 个时间单位）。控制器按每个实例代次的实际
   在线时段（up/down 生命周期合并后的半开区间）裁剪窗口，检查观测区间是否
   *完整且互不重叠*地覆盖全部在线时间：
   - 缺片 → `gap`
   - 观测重叠 → `overlap`
   - 跨窗边界、越出在线时段、忙碌超过区间长度 → `contradiction`
   - 窗口内无任何在线实例 → `no-online`

   存在任一问题时窗口结论为 `unverifiable`，并在 `Verdict.Problems` 中逐条说明
   哪些实例/代次/子区间暂不能核验。
3. **水位比较**：可核验窗口的利用率 = 总忙碌时间 / 总在线时间（以 permille 整数
   表示，四舍五入）。`>= 高水位（默认 800‰）` 为 `high`，
   `<= 低水位（默认 300‰）` 为 `low`，其余为 `normal`。边界值按包含语义处理。
4. **连续段**：扩容需要连续 **2** 个高压窗，缩容需要连续 **3** 个低压窗。
   `normal` 与 `unverifiable` 都会打断连续段——缺证据无法把两段压力拼起来。
5. **动作与冷却**：动作发出后需 `Acknowledge` 回执确认，**冷却从回执时刻起算**
   （默认 50 个时间单位）。存在未确认动作或处于冷却期时，到期的动作被保留为
   blocked 决策，并在之后每次 `Pump` 重试，到达第一个合法时刻刷出；动作号单调，
   已确认动作不会重发，重复/迟到回执被拒绝。
6. **迟到更正**：同一 `(Instance, Gen, Start, End)` 用更大的 `Rev` 替换旧记录；
   平级或更旧修订被拒绝。更正可补齐**尚未发布**的窗口（`Preview` 先看到缺片、
   更正后可核验）；已冻结窗口的结论不回滚、不重发动作，但更正数据仍被保留。

## 公开 API

| 符号 | 说明 |
| --- | --- |
| `New(cfg Config) (*Controller, error)` | 构造控制器（`DefaultConfig()` 提供默认参数） |
| `(*Controller).IngestObservation(Observation) error` | 摄入/修订一条观测 |
| `(*Controller).RegisterLifecycle(Lifecycle) error` | 注册 up/down 事件，自动合并在线段 |
| `(*Controller).OnlineSpan(instance, gen, start, end) error` | 便捷注册完整在线时段 |
| `(*Controller).Pump(now) []Decision` | 发布所有已关闭未发布窗口，必要时刷出 blocked 动作 |
| `(*Controller).Preview(now) (Verdict, error)` | 不冻结地预演下一窗口（可观察迟到更正效果） |
| `(*Controller).Acknowledge(actionID, at) error` | 回执确认，冷却从 `at` 起算 |
| `(*Controller).FrozenVerdicts() []Verdict` | 已发布窗口结论（含问题明细） |
| `(*Controller).Actions() []Action` | 全部动作（含 `IssuedAt/AckedAt`） |
| `(*Controller).BlockedDecision() *Decision` | 当前到期但被扣留的动作 |
| `(*Controller).PendingActionID() int64` | 未确认动作号（0 表示无） |
| `(*Controller).CooldownUntil() int64` | 最早可再次发出动作的时刻 |
| `(*Controller).Streaks() (high, low int)` | 当前连续高/低压窗计数 |

所有导出方法均带互斥锁，可并发调用。

## 采样事件序列（主测试 `TestSampledRestartCorrectionAndLateAck`）

时间单位为抽象 tick，窗口长度 10，高/低水位 800‰/300‰，冷却 50。

- 生命周期：`A gen0 [0,24)`，重启后 `A gen1 [24,50)`；`B gen0 [0,80)`。
- W0/W1 均为 800‰ 高压 → `Pump(22)` 发出 **动作 #1 scale_out@22**，等待回执。
- W2 跨越 A 的重启：A1 的 `[26,30)` 缺片；同时迟到一条 **A 旧代次 gen0** 的
  `[26,30)` 观测，被标 `contradiction`，不能补缺。`Preview(34)` 与 `Pump(34)`
  均为 `unverifiable`，连续段中断，不产生第二个动作。
- 随后到达 A1 `[24,26)` 的 rev2 更正与缺失的 `[26,30)` 记录；W2 已冻结故结论不变，
  但数据被保留；已确认动作不重发。
- W3/W4 再次连续高压（900‰），但 #1 无回执、随后 `Acknowledge(1, 46)` 启动冷却
  `[46,96)`，扩容决策被 blocked。
- W5/W6/W7 连续低压（300‰）→ 缩容决策同样被冷却 blocked；反向压力先清除旧的
  blocked 扩容候选。
- `Pump(90)`：W8 无在线实例 → `no-online`/`unverifiable`，不发动作。
- **`Pump(96)`：首次可再次调整的时刻，blocked 的 scale_in 作为动作 #2 刷出**
  （`IssuedAt=96`，`Flushed=true`）。

## 复现与验证

```bash
go test ./... -v          # 全部功能测试（含真实断言）
go test -race ./...       # 并发交错竞态检测
go test ./... -count=5    # 稳定性重复运行
gofmt -l .                # 格式检查（无输出即通过）
```

### 验证范围

| 测试 | 覆盖的验收场景 |
| --- | --- |
| `TestSampledRestartCorrectionAndLateAck` | 跨实例重启代次隔离、旧代矛盾、迟到更正、扩容回执迟到、冷却起止、blocked 刷出、动作号与最早再调整时刻 |
| `TestMissingEvidenceBreaksStreak` | 缺片导致 `unverifiable` 并打断高压连续段 |
| `TestOverlapAndGenerationContradiction` | 重叠、旧代覆盖新代时间、部分缺片的问题明细 |
| `TestWatermarkBoundaries` | 800‰/300‰ 边界包含语义 |
| `TestScaleInRequiresThreeLows` | 三窗低压才缩容、回执前不启动冷却 |
| `TestRevisionRules` | 平级/过期修订拒绝、有效修订在发布前替换 |
| `TestLateCorrectionFillsUnpublishedWindow` | 迟到更正补齐未发布窗口 |
| `TestAcknowledgeRules` | 未知/回溯回执拒绝、冷却只在有效回执后开始 |
| `TestConcurrentInterleaving` | 并发摄入/生命周期/预览下的互斥与计数一致性（`-race`） |

所有数据均为工作区内合成事件，不访问网络、不读取外部凭据。
