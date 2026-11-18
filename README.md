# 多设备取证时间线组件

把摄像设备、门禁控制器、取证工作站等来源的记录放到同一条真实时间轴上：
以**共同观测事件**、**绝对时间锚点**和各设备的**重启区间**估计每台设备每个
时钟段的偏移与误差，输出每条记录的可能时间范围，并对来源明确给出的先后关系
做因果保留与顺序判定。

## 建模与算法

- 每次重启把一台设备的时钟切成互不相关的**段（segment）**，段间偏移独立估计；
  恰在重启时刻的记录归入重启后的新段。
- 变量：`REF=0`（时间轴原点）、每段一个偏移变量 `off`、每条记录一个真实时间 `t`，
  满足 `t = 本地读数 + off + 观测误差`。
- 全部约束编译为**差分约束系统**（`x_v - x_u <= w`），用 Bellman-Ford 求解：
  - 记录观测误差：`|t - (off + local)| <= obs_error`
  - 同段本地时钟单调：同段内按本地读数 `t_prev <= t_next`（跨重启不继承）
  - 共同观测锚点：`|t_a - t_b| <= tolerance`
  - 绝对时间锚点：`|t - abs_ts| <= tolerance`
  - 明确先后：`t_after - t_before >= min_gap`
- 可行区间：原图最短路给上界、反向图最短路给下界；与 REF 不连通的变量保持
  `±inf`（无锚点即无界，而不是被错误地压到 0）。
- 矛盾即负环：隔离每个涉事锚点单独求其隐含的偏移可行区间，区间交集为空时
  在 `ConflictReport.interval_clashes` 中注明冲突段、锚点 ID、各自区间与源记录。
- 顺序判定（`compare` / `undetermined_pairs`）：
  - 有因果依据（明确先后或同段单调链可达）→ `before`/`after`，并返回依据来源；
  - 无因果依据但区间严格分离 → 按区间证据定序；
  - **区间相交（含端点相触）且缺少因果依据 → `undetermined`（顺序未定）**。
- 增量影响面：锚点/先后关系在段连接图上连边，新增锚点后只重算其连通分量内
  的设备段，见 `last_affected_segments()` / `last_recomputed_records()`；
  被拒锚点会回滚，引擎保持上一可行状态。

## 目录与入口

```
timeline/
  model.py    数据模型：Device/Record/SharedEventAnchor/AbsoluteAnchor/
              Ordering/TimeRange/SegmentKey/ConflictReport/ConflictError
  solver.py   差分约束图：约束添加、负环检测（带溯源标签）、可行域求解
  engine.py   TimelineEngine：录入、求解、增量重算、因果顺序判定、冲突报告
demo.py       三设备端到端演示（含矛盾锚点拒绝）
tests/
  test_timeline.py  unittest 验收用例（16 个）
```

公共入口为 `timeline.TimelineEngine`，典型用法：

```python
from timeline import (TimelineEngine, Device, Record,
                      SharedEventAnchor, AbsoluteAnchor, Ordering, ConflictError)

eng = TimelineEngine()
eng.add_device(Device("cam", kind="camera", reboots=[200.0]))
eng.add_record(Record("cam1", "cam", 100.0, obs_error=1.0, source="cam.log:L1"))
eng.add_shared_anchor(SharedEventAnchor("A1", "cam1", "ws1", tolerance=1.0))
eng.add_absolute_anchor(AbsoluteAnchor("ABS1", "ws1", abs_ts=120.0))
eng.add_ordering(Ordering("cam1", "ws2", min_gap=2.0, source="custody:1"))

for rid, rng in eng.time_ranges().items(): ...   # 每条记录的可能时间范围
eng.offset_bounds()                               # 每个重启段的偏移区间
eng.compare("cam1", "ws2")                        # before/after/undetermined + 依据
eng.undetermined_pairs()                          # 相交且无因果依据的记录对
```

## 运行与验证

仅使用本地合成夹具，无网络、无外部数据、无真实目标探测；Python 3.11.6，
标准库实现，无第三方依赖。建议以隔离模式运行，避免 cwd 中被植入的模块：

```bash
python3 -I -m unittest discover -s tests -v   # 验收测试（真实断言）
python3 -I demo.py                            # 端到端演示
```

### 验证范围（tests/test_timeline.py，16 个用例）

| 类别 | 用例 | 覆盖的交叉约束 |
| --- | --- | --- |
| 可接受结果 | `test_ranges_and_offsets` | 共同观测+绝对锚点+观测误差下的记录区间与各段偏移 |
| 可接受结果 | `test_explicit_ordering_is_preserved_with_reason` | 来源明确的先后关系保留且可溯源 |
| 可接受结果 | `test_same_segment_monotone_is_causal` | 同段本地时钟单调构成因果链 |
| 可接受结果 | `test_undetermined_pairs_only_overlapping_non_causal` | 相交且无因果依据并列为顺序未定，含端点相触 |
| 边界/交错 | `test_touching_ranges_undetermined_without_cause` | 区间端点相触=未定；补明确先后（gap=0）后转为 before |
| 边界/交错 | `test_strictly_disjoint_ranges_are_ordered_by_range_evidence` | 无因果但严格分离时按区间证据定序 |
| 边界/交错 | `test_interleaving_ranges_undetermined` | 区间交错且无因果依据 → 未定 |
| 边界/交错 | `test_no_anchor_means_unbounded` | 无锚点设备的记录与偏移保持 ±inf 无界 |
| 重启分段 | `test_segment_offsets_are_independent` | 重启后新段偏移独立，旧段无锚点则无界 |
| 重启分段 | `test_records_across_reboot_have_no_monotone_causality` | 跨重启无单调因果；显式先后可跨段建立 |
| 重启分段 | `test_record_exactly_at_reboot_joins_new_segment` | 重启时刻边界归属 |
| 应拒绝 | `test_contradictory_absolute_anchors_raise_with_report` | 矛盾绝对锚点被拒，报告冲突段/区间/源记录并回滚 |
| 应拒绝 | `test_contradictory_shared_anchor_raise_and_rollback` | 共同观测与绝对锚点矛盾被拒并回滚 |
| 应拒绝 | `test_contradictory_ordering_cycle_is_rejected` | 先后关系负环被拒，旧结论保持 |
| 增量重算 | `test_new_anchor_only_recomputes_connected_component` | 新锚点只重算相关连通分量，无关记录区间不变 |
| 增量重算 | `test_full_solve_marks_every_segment_affected` | 全量求解标记全部段 |

## 语义说明

- 观测误差 `obs_error` 收紧的是**偏移可行区间**；当记录被容差为 0 的共同观测
  或绝对锚点钉住时，其点值由锚点决定，误差体现在偏移上而非记录区间上。
- 所有数值为同一单位的浮点秒（或任意一致的单调刻度）；`inf` 表示无界。
