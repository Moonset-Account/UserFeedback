# pcm_stitch — 取证拼接库

将按顺序导出的若干段单声道整数 PCM 拼接回完整录音。每次续录会重复上一段
末尾的一部分样本（重叠）；库根据每个相邻接缝允许的重叠长度范围、样本全等
约束与锚点约束，枚举全部合法拼法，并标注输出时间轴上的确认/歧义区段。

## 运行环境

- Python 3.11.6（仅标准库，无第三方依赖）

## 实际入口

- 库入口：`pcm_stitch.py` 中的 `stitch(segments, seams, anchors=())`
  - `segments`：按记录顺序排列的整数样本片段，首段绝对位置从 0 开始；
  - `seams`：每个相邻接缝允许的重叠长度闭区间，`(lo, hi)` 或 `Seam(lo, hi)`，
    数量须为 `len(segments) - 1`，上界超出片段长度时自动截断；
  - `anchors`：`Anchor(segment, offset, position)`，要求第 `segment` 段内偏移
    `offset` 的样本落在绝对位置 `position`，所有锚点必须同时成立。
- 结果：`StitchResult`
  - `status`：`"ok"` / `"no_feasible"`（无合法拼法时 `segments` 保留原始片段）；
  - `solutions`：全部合法拼法的重叠长度组合；
  - `regions`：时间轴上的 `Region` 列表，`confirmed=True` 的区段带有来源
    `(segment, offset)`，`confirmed=False` 为歧义区段；
  - `source_at(position)`：反查绝对位置的 `(片段编号, 片段内样本偏移)`，
    歧义或越界位置返回 `None`；
  - `value_at(position)`：样本值；歧义位置仅当所有覆盖它的拼法取值一致时
    返回该值，否则返回 `None`。

## 语义要点

- 两段相接处重叠的 `k` 个样本必须全部相等（`k=0` 恒合法）；
- 重叠区样本相等，来源归属较早的片段；
- 只有被**全部**可行拼法共同支持的来源映射才标为已确认，其余标为歧义
  （静音/周期片段产生多解时，尾部通常落入歧义区段）；
- 锚点在回溯搜索中逐段判定，可消除歧义，也可能使整体无可行拼法。

## 验证范围与测试入口

```bash
python -m unittest discover -s tests -t . -v
```

`tests/test_pcm_stitch.py`（unittest，17 个用例，均含真实断言）覆盖：

- **可接受结果**：唯一接缝 + 锚点成立的全确认拼接及逐样本来源反查
  （`test_unique_seam_fully_confirmed`）、三段链式接缝传播
  （`test_three_segments_chained`）；
- **应被拒绝的结果**：接缝样本不全等（`test_mismatched_seam_rejected`）、
  锚点互相矛盾（`test_conflicting_anchors_rejected`）、首段锚点冲突
  （`test_first_segment_anchor_conflict`）、锚点与全部可行接缝冲突
  （`test_anchor_must_hold_in_all_solutions`），均断言 `no_feasible` 且原段保留；
- **边界/交错场景**：周期片段多解的歧义标注（`test_periodic_segments_mark_ambiguous`）、
  锚点消歧（`test_anchor_disambiguates`）、歧义区取值一致性
  （`test_value_conflict_in_ambiguous_region`）、零重叠边界
  （`test_zero_overlap_boundary`）、重叠范围上界截断
  （`test_overlap_range_clamped_to_segment_length`）；
- **输入校验**：接缝数量/范围、锚点越界、空片段、非整数样本、单段输入。
