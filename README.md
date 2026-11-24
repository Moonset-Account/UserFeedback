# 本地时间作业控制器（含 DST 换表语义）

实现一个以**当地挂钟时间**配置每日定时作业的控制器，正确处理夏令时
（DST）换表：

- **秋令时回拨（歧义时刻）**：同一挂钟时刻（如 01:30）在一个夜晚出现
  两次，控制器把每一次实际出现分别作为一个独立候选时刻；
- **春令时前拨（缺口时刻）**：计划挂钟时刻被直接跳过、当天不存在，
  控制器不产生任何实例，只产出一条 `GapNotice` 审计信息；
- **重启补看**：重启后在日程允许的历史窗口 `lookback` 内补算，候选范围
  为闭区间 `[max(last_confirmed_utc, now_utc - lookback), now_utc]`；
- **禁并发**：`allow_concurrent=False` 的日程遇到仍在运行（或创建请求
  已发出未确认）的*另一个挂钟时刻*的实例时，按时刻记录
  `SKIPPED_CONCURRENCY` 跳过；
- **幂等创建标识**：创建标识由 `(schedule_id, 计划 UTC 时刻)` 用
  `uuid5` 确定性派生。控制器在发出创建请求后立即重启、重新计算同一
  时刻时会得到完全相同的标识，运行端据此识别重复请求（重放），不会
  二次创建。

## 目录结构

```
job_controller/
  __init__.py     公开 API 汇总导出
  time_map.py     LocalTimeMap 协议、SyntheticZone、ZoneInfoLocalTimeMap
  store.py        RunStatus、RunRecord、RunStateStore 协议、内存实现
  controller.py   Schedule、Decision 族、GapNotice、JobController、creation_id_for
tests/
  synthetic.py                  确定性合成时区与测试常量
  test_time_map.py              映射语义（普通/歧义/缺口）+ 真实 zoneinfo 适配
  test_controller.py            控制器规划、重启幂等、禁并发、确认点、校验
  test_zoneinfo_integration.py  基于 America/New_York 的端到端测试
```

无第三方依赖，仅使用 Python 3.11 标准库（`dataclasses`、`datetime`、
`uuid`、`enum`、`zoneinfo`、`unittest`）。

## 公开 API

### 时间映射 `job_controller.time_map`

| 名称 | 说明 |
| --- | --- |
| `LocalTimeMap` | 协议：`resolve(local_wall_time) -> tuple[UTC, ...]`（0/1/2 个）与 `to_local(utc) -> naive local` |
| `SyntheticZone(name, initial_offset, transitions)` | 由 `Transition(at_utc, new_offset)` 列表定义的合成时区，测试可精确控制换表 |
| `ZoneInfoLocalTimeMap(key)` | 标准库 `zoneinfo` 适配器，如 `ZoneInfoLocalTimeMap("America/New_York")` |

`resolve` 约定：输入 naive 当地挂钟时间；返回带 `tzinfo=utc` 的时刻；
普通时刻 1 个、回拨歧义 2 个（按真实发生时间升序）、前拨缺口 0 个。

### 状态存储 `job_controller.store`

| 名称 | 说明 |
| --- | --- |
| `RunStatus` | `CREATE_REQUESTED / RUNNING / SUCCEEDED / FAILED / SKIPPED_CONCURRENCY`，含 `is_terminal`、`in_flight` 属性。换表缺口不产生记录，故无对应状态 |
| `RunRecord(schedule_id, scheduled_utc, status, creation_id=None, note=None)` | 单个计划时刻的运行记录，键为 `(schedule_id, scheduled_utc)` |
| `RunStateStore` | 协议：`get` / `list_for_schedule` / `put` |
| `InMemoryRunStateStore` | 内存实现；跨控制器实例保留即模拟重启恢复 |

### 控制器 `job_controller.controller`（包根目录均可直接导入）

```python
from datetime import datetime, time, timedelta, timezone
from job_controller import (
    JobController, Schedule, InMemoryRunStateStore,
    ZoneInfoLocalTimeMap, creation_id_for,
)

store = InMemoryRunStateStore()
controller = JobController(store, ZoneInfoLocalTimeMap("America/New_York"))
schedule = Schedule(
    schedule_id="daily-report",
    local_time=time(1, 30),          # 当地挂钟时间
    allow_concurrent=False,
    lookback=timedelta(days=3),      # 重启补看窗口
)

result = controller.plan(
    schedule,
    now_utc=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
    last_confirmed_utc=datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc),
)
for decision in result.decisions:      # Create / Recreate / Skip
    ...
for gap in result.gap_notices:         # 前拨跳过的挂钟时刻
    ...
```

决定类型：

- `CreateDecision(kind="create")`：新时刻，已落盘 `CREATE_REQUESTED`，
  携带 `creation_id`；
- `RecreateDecision(kind="recreate")`：重算到已发请求但未确认的时刻，
  返回**同一** `creation_id` 供运行端去重；
- `SkipDecision(kind="skip")`：`reason` 为 `running`（本时刻仍在运行）、
  `concurrency`（被另一挂钟时刻的在途实例阻塞，已落盘
  `SKIPPED_CONCURRENCY`，含 `blocking_scheduled_utc`）或
  `terminal:<status>`（终态不重放）。
- `creation_id_for(schedule_id, scheduled_utc)`：确定性创建标识，
  可独立调用。

### 关键语义约定

1. **同挂钟时刻豁免并发**：回拨夜晚同一挂钟时间的两次实际出现视为同一
   逻辑触发，前一遍在途不会阻塞后一遍；只有*不同挂钟时刻*的在途实例
   才会阻塞（见 `JobController._find_in_flight_blocker`）。
2. **缺口不落运行记录**：不存在的挂钟时刻没有实际实例，仅通过
   `GapNotice` 暴露；重算结果稳定、不会产生重复创建。
3. **跳过即终态**：`SKIPPED_CONCURRENCY` 落盘后即使阻塞实例随后结束，
   也不会追溯补建（避免在窗口内重复触发）。
4. 控制器本身无状态，持久化全部委托给 `RunStateStore`；传入任意实现
   即可对接真实数据库。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围

全部基于本地合成数据，无联网、无外部服务、无第三方依赖。共 36 个
测试用例，覆盖：

**时间映射（`test_time_map.py`）**
- 普通时刻唯一映射；回拨歧义映射为两个按时间升序的 UTC；前拨缺口返回
  空元组；换表瞬间偏移边界；`to_local` 回转一致性；naive/aware 入参
  校验；合成时区构造校验；
- 真实 `America/New_York`（2026 年规则）的歧义、缺口、普通时刻与
  `to_local`（无 tzdata 环境自动跳过）。

**控制器（`test_controller.py`）**
- 正常每日调度与创建落盘；补看窗口两端闭区间；`lookback` 限制历史
  重放；
- “发出创建请求后立即重启”：新控制器实例 + 保留存储，重算得到
  `RecreateDecision` 与相同 `creation_id`，且不产生第二条记录；
  `creation_id` 对相同时刻稳定、对不同日程/时刻不同；
- 禁并发：`RUNNING` 与 `CREATE_REQUESTED` 均占用名额，阻塞按时刻记录
  `SKIPPED_CONCURRENCY` 并标注阻塞来源；允许并发时不阻塞；
- 回拨日每个实际出现各一个候选、标识互不相同；同挂钟时刻两遍互不阻塞；
  不同挂钟时刻的在途实例同时阻塞两遍；
- 前拨日无候选、仅 `GapNotice`、无运行记录，且重复规划结果稳定；
- 最近确认点裁剪历史；所有终态（成功/失败/并发跳过）不重新创建；
  多日交错状态及阻塞解除后的终态稳定性；
- 入参校验：空日程 ID、非正 `lookback`、naive 时刻、确认点晚于当前、
  在途记录缺少 `creation_id` 等。

**真实时区端到端（`test_zoneinfo_integration.py`）**
- `America/New_York` 回拨日创建两个实例、前拨日仅审计通知、重启后
  创建标识保持不变。

## 实测结果

```
Ran 36 tests in 0.015s

OK
```

（在 Python 3.11.6 / macOS 上以 `python3 -m unittest discover -s tests -v`
实际执行通过。）
