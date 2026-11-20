# histogram_delta —— 采集端累计直方图转换模块

## 1. 模块职责

采集端按进程周期性上报以下数据：

- 进程标识：`process_id` 与进程启动标识 `process_start`；
- 采样时间戳 `timestamp`；
- **由小到大**的桶上界 `upper_bounds`，末桶上界为正无穷 `math.inf`；
- 每个上界对应的**累计**请求数 `cumulative_counts`。

下游需要相邻两次采样之间，各个**不重叠桶**内的**新增请求数**。本模块负责把
“累计计数”转换成“区间增量”，并在桶界变化、进程重启、坏样本、重传等场景下
执行固定的结算规则。

纯 Python 标准库实现，无第三方依赖，无任何 I/O 或网络访问。

## 2. 实现的规则（对应题面）

1. **只在前后共有的上界上结算。** 桶界变化时，取两次采样上界集合的交集，
   并按新样本中的升序排列。旧桶里的请求**不会被猜测或拆分**到新桶；
   非共有的上界不产生任何输出区间。
2. **换进程的首样只建基线。** 首次见到某 `process_id`，或同一 `process_id`
   的 `process_start` 发生变化（视为进程重启）时，首样返回
   `BaselineEstablished`，不产出增量。
3. **负增量拒绝并保留上次有效基线。** 若某区间换算出的新增请求数为负，
   抛出 `NegativeIncrementError`，本次样本被拒绝，内部基线保持为上一份有效
   样本；**下一份好样本仍跨过坏样本**，针对保留下来的旧基线结算。
4. **重传区分。**
   - 相同时间戳且桶界、累计数完全相同 → 返回 `Retransmission`（忽略）；
   - 相同时间戳但内容不同 → 抛出 `ConflictingRetransmissionError`，
     让调用方显式看到冲突，基线不变。
5. **时间推进约束。** 同一进程的样本时间戳必须不早于基线时间戳；更早的样本
   抛出 `OutOfOrderTimestampError`（基线保留）。同时间戳走第 4 条重传逻辑。

## 3. 数据模型与区间语义

- 桶用半开区间 `(lower_bound, upper_bound]` 表示；第一个结算区间的下界为
  `0.0`，后续区间的下界是前一个共有上界。
- 对共有上界 `b`，累计增量 `Δ(b) = 新样本累计数(b) − 基线累计数(b)`；
  区间增量 `= Δ(当前上界) − Δ(上一个共有上界)`（首个区间的前值为 0）。
- 只要任一区间增量为负即拒绝整份样本（该检查同时覆盖了“共有上界上累计
  计数倒退”的情形）。
- 末桶上界恒为 `math.inf`，因此即使其它桶界全部变化，至少能在正无穷上界
  结算出总新增请求数。
- 单份样本内部也要求累计数非负且非递减，否则构造时即报
  `InvalidSampleError`（属于结构性坏数据，调用方必须修正后重发）。

## 4. 使用示例

```python
import math
from histogram_delta import (
    HistogramDeltaConverter,
    ProcessSample,
    NegativeIncrementError,
)

INF = math.inf
converter = HistogramDeltaConverter()

# 首样：只建基线，无增量
converter.submit(ProcessSample.build(
    "proc-1", "start-a", timestamp=1,
    upper_bounds=(10, 20, INF), cumulative_counts=(3, 7, 10),
))

# 正常结算：Δ = (2, 3, 4)，区间增量 = [2, 1, 1]
settled = converter.submit(ProcessSample.build(
    "proc-1", "start-a", timestamp=2,
    upper_bounds=(10, 20, INF), cumulative_counts=(5, 10, 14),
))
for bucket in settled.increments:
    print(bucket.lower_bound, bucket.upper_bound, bucket.new_requests)
# 0.0 10 2
# 10  20 1
# 20  inf 1
print(settled.total_new_requests)  # 4

# 桶界变化：旧上界 10 消失、新上界 15 出现，只在共有上界 20、inf 上结算
# 对照 t=2 基线 counts=(5,10,14)：20 处 12-10=2，inf 处 18-14=4
changed = converter.submit(ProcessSample.build(
    "proc-1", "start-a", timestamp=3,
    upper_bounds=(15, 20, INF), cumulative_counts=(8, 12, 18),
))
# 输出区间只有 (0,20] 与 (20,inf]；(0,15]、(15,20] 不做任何猜测拆分
print(changed.boundaries_changed)        # True
print(changed.common_upper_bounds)       # (20, inf)
print([b.new_requests for b in changed.increments])  # [2, 2]

# 坏样本：样本自身非递减（5<=7<=19），但在共有上界 20 处累计数
# 7 小于基线的 12，区间 (0,20] 增量为 -5 -> 拒绝，保留 t=3 基线
try:
    converter.submit(ProcessSample.build(
        "proc-1", "start-a", timestamp=4,
        upper_bounds=(15, 20, INF), cumulative_counts=(5, 7, 19),
    ))
except NegativeIncrementError as exc:
    print(exc)  # 调用方可记录告警；converter 内部基线未被污染

# 下一份好样本跨过坏样本，仍对照 timestamp=3 的基线 (8,12,18) 结算：
# (0,20]: 15-12=3；(20,inf]: (22-18)-(15-12)=4-3=1
good = converter.submit(ProcessSample.build(
    "proc-1", "start-a", timestamp=5,
    upper_bounds=(15, 20, INF), cumulative_counts=(9, 15, 22),
))
print([b.new_requests for b in good.increments])  # [3, 1]
```

无状态的单次结算也可以直接调用纯函数
`compute_increments(baseline_sample, new_sample) -> Settlement`。

## 5. API 一览

| 名称 | 说明 |
| --- | --- |
| `ProcessSample.build(...)` | 校验并构造一份不可变样本 |
| `HistogramDeltaConverter()` | 按进程维护基线的有状态转换器 |
| `converter.submit(sample)` | 提交样本，返回 `BaselineEstablished` / `Retransmission` / `Settlement` |
| `converter.baseline_for(pid)` | 查看某进程当前保留的有效基线 |
| `converter.tracked_processes()` | 当前已建基线的进程 ID |
| `compute_increments(baseline, sample)` | 不触碰状态的纯结算函数 |

异常体系（均继承 `ConversionError`）：

- `InvalidSampleError`：样本结构非法；
- `OutOfOrderTimestampError`：时间戳早于基线；
- `ConflictingRetransmissionError`：同时间戳冲突重传；
- `NegativeIncrementError`：区间增量为负，样本被拒绝、基线保留。

## 6. 目录结构

```
histogram_delta/
    __init__.py        # 对外导出
    converter.py       # 校验、纯结算逻辑、有状态转换器
tests/
    test_converter.py  # 本地合成数据 unittest 用例
README.md
```

## 7. 运行测试的方式（本环境未执行）

```
python -m unittest discover -s tests -v
```

> **状态声明：按照本次任务约束，上述测试与任何代码均未在本环境中实际运行、
> 构建或验证。** `tests/test_converter.py` 仅使用普通本地合成数据
> （手工构造的样本元组），无外部请求、无文件/网络 I/O；用例的预期值是
> 依据实现规则静态推演得出，不代表“测试已通过”。

测试覆盖的场景包括：首样建基线、稳定桶界结算、零增量、三样本链式结算、
桶界变化只在共有上界结算、仅正无穷共有的退化情形、负增量拒绝且基线保留、
坏样本后好样本跨样本结算、同值重传忽略、冲突重传报错、重启进程重新建基线、
乱序时间戳、多进程互不干扰，以及样本结构校验（含末桶必须为无穷、桶界严格
升序、计数非递减、拒绝布尔值伪装成数字等）。

## 8. 关键设计取舍

- **不猜分旧桶**：桶界变化时只输出共有上界切出的区间，宁可少报也不制造
  无法证明的分桶数据；`Settlement.common_upper_bounds` 与
  `boundaries_changed` 让下游能识别结算口径。
- **先算完再更新状态**：负增量在纯函数内抛出，转换器仅在结算成功后替换
  基线，保证坏样本绝不污染状态。
- **重传判定以保留基线为准**：即使中间出现过被拒绝的坏样本，重传基线
  样本仍被识别为同值重传。
- **进程身份二元组**：`process_id` 相同但 `process_start` 不同即视为新
  进程，首样只建基线，避免把重启前后的累计计数直接相减。
