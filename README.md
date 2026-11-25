# 电话坐席高风险操作 · 确认服务（confirm）

当坐席代替用户发起高风险操作（如转账）时，确认服务必须判断**用户听到并复诵的
内容能否唯一指向待授权的那一笔**。本模块只做防御性判断：选择播报、消费播放与
复诵事件、在提交时重新校验区分性，绝不生成或执行任何利用、入侵或规避载荷。

纯标准库，Python 3.11.6，无第三方依赖，不联网。

## 公开 API

从 `confirm` 包导入：

### 数据模型

| 名称 | 说明 |
| --- | --- |
| `Field(field_id, name, aliases)` | 一个字段。`field_id` 为整数编号（用于确定性并列），`aliases` 为该字段值所有可朗读别名。 |
| `Candidate(candidate_id, fields, label="")` | 一笔候选待授权操作。 |
| `CandidateBook(candidates)` | 一次已认证通话中的全部候选快照。 |
| `Segment(segment_index, field_ids, duration_ms)` | 一段播报：朗读哪些字段、时长多少。 |
| `PlaybackEvent(segment_index, target_id, completed=True)` | 逐段播放完成事件。 |
| `RepetitionEvent(field_id, alias, matched=True)` | 用户对某字段的复诵事件。 |
| `HomophoneTable(groups=())` | 显式同音别名等价类；`link(*spellings)` 可增量登记。 |

### 播报选择

```python
plan_confirmation(book, target_id, required_fields, segments, budget_ms,
                  *, homophones=None) -> ConfirmationPlan | None
```

在时长预算 `budget_ms` 内，选能区分目标与**所有**其他候选的最短播报：

* 同音别名视为相同（同音等价类内任一写法都算“听起来一样”，不能用来区分）；
* 总时长更短者优先；
* **等长**时取朗读字段编号序列的字典序较小者；
* 预算内不存在可行播报时返回 `None`；
* 结构性问题（未知目标、必读字段不在目标中、段含目标不存在的字段、段编号重复）
  抛 `ValueError` / `KeyError`。

返回的 `ConfirmationPlan` 含 `segments`、`field_ids`（按段升序、段内顺序拼接的
编号元组）、`total_duration_ms`、`segment_indices`。

### 确认会话

```python
ConfirmationSession(plan, book, homophones=None)
session.on_playback(PlaybackEvent)      # 记录逐段播放完成
session.on_repetition(RepetitionEvent)  # 记录复诵
session.update_candidates(book)         # 通话中候选集变化
session.submit(required_fields, *, book=None) -> SubmitResult
session.completed_segment_indices()
session.announced_fields()
```

`SubmitResult.status` 为 `SubmitStatus` 之一：`CONFIRMED` / `SUPPLEMENTAL_REQUIRED` /
`REJECTED`，并带 `reason`、`detail`、`confirmed_fields`、`ambiguous_candidate_ids`。

提交时的判定顺序：

1. **未播完的段不计入确认**：只有 `completed=True` 的段，其字段才进入“已听到”
   集合；必读字段未由已播完段覆盖 → `SUPPLEMENTAL_REQUIRED`（`unfinished_segments`）。
2. **提交时重算区分性**：用提交时刻的候选快照重新判断已播字段能否区分目标与
   所有其他候选。**若播报期间新增了另一笔听起来完全相同的候选，退回补充确认**
   （`new_indistinguishable_candidate`），绝不沿用计划期的区分结果。
3. 复诵须覆盖全部已播字段且全部匹配；复诵了未播字段或漏复诵 → `SUPPLEMENTAL_REQUIRED`；
   复诵与播报不一致 → `REJECTED`。
4. 复诵内容必须唯一指向目标（对当前全部候选逐字段对音），否则不确认。

确认成功后会话锁定，不再接受播放/复诵事件，也不能重复提交。补充确认后可以补充
播放/复诵并重新提交。

### 工具函数

* `sound_key(text, table=None)` / `base_sound_key(text)` —— 听感归一（NFKC、忽略
  大小写/空白/标点/拼音声调；`table` 提供时并入同音等价类）。
* `sound_signature(candidate, field_id, table=None)` —— 候选某字段的听感签名集合，
  缺该字段返回 `None`。
* `candidates_consistent_with_repetitions(book, repetitions, table=None)` —— 能解释
  全部复诵的候选编号。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围

`tests/` 覆盖（共 45 个用例）：

* **听感归一**（`test_sound.py`）：大小写/空白/标点忽略、声调字母与声调数字忽略、
  纯数字尾号不被误改、非字符串拒绝、同音表正向/反向/传递合并/规范键确定性。
* **播报选择**（`test_planner.py`）：分段合计短于单段全量、预算不足返回 `None`、
  必读字段强制、等长取编号序列字典序较小者（含 `(1,10) < (2,)` 的序列比较）、
  共享字段不足以区分须带区分字段、同音后无解、部分别名重叠导致不可区分、
  缺字段视为可区分、多候选必须区分全部、非法输入报错。
* **会话与交错**（`test_session.py`、`test_integration.py`）：未播完/迟到未完成事件
  不计入、完成状态不可回退、事件乱序（先复诵后播完）仍正确、补充确认后可重试、
  播报中新增同音候选 → 补充确认（通过 `update_candidates` 与 `submit(book=...)`
  两条路径）、新增可区分候选不阻断、确认后锁定、播放事件目标/段编号校验。
* **同音提交**（`test_homophone_submit.py`）：同音表使区分字段失效时计划无解；
  复诵经同音表归一回配成功。

另以脚本对 `plan_confirmation` 做了 3000 组随机候选/字段/段/预算的暴力枚举交叉
校验（临时脚本，未纳入 `tests/`），最短播报结果与暴力最优完全一致，无偏差。

### 设计取舍

* “能否区分”采用保守语义：目标与另一候选在某字段上的听感签名集合只要**相交**
  就视为可能混淆、不能区分；完全不相交才可区分；另一候选缺该字段则天然可区分。
* 提交路径始终以**提交时刻**的候选快照重算，避免“播报中途新增同音候选却沿用旧
  区分结论”的安全缺口。
