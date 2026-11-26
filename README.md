# 跨区域切流错误预算离线核算器

一个纯离线、确定性的 Python 3.11 核算器，用于判断跨区域切流期间各服务的
逻辑请求是否应计入错误预算。无网络访问、无第三方依赖、相同输入必得相同输出。

## 业务规则与建模口径

| 题面要求 | 实现方式 |
| --- | --- |
| 一次最终成功的逻辑请求可免记首跳失败 | 重试链最后一跳成功时，第 1 跳的可用性失败（FAILURE/TIMEOUT）与首跳延迟违约全部豁免（`accounting.evaluate_request`） |
| 超出截止时间的重试不能消除已经发生的违约 | 首跳之后任一跳结算时刻晚于截止时间即记延迟违约，最终成功也保留；后续重试成功不冲销 |
| 不同时钟源有可校对的误差范围 | `ClockSource(epsilon)` 把每次读数映射为参考时钟闭区间 `[t-ε, t+ε]`；截止时间判定输出三态：确定守约 / 确定违约 / 可能违约 |
| 区域间交接不能两边都进独立预算 | 跨区交接两侧共用同一 `request_id`；`merge_reports` 按 id 去重，服务级只计一次。完全重复上报视为重传忽略，内容冲突抛 `DoubleCountingError` |
| 多个观察者的局部区间汇成服务级结果 | 覆盖区间求并集（端点相接视为连续），并输出核算全域内的观测缺口 |
| 输出确定违约、可能违约、剩余额度的边界 | 每个滑动窗口输出 D（确定）、P（可能）、`remaining_best = B-D`、`remaining_worst = B-D-P`，以及状态 OK / AT_RISK / EXHAUSTED |
| 一天内切流在短窗和长窗触发不同处理 | 合成场景中短窗（300s，预算 2）在切流峰值耗尽，长窗（3600s，预算 7）仅进入风险（AT_RISK） |

### 三态与窗口状态语义

- 事件三态（`accounting.ViolationStatus`）：`definite`（含任一确定违约）、
  `possible`（无确定违约但含可能违约）、`none`。
- 一条逻辑请求在一个窗口内**最多消耗 1 个事件额度**；明细违约条数
  （`definite_violations` / `possible_violations`）仍逐条保留用于审计。
- 窗口状态（`windows.WindowStatus`）：
  - `OK`：最坏情况仍有余量，`D + P < B`
  - `AT_RISK`：确定消耗未超预算，但最坏情况会超，`D <= B < D + P`
  - `EXHAUSTED`：确定违约已耗尽/超出预算，`D >= B`
- `remaining_worst` 允许为负，负值表示即使预算耗尽仍存在的超额违约。

## 目录结构

```
sla_accounting/          # 核算器包（仅标准库）
  clocks.py              # 时钟源、观测区间、三态比较与截止判定
  model.py               # Attempt / LogicalRequest / Contract / WindowSpec
  accounting.py          # 单请求核算：首跳豁免、超期重试保留、三态裁决
  aggregation.py         # 观察者区间合并、观测缺口、跨区去重
  windows.py             # 滑动窗口核算、剩余额度边界、全天摘要
  calculator.py          # 离线核算器门面与一天扫描时刻构造
demo/
  scenario.py            # 本地合成的一天切流场景（无外部数据）
  run_demo.py            # 命令行演示
tests/                   # 69 个 unittest 用例
tools/run_isolated.py    # python3 -I 隔离模式运行入口
```

## 公开 API

### 时钟与三态判定

```python
from sla_accounting import ClockSource, Ternary, compare, meets_deadline

clk_a = ClockSource("region-a", epsilon=0.2)
clk_b = ClockSource("region-b", epsilon=0.5)
event = clk_b.observe(102.6)          # ObservedTime，参考区间 [102.1, 103.1]
deadline = clk_b.observe(102.0)       # 参考区间 [101.5, 102.5]
meets_deadline(event, deadline)       # Ternary.FALSE / TRUE / UNCERTAIN
compare(event, deadline)              # 事件是否不晚于目标（同三态）
```

### 领域模型

```python
from sla_accounting import Attempt, LogicalRequest, Outcome, Contract, WindowSpec

attempt = Attempt(region, observer, start, finish, Outcome.FAILURE, deadline)
request = LogicalRequest(request_id, service, origin_region,
                         attempts=(attempt, ...), handed_off=True)
contract = Contract("checkout", (
    WindowSpec("short", length_seconds=300.0, error_budget=2),
    WindowSpec("long",  length_seconds=3600.0, error_budget=7),
))
```

### 单请求核算

```python
from sla_accounting import evaluate_request
verdict = evaluate_request(request)
verdict.status                # ViolationStatus.DEFINITE / POSSIBLE / NONE
verdict.excused_availability  # 被首跳豁免的可用性失败数
verdict.excused_latency       # 被首跳豁免的延迟违约数
verdict.marks                 # 逐跳判定明细（含 deadline_verdict 三态）
```

### 观察者合并与跨区去重

```python
from sla_accounting import ObservationReport, merge_reports
report = ObservationReport(observer, region,
                           covered_intervals=((0.0, 86400.0),),
                           requests=(...))
aggregate = merge_reports("checkout", [report_a, report_b],
                          horizon=(0.0, 86400.0))
aggregate.requests                 # 去重后的逻辑请求
aggregate.coverage / aggregate.gaps
aggregate.duplicate_reports_ignored
# 同 id 内容冲突 -> DoubleCountingError
```

### 离线核算器门面

```python
from sla_accounting import OfflineBudgetCalculator, build_day_sweep

eval_times = build_day_sweep((0.0, 86400.0), tick_seconds=60.0)
result = OfflineBudgetCalculator(contract, horizon=(0.0, 86400.0)) \
    .evaluate([report_a, report_b], eval_times)
result.outcome_at("short", 40200.0)      # 某时刻窗口快照
result.window_summaries["long"]          # 全天摘要（是否曾耗尽/风险、峰值）
```

## 运行方法

要求 Python 3.11（开发与验证版本 3.11.6），无需安装任何依赖。

```bash
# 测试（题面指定命令）
python3 -m unittest discover -s tests -v

# 演示：合成一天切流场景的完整核算输出
python3 -m demo.run_demo

# 隔离模式（忽略 PYTHONPATH/站点包/cwd，防御同名模块注入）
python3 -I tools/run_isolated.py tests
python3 -I tools/run_isolated.py demo
```

## 验证范围（69 个用例，全部实测通过）

- `test_clocks.py`（10）：误差区间构造、负 ε 拒绝、确定先于/晚于、
  区间相交不确定、闭区间相切、跨时钟源误差叠加、区间包含/相交边界。
- `test_accounting.py`（14）：最终成功豁免首跳 FAILURE/TIMEOUT、
  最终失败不豁免、超截止重试违约在成功后保留、重试跳可用性失败计数、
  多跳明细计数、误差带内 POSSIBLE、DEFINITE 优先于 POSSIBLE、
  首跳 POSSIBLE 随成功豁免、模型校验（空重试链、假交接、起点区缺失）。
- `test_aggregation.py`（10）：区间并集（不交/重叠/相切/乱序）、
  观测缺口（内部缺口、全域裁剪、全覆盖）、交接请求双侧上报只计一次、
  重传忽略计数、冲突上报抛 `DoubleCountingError`、跨服务上报拒绝、反向区间拒绝。
- `test_windows.py`（9）：OK/AT_RISK/EXHAUSTED 三态、剩余额度最好/最坏、
  事件随窗口滑出、闭区间端点纳入、POSSIBLE 单独导致风险、全天扫描与摘要。
- `test_calculator.py`（10）：扫描时刻构造与非法参数、门面端到端、
  重名窗口拒绝、零预算口径、观察者覆盖交错缺口、跨时钟源交接链误差带、
  多跳违约只占一个事件额度。
- `test_scenario_e2e.py`（12）：合成一天场景的全链路断言——
  53 条请求去重（9 条交接重传）、全天覆盖无缺口、r1~r10 逐请求裁决、
  切流峰值短窗耗尽 vs 长窗风险、窗口计数与请求级裁决交叉校验、
  切流滑出后预算恢复、43 条基线零消耗、分钟级扫描覆盖全天。

### 合成场景关键结论（实测输出）

切流峰值 t=40200s：

| 窗口 | 确定违约 | 可能违约 | 剩余最好 | 剩余最坏 | 状态 |
| --- | --- | --- | --- | --- | --- |
| short（300s，预算 2） | 5 | 2 | -3 | -5 | EXHAUSTED |
| long（3600s，预算 7） | 5 | 2 | 2 | 0 | AT_RISK |

## 建模假设与边界说明

1. 预算按**违约事件数（绝对值）**计量；一条逻辑请求无论含多少条明细违约，
   在一个窗口内只占一个事件额度，明细条数另存于裁决结果供审计。
2. 事件归属窗口以其各跳结算时刻参考区间的外包络与窗口做闭区间相交判断；
   时钟误差导致的窗口归属不确定性，保守纳入并通过 POSSIBLE 计数体现。
3. 首跳豁免以"重试链最后一跳成功"为最终成功判据；豁免同时覆盖首跳的
   可用性失败与首跳延迟违约（含误差带内的可能违约）。
4. 时钟误差为对称绝对误差且已校对（无漂移建模）；所有时间以统一参考时钟
   秒为单位，输入数据为离线本地合成数据，不读取任何外部凭据或网络目标。
