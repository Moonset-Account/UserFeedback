# 法证样品接力 · 证据核算模块

在样品送检前，依据容器换装记录、温度传感器覆盖与校准区间、封条核对记录，以及
运输双方上报的交接时间窗，按样品核算哪些运输片段具备**合格的温控证据与封存证据**，
并给出最早的未覆盖位置和能够解释该缺口的原始单据。

全部逻辑仅使用 Python 3.11 标准库，无第三方依赖，所有验证数据均为本地合成数据，
不联网、不读取真实凭据。

## 判定规则

时间统一用同一时钟上的可比较数值（如 epoch 秒，允许小数）。所有区间均为
**半开区间 `[start, end)`**。

1. **传感器按容器隔离**：换装后留在旧容器上的传感器，只能为仍在旧容器中的样品
   作证。传感器只有在“实际覆盖区间”与“有效校准区间”相交、且其附着容器正是该
   运输腿所用容器时，才能提供温控证据。
2. **交接窗覆盖不全时保留歧义**：交接时刻必须由运输**双方**的时间窗共同覆盖
   （单方上报、双方窗口无共同覆盖、或时刻只落在半开边界上，均视为未确认）。
   未确认时：
   - 交接时刻本身记为**阻断点（blocked point）**，即使两侧都有证据也不计入合格段，
     且不允许跨该点拼接；
   - 双方上报窗口的整体跨度（最早开始到最晚结束，含窗口之间无人记录的空隙）记为
     **歧义带（ambiguous band）**，整体不计入合格段，但会在结果中保留并随附单据。
3. **相邻合格区间可拼接**：同一腿内首尾相接（半开相切）的合格区间合并；已确认的
   交接允许跨容器拼接；阻断点和歧义带不参与拼接。

某一时刻同时满足“有合格温控证据”和“有封条核对记录”才是可接收的运输片段。

## 目录结构

```
evidence_accounting/
  __init__.py        公开 API 导出
  intervals.py       半开区间原语：交、差、并、带阻断点合并、最早缺口
  accounting.py      证据核算主逻辑与输入/输出数据模型
tests/
  test_intervals.py  区间原语单测
  test_accounting.py 端到端核算单测（本地合成数据）
scripts/
  demo.py            可运行的综合场景演示
```

## 公开 API

### 输入记录（`evidence_accounting.accounting`，均为不可变 dataclass）

| 类型 | 关键字段 | 含义 |
| --- | --- | --- |
| `SampleJourney(sample_id, container_id, start, end, transfers=())` | 起始容器、旅程区间、换装序列 | 一个样品的完整运输旅程 |
| `Transfer(time, from_container, to_container, transfer_doc_id)` | 换装时刻与单据号 | 一次接力换装 |
| `Sensor(sensor_id, container_id, coverage, calibrations)` | 附着容器、测量覆盖区间、若干有效校准区间 | 温度传感器 |
| `SealCheck(seal_doc_id, container_id, verified_window)` | 容器与封条核对合格区间 | 封条记录 |
| `HandoverWindow(transfer_doc_id, party, window)` | 单据号、上报方、时间窗 | 一方上报的交接窗 |

### 输出记录

- `SampleAssessment`
  - `sample_id`
  - `acceptable_segments: tuple[(start, end), ...]` — 可接收的运输片段
  - `earliest_uncovered: float | None` — 最早未覆盖位置（全程覆盖时为 `None`）
  - `gap_documents: tuple[GapDocument, ...]` — 解释最早缺口的原始单据
  - `ambiguous_intervals` — 保留下来的歧义带
  - `blocked_points` — 未确认交接的阻断时刻
- `GapDocument(doc_id, doc_type, reason, detail)`
  - `doc_type ∈ {"sensor","seal","handover","transfer"}`
  - `reason` 取值：`sensor_coverage_missing`、`sensor_uncalibrated_at_time`、
    `seal_record_missing`、`handover_not_confirmed`、`handover_window_ambiguous`

### 入口函数

```python
from evidence_accounting import assess_all, assess_sample

assess_sample(journey, sensors, seal_checks, handover_windows) -> SampleAssessment
assess_all(journeys, sensors, seal_checks, handover_windows)
    -> dict[sample_id, SampleAssessment]
```

输入非法（区间非正、换装时刻不在旅程内、换装链与容器不一致、重复样品、
交接窗引用未知单据、同一单据同一方重复上报、同一单据超过两方上报等）会抛出
`ValueError`。

## 运行方法

```bash
# 运行全部单元测试
python3 -m unittest discover -s tests -v

# 运行综合场景演示
python3 scripts/demo.py
```

## 验证范围

测试全部基于本地合成数据，共 51 个用例（区间原语 24 个、核算 27 个），覆盖：

- 区间交/差/并、半开相切、带阻断点合并与拆分、最早缺口计算；
- 单腿全程合格、传感器覆盖缺口、校准缺口、封条缺口、无传感器/无封条；
- 同一腿内相邻合格区间拼接、首尾相切拼接、尾部缺口；
- 双方共同覆盖确认后跨容器拼接；
- 旧容器传感器换装后不得为新容器样品作证；
- 无交接窗、单方窗口、双方窗口相切于交接时刻、窗口不相交、窗口部分覆盖等
  歧义/阻断场景，以及歧义带对相邻腿的全局裁剪和超界裁剪；
- 浮点时间、校准恰在交接半开边界到期；
- 多样品在不同容器上时间线交错、三接力链中“确认 + 未确认”混合；
- 各类非法输入的 `ValueError` 校验与空输入。

所有结论均以实际运行的断言为准；演示脚本 `scripts/demo.py` 可独立复跑，
直观展示三条规则在同一旅程中的联动结果。
