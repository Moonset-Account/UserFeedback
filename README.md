# 带旋转延迟的磁盘读调度仿真器

纯 Python 3.11 标准库实现，无第三方依赖。仿真一个持续旋转的盘片与一个
可径向移动的磁头，对带到达时刻与最迟完成时刻的读请求执行三层读调度，
并产出逐事件时间线。

## 计时模型（全部为整数 tick）

- **寻道**：耗时 `|柱面差| × seek_ticks_per_cylinder`，与旋转无关。
- **盘片恒速旋转**：一圈 `sectors_per_track × ticks_per_sector` 个 tick。
  扇区 `s` 的起始边界出现在 `(k × sectors_per_track + s) × ticks_per_sector`
  时刻。**寻道期间盘片继续转动**，寻道结束时磁头下方扇区由结束时刻本身
  决定（`head_sector_at(t) = (t // ticks_per_sector) % sectors_per_track`）。
- **旋转延迟**：寻道结束后等目标起始扇区转到磁头下；恰好在扇区起始边界
  到达时延迟为 0。
- **读取**：耗时 `length × ticks_per_sector`，一旦开始不可中断、不可抢占。
- **刚好错过（sector_miss）**：寻道结束时目标扇区的结束边界刚过去
  （旋转延迟 ≥ 一圈 − 一个扇区槽），需几乎再等一整圈，时间线给出
  `sector_miss` 事件。
- **截止期**：完成时刻 `complete ≤ deadline` 视为按时，相等也算按时。

## 调度规则

决策点为仿真开始时刻与每次读取完成时刻，并先吸收该时刻之前（含）的全部
到达请求；候选仅包含**已到达且在队列中**的请求，未来到达不参与当前可行性
判断。

1. **普通首选（SCAN）**：沿当前磁头方向，优先服务前进方向上的请求
   （无则整体反向），同方向取最近柱面；同柱面取最早能转到的起始扇区，
   再以读取开始时刻、rid 兜底。
2. **截止期保护**：若普通首选会使已到请求失去“全部按时”的顺序，则枚举
   所有仍可保住全部截止期的安全首选（带记忆化与截止期剪枝的 DFS），
   在其中选**磁头移动最少**者（旋转延迟、读取开始时刻、rid 依次兜底）。
3. **降级择优**：已无全保方案时，枚举队列全排列，先最大化按时完成数，
   同分再按总寻道距离升序、再按 rid 编号序列字典序择优。违约请求仍会被
   服务，只是标记 `on_time=False`。

队列上限默认 8：到达时若队列已满，请求被拒绝并记入 `rejected`，产生
`accepted=False` 的 arrival 事件；服务完成腾出的空位可接纳后续到达。

## 公开 API

```python
from disksim import (
    DiskConfig,        # 磁盘几何/计时参数（不可变值对象）
    Request,           # rid, cylinder, sector, length, arrival, deadline
    ServiceRecord,     # 单请求服务时刻、寻道距离、旋转延迟、just_missed、on_time
    TimelineEvent,     # time/kind/rid/mode/detail/extra 事件
    SimulationResult,  # records, timeline, rejected, served_order
    DiskScheduler,     # 调度器，入口 run(requests) -> SimulationResult
    plan_service,      # 给定时刻/柱面/请求，计算 ServicePlan
    evaluate_order,    # 评估固定服务顺序的完成时刻与达标情况
)
```

### DiskConfig

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `cylinders` | 柱面总数（编号 0..cylinders-1） | 必填 |
| `sectors_per_track` | 每磁道扇区数 | 必填 |
| `ticks_per_sector` | 每扇区读取/经过的 tick 数 | 1 |
| `seek_ticks_per_cylinder` | 每跨越一个柱面的寻道 tick | 1 |
| `max_queue` | 待处理队列上限 | 8 |
| `head_cylinder` | 磁头初始柱面 | 0 |

属性 `rotation_period` 为整圈 tick；方法 `seek_ticks(a, b)`、
`head_sector_at(t)` 为计时辅助。

### TimelineEvent.kind

`arrival`（含 `accepted`）、`decision`（含 `mode`：`normal` / `safe` /
`best_effort`，及候选排序、安全首选集、降级评分等 `extra`）、`seek`、
`rotational_wait`、`sector_miss`、`read`、`idle`。

### SimulationResult

- `records`：按服务顺序的 `ServiceRecord`
- `timeline`：全部事件（按时间顺序）
- `rejected`：被拒绝请求的 rid
- `served_order`：服务顺序 rid 元组
- 属性 `on_time_count`、`total_seek_distance`，方法 `record_of(rid)`

参数非法（越界柱面/扇区、跨磁道尾界的读取、重复 rid、截止期早于到达等）
会抛出 `ValueError`。本实现不支持跨磁道尾界的环绕读取。

## 运行方法

```bash
# 运行全部单元测试（50 个）
python3 -m unittest discover -s tests -v

# 8 项满载端到端冒烟（含一次寻道期间刚好错过扇区的时间线）
python3 scripts/smoke_full_queue.py
```

仅使用 Python 3.11 标准库，无需安装任何依赖，无需联网。

## 验证范围

全部基于本地合成数据，测试文件与覆盖点：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_basic.py` | 扇区起始边界计算（对齐/同圈/绕圈/多 tick 扇区）、单请求完整时间线、SCAN 方向组与反向、同柱面扇区先后、跨圈等待、normal 模式标记 |
| `tests/test_deadline.py` | 普通首选违约时改选安全首选（手工推演两种顺序的完成时刻）、多安全首选取最小寻道、降级模式按时数优先、总寻道同分裁决、rid 序列字典序、截止期相等边界、违约请求仍完整服务 |
| `tests/test_timeline.py` | 寻道期间盘片转动、扇区结束边界刚好错过（含 `ticks_per_sector>1`）、部分延迟不算错过、零延迟无等待事件、多扇区读取、槽中间到达等边界、寻道耗时缩放 |
| `tests/test_queue_arrivals.py` | 8 项队列上限与第 9 项拒绝、腾位接纳、服务期间到达不可中断、未来到达不进可行性集、空闲推进、同刻到达先全部吸收再决策、参数校验 |
| `tests/test_integration.py` | 8 项满载交错到达端到端：模式切换、服务顺序、sector_miss 数值、6/8 按时与总寻道距离、容量边界接纳 |

关键手工推演场景（如截止期覆盖、刚好错过）均在测试注释中给出逐 tick
计算过程，并对 `seek_end / read_start / complete / latency / on_time`
做数值断言，而非仅断言顺序。

## 目录结构

```
disksim/
  __init__.py     # 公开 API 导出
  model.py        # 值对象、几何校验、计时辅助
  scheduler.py    # SCAN 首选、安全 DFS、降级枚举、仿真主循环
tests/            # 5 个测试模块，50 个用例
scripts/
  smoke_full_queue.py
```
