# capacity-audit：排队耗尽与补发容量对账

一个仅使用 Python 标准库的分析模块，用于容量值班判断压测指标在排队/追偿场景中应如何计量。它接收到达、限速、补发/追偿、服务完成、主动撤回和显式容量丢失事件；支持可控时钟、客户端时钟偏差/误差界限、重复事件去重、序列号缺失检测、排队区间解释、窗口吞吐上下界，以及撤回与容量丢失的分别定位。

运行环境：Python 3.11.6（Python 3.10+ 即可），无第三方依赖。

## 1. 运行与验证

在题目工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成场景并打印中文对账报告：

```bash
python3 -m capacity_audit --demo
python3 -m capacity_audit --demo --json
```

读取自己的 JSON 事件文件：

```bash
python3 -m capacity_audit --events examples/cross_window_reissue.json
python3 -m capacity_audit --events examples/cross_window_reissue.json --json
```

JSON 输入可以是事件数组，也可以是包含 `events`、`window_width`、`exhaustion_at`、`clock_bounds` 等参数的对象。该工具不联网、不上传材料，也不安装依赖。

## 2. 快速 API 示例

```python
from capacity_audit import analyze

events = [
    {"event_id": "arr-1", "kind": "arrival", "timestamp": 9.2, "count": 4},
    {"event_id": "limit-1", "kind": "rate_limited", "timestamp": 9.6,
     "source_arrival_id": "arr-1", "count": 4},
    {"event_id": "rr-1", "kind": "reissue", "timestamp": 10.4,
     "source_arrival_id": "arr-1", "batch_id": "recovery-1", "count": 4},
    {"event_id": "done-3", "kind": "completion", "timestamp": 14.0,
     "source_arrival_id": "arr-1", "count": 3},
    {"event_id": "wd-1", "kind": "withdrawal", "timestamp": 15.0,
     "source_arrival_id": "arr-1", "count": 1},
]

result = analyze(
    events,
    window_width=10.0,
    exhaustion_at=30.0,
    default_clock_bounds=0.02,
)

w0 = result.window(0)
w1 = result.window(1)

# 计数上下界均为 MetricBounds(lower=..., point=..., upper=...)
assert w0.arrivals.point == 4
assert w1.reissues.point == 4
assert w1.completions.point == 3
assert w1.arrivals.point == 0          # 补发不是新的唯一需求
assert result.capacity_losses == []    # 3 完成 + 1 撤回，余额为 0

attribution = result.cross_window_reissues[0]
print(attribution.rule)
print(result.to_dict())                # JSON-friendly
```

也可以使用有状态封装：

```python
from capacity_audit import CapacityAnalyzer, ManualClock

clock = ManualClock(start=0.0)
analyzer = CapacityAnalyzer(window_width=10.0, clock=clock)
# 测试代码可按真实时间推进 clock.advance(...)，再用 clock.now 作为 horizon。
result = analyzer.analyze(events)
```

文本报告：

```python
from capacity_audit import format_report
print(format_report(result))
```

## 3. 公开 API

主要导出位于 `capacity_audit` 包根：

| API | 用途 |
| --- | --- |
| `analyze(events, ...)` | 主入口：摄入、去重、时钟修正、关联、窗口计量、区间和事件定位 |
| `CapacityAnalyzer` | 固定窗口/时钟/客户端误差配置的有状态分析器 |
| `Event` / `Event.from_mapping()` | 标准化事件；可直接传 dict，分析器也会自动转换 |
| `ingest_records()` | 仅执行解析、幂等去重和序列号缺口检测 |
| `ManualClock` | 测试用可控时钟：`advance()`、`set()`、`now` |
| `ClockSpec` / `TimeBounds` | 客户端偏移、误差界限和修正后的时间区间 |
| `WindowGrid` / `MetricBounds` | 半开窗口与 `lower/point/upper` 界限 |
| `AnalysisResult` | `windows`、`intervals`、`withdrawals`、`capacity_losses`、`admission_gaps`、`reissue_attributions`、`cohorts`、`issues`、`summary` |
| `format_report()` | 中文值班报告 |

`analyze()` 的关键参数：

- `window_width`、`window_origin`：窗口宽度和原点，默认宽度 10。
- `clock_bounds`：按 `client_id` 配置 `ClockSpec`、误差浮点数、`[offset, error]` 或 dict。
- `default_clock_bounds`：未配置客户端的默认时钟模型。
- `clock`：可选 `ManualClock`；未传 `horizon` 时使用 `clock.now`。
- `horizon`：观察截止时间；未闭合请求保持 `open`，不会被直接判成丢失。
- `exhaustion_at`（别名 `settle_at`）：排队/追偿耗尽结算边界；此时仍无完成或撤回证据的已知到达会被定位为推断容量丢失或限速后未追偿缺口。若未显式传入，分析器会使用最晚的 `queue_exhausted` 事件的修正点时间。
- `initial_backlog`：已知初始积压数量，仅用于缺失完成报告的乐观上限。

## 4. 事件模型

支持的 `kind` 及常见别名：

| 标准 kind | 别名示例 | 含义 |
| --- | --- | --- |
| `arrival` | `arrived`, `submitted`, `true_arrival` | 真实原始到达/唯一需求 |
| `rate_limited` | `rate_limit`, `throttled`, `rejected` | 限速、限流或准入拒绝信号 |
| `reissued` | `reissue`, `retry`, `recovery_batch`, `backlog_batch` | 积压后的补发/追偿批次 |
| `completed` | `completion`, `service_completion`, `served` | 服务完成 |
| `withdrawn` | `withdrawal`, `cancelled`, `canceled`, `active_withdrawal` | 客户端主动撤回 |
| `capacity_lost` | `loss`, `dropped`, `capacity_loss` | 显式容量丢失/丢弃 |
| `queue_exhausted` | `exhaustion`, `exhausted` | 队列耗尽标记 |

常用字段：

- `event_id` / `id` / `report_id`：幂等 ID。
- `timestamp` / `ts` / `time`：客户端上报时间（数值秒）。
- `client_id` / `client`：客户端标识，用于选择时钟误差界限。
- `request_id`：单请求关联；也可用 `request_ids` 展开批次中的 ID。
- `lineage_id` / `cohort_id`：原始到达批次/请求谱系。
- `source_arrival_id` / `arrival_id` / `source_event_id`：补发、完成、撤回所对应的原始到达事件 ID。
- `batch_id`：补发或终态批次 ID。
- `count`：批次数量，默认 1。
- `seq`、`seq_stream`：客户端序列号和可选流名称；缺省按客户端全流检测缺口。
- `clock_offset`、`clock_error`：事件级时钟修正，优先于全局配置。
- `true_timestamp`：合成数据中的真值/测试预言；若超出推断时间区间会产生 `clock_bound_violation`。

## 5. 补发落到下一窗口时如何计量

核心原则是区分三个账本：

1. **原始需求账本（origin demand）**  
   原始 `arrival` 计入真实到达所在窗口的新增唯一需求和排队积压。即使补发批次在下一窗口才发送，原始需求仍归属原窗口。

2. **物理发送/追偿账本（physical send）**  
   `reissued` 计入补发实际所在窗口的物理发送/重试压力。它解释积压在何时回到系统，但在该窗口的 `new_unique_demand` 为 `0`，不能再次加到 `arrivals`。

3. **服务完成账本（completion throughput）**  
   `completed` 计入实际完成窗口的吞吐。请求来自哪个原始窗口不改变完成窗口归属。

例如原始到达在 W0、4 件全部被限速，补发批次 4 件在 W1，随后 W1 完成 3 件、撤回 1 件：

| 窗口 | arrivals L/P/U | reissues | completions | withdrawals | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| W0 `[0,10)` | 4/4/4 | 0 | 0 | 0 | 4 个唯一需求和积压形成 |
| W1 `[10,20)` | 0/0/0 | 4/4/4 | 3/3/3 | 1/1/1 | 4 是追偿发送，不是 4 个新到达；3 个完成计入 W1 容量 |

`ReissueAttribution.entries` 会同时给出 W0 的 `arrivals` / `backlog_to_be_recovered` 解释分录，以及 W1 的 `reissues` 和 `new_unique_demand=0` 分录。解释分录用于审计，不应被当作另一组可加总事件。

如果时钟误差区间跨过窗口边界，例如补发时间点 10.05、误差 ±0.2，则它可能属于 W0 或 W1：

- W0/W1 的 `upper` 都包含该批次；
- 两个窗口的 `lower` 都不包含；
- `point` 按修正点估计落入 W1；
- `ReissueAttribution.reissue_window.certain_index is None`，`possible_indices == [0, 1]`。

## 6. 时钟误差与窗口上下界

参考时间估计为：

```text
reference_point = reported_timestamp + offset
true_time ∈ [reference_point - error_bound, reference_point + error_bound]
```

窗口采用半开区间 `[start, end)`，因此时间恰好等于边界时属于右侧窗口。

每个指标返回 `MetricBounds(lower, point, upper)`：

- `lower`：整个时钟不确定区间都确定位于该窗口的数量；
- `point`：按修正点时间归属的数量；
- `upper`：不确定区间与该窗口可能相交的数量。

同一跨边界事件可能进入多个窗口的 `upper`，所以各窗口上界之和可能大于事件总数；这是有意保留的保守界限。

`WindowReport` 中：

- `counts["completions"]` 是去重后完成事件的计数界限；
- `reported_throughput` / `settled_throughput` 是完成数除以窗口秒数；
- `throughput` 的下界和点估计同已观测完成率，上界额外加入已知未闭合工作的“完成报告缺失”乐观空间；
- `missing_report_ceiling` 是可能漏掉完成报告的已知请求/批量余额。它是条件上界：假设所有原始到达已知，未知的新到达不在内。

## 7. 重复、缺失与容量丢失判断

### 重复事件

以下报告只计数一次：

- 相同 `event_id`；
- 没有事件 ID 但规范化业务字段指纹相同；
- 同一 `request_id` 的重复到达或重复同类终态报告。

相同 `event_id` 但业务字段冲突时保留第一份并产生 `event_id_conflict`。同一请求同时出现完成和撤回时产生 `terminal_conflict`；主账本不武断二选一，窗口上界保留两种终态可能。

### 缺失事件

- `seq` 跳号会生成 `sequence_gap` issue，只报告缺失报告数，不虚构事件内容或数量。
- 缺少原始到达但出现终态时，生成 `missing_arrival_evidence`，队列区间标记为 `missing_arrival_evidence`。
- 已知到达没有完成事件时，`missing_report_ceiling` 给出吞吐上界敏感性。
- 只提供 `horizon` 而未提供 `exhaustion_at` 时，未终态请求保持 `open_observed_to_horizon`，不直接判为丢失。

### 主动撤回 vs 容量丢失 vs 接入缺口

- **主动撤回**：来自 `withdrawn` 事件，生成 `result.withdrawals`。它表示需求主动离开队列，不是服务完成容量，默认也不是服务端容量丢失。
- **显式容量丢失**：来自 `capacity_lost` / `dropped` 事件，按事件时间定位。
- **推断容量丢失**：在 `exhaustion_at` 后，已知原始到达仍无完成、撤回或显式丢失终态，并且没有“限速后完全未补发”的相反证据；生成 `result.capacity_losses`，定位在耗尽边界窗口。
- **限速后未追偿缺口**：若请求或批次有限速证据、却没有补发批次和终态，单列到 `result.admission_gaps`，定位在限速事件窗口。它是接入/客户端积压缺口，不记作主动撤回，也不武断等同于服务端容量丢失。补发后仍无终态的余额则归为容量丢失。

## 8. 可解释排队区间

`result.intervals` 包含两类：

- `subject_type="request"`：按 `request_id` 关联的单请求区间；
- `subject_type="cohort"`：按 `lineage_id` 的批次余额区间。

每个区间包含：

- `start_bounds` / `end_bounds`；
- `duration_bounds.lower/point/upper`；
- 起止窗口分配；
- `status`，如 `completed`、`withdrawn`、`capacity_lost`、`rate_limited_unrecovered`、`open`、`missing_arrival_evidence`；
- `event_ids`、`reissue_event_ids`、`rate_limit_event_ids`；
- `spans`，例如从原始到达到每次补发时的积压年龄；
- 中文 `explanation`。

时长下界使用“最早结束 − 最晚开始”，上界使用“最晚结束 − 最早开始”；因果顺序导致的负下界会截断为 0。

## 9. 验证范围

`tests/` 中的本地合成测试覆盖：

- `ManualClock` 推进、设置和非法回退；
- 客户端 offset/error bound 与真值区间；
- 相同事件 ID、相同 payload、请求级重复终态和冲突终态；
- 非法事件行生成 issue，但不阻断其他事件；
- 序列号缺口检测；
- 半开窗口边界和跨边界时钟不确定性的 L/P/U；
- 缺失完成报告的 tracked upper ceiling；
- 完成、撤回、推断容量丢失、显式容量丢失；
- 限速后未补发归入 admission gap，补发后未终态归入容量丢失；
- 聚合批次 mass balance 和无名容量丢失定位；
- 缺失原始到达；
- 单批和分批追偿在下一/后续窗口的分别计量；
- 一个双客户端、含时钟偏差、重复报告、序列缺失、成功、撤回、限速、跨窗口补发和容量丢失的交错端到端场景；
- `examples/cross_window_reissue.json` 和 JSON 可序列化。

最终实际执行：

```text
python3 -m unittest discover -s tests -v
```

结果以当次执行输出为准；编写 README 时的本地完整运行为 27 个测试全部通过。
