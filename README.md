# 事件匹配模块（关联规则引擎）

检测“敏感读取后五分钟内发生外传，且两者之间没有有效批准”这类关联规则的本地实现。
纯本地、纯合成数据，无外部服务依赖。

## 文件

- `src/event_matcher.mjs` — 事件匹配引擎 `EventMatcher`
- `tools/local_verify.mjs` — 本地核验入口（运行入口，不是独立合格证明）

## 规则模型

规则通过独立的发布记录进入引擎，按 `(ruleId, version)` 版本化：

```js
{
  subjectKey: ['user'],                              // 主体键：由事件字段拼接
  sequence: [{ type: 'sensitive_read' }, { type: 'external_send' }], // 正向事件序列
  maxIntervalMs: 300_000,                            // 相邻步骤最大间隔
  absence: [{ type: 'approval' }],                   // 区间内不应出现的事件
  allowedLatenessMs: 60_000                          // 规则级允许迟到（可选）
}
```

输入事件必须携带：`id`（事件 ID）、`type`、`eventTimeMs`（事件时间）、
`arrivalTimeMs`（到达时间），以及主体键字段（如 `user`）。

## 语义要点

- **显式水位线**：`advanceWatermark(ts)` 单调推进。暂定结果在其缺席窗口
  `(startMs, endMs]` 被水位线闭合（`watermark > endMs`）后才定案为 `confirmed`；
  闭合之前只可能产生 `tentative` / `revoked`。
- **缺席条件**：窗口内到达的缺席事件撤销暂定匹配；候选未完成时，晚于首步的
  缺席事件保守地撤销该候选（见“未覆盖边界”）。
- **允许迟到**：`eventTimeMs <= watermark` 的记录为迟到；迟到量不超过允许值
  （引擎级与当前规则级取最大）仍被处理，可撤销暂定匹配；超过则进入
  **迟到通道**（`getLateChannel()`），不再生效。
- **迟到批准撤销**：允许迟到范围内的批准到达后撤销对应主体的暂定匹配，
  撤销记录标记 `late: true`。
- **多候选共享事件**：一个事件可推进/完成多个尚未闭合的候选。
- **重复 / 冲突 ID**：同 ID 同内容重投为 no-op（不产生新候选）；同 ID 不同
  内容被隔离进 `getConflicts()`，不参与匹配。
- **规则版本**：已有候选按其创建时的版本快照续接；新候选只按当前版本
  （已发布的最大版本号）创建。
- **状态预算**：`maxActiveCandidates` 限制活跃候选数。超预算时：
  - `reject`（默认）：不建候选，产生 `undetermined`（未能判断）结果；
  - `evict-oldest`：丢弃最旧候选，产生 `dropped` 记录。
  两者都附参与记录；`getRiskSummary()` 将其计入 `riskUnknown`，结论为
  `risk-unknown`，**绝不把丢弃/未判断候选当作无风险**。

## API

| 方法 | 说明 |
| --- | --- |
| `new EventMatcher({ maxActiveCandidates, onBudgetExceeded, allowedLatenessMs, initialWatermarkMs })` | 构造引擎 |
| `publishRule({ ruleId, version, definition, publishedAtMs })` | 发布规则版本；当前版本取最大版本号 |
| `ingest(event)` | 摄入事件，返回 `{ accepted, duplicate?, conflict?, late? }` |
| `advanceWatermark(tsMs)` | 推进水位线（单调），定案并过期候选 |
| `getResults({ status, subjectKey, ruleId }?)` | 结果快照（tentative/confirmed/revoked/undetermined/dropped，含参与记录与历史） |
| `getActiveCandidates()` / `getLateChannel()` / `getConflicts()` / `getCandidateAudit()` / `getPostFinalizationNotices()` / `getInvalidRecords()` | 审计视图 |
| `getRiskSummary()` | 风险汇总；`riskUnknown > 0` 时结论为 `risk-unknown` |

## 核验命令

```bash
node tools/local_verify.mjs --list          # 列出用例
node tools/local_verify.mjs --case C01      # 单用例，输出 JSON 断言记录
node tools/local_verify.mjs --case ALL      # 全部用例
```

任一断言失败、入口缺失、未实现、零断言或整组跳过均非零退出。
输出含 `case`、`assertions`、`passed/failed/skipped`、`seed`、可重放
`samples` 及逐条期望/观察摘要；全部为合成数据，不含真实敏感内容。

## 可重放样本（seed = 20261119，基准时间 T0 = 1700000000000）

- **C01**：u1 read@0 + send@100s（间隔内成匹配）；u2 send@400s（超 300s 间隔）；
  u3 read 后 approval@50s（候选被撤销）；u4 暂定匹配被窗口内 approval@60s 撤销；
  u1 窗口外 approval@150s 无影响；水位线推进到 300s 前无 confirmed，之后 u1 定案。
- **C02**：u1 read@0 + send@240s，水位线 200s 时批准@180s 迟到 20s（≤60s 允许）
  撤销暂定；u2 批准@100s 迟到 100s（>60s）进入迟到通道，窗口闭合后 u2 定案。
- **C03**：r1 重投（重复，不建候选）与同 ID 不同内容（隔离）；发布 v2
  （间隔收紧为 100s）后，旧候选 A 按 v1 在 150s 处完成，新候选 B 按 v2 在 95s
  处完成；send@20s 同时完成 u2 的两个未闭合候选。
- **C04**：预算 2 + `reject`：第三个 read 产生 `undetermined`（含参与记录 r3）；
  预算 1 + `evict-oldest`：旧候选被驱逐产生 `dropped`（含参与记录 a1）；
  两种情况下风险汇总均为 `risk-unknown`。

## 未覆盖边界（审计时需保留未知）

- **已定案结果的迟到缺席事件**：`confirmed` 为终态；定案后到达的窗口内缺席
  事件只记入 `postFinalizationNotices`，不改判。是否允许翻案未定义。
- **候选未完成时的缺席判定是保守的**：只要缺席事件时间晚于候选首步即撤销，
  对乱序到达（send 事件时间早于批准但到达更晚）可能误撤。
- **单分区假设**：无并发摄入、无持久化、无崩溃恢复；状态全在内存。
- **允许迟到取值**：引擎级与**当前**规则版本取最大；历史版本候选的迟到
  容忍度不按其创建版本回溯。
- **到达时间仅作审计**：迟到判定基于事件时间与水位线，不用到达时间。
- **主体键缺失**：事件缺主体键字段时进入 `invalidRecords`，不参与匹配。
- 核验包装器只是运行入口；审计仍应阅读代码与真实日志，对不能证实的内容
  保留未知。
