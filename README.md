# 风险复核名额与商户公平排班工具

每个班次只有固定数量的人工复核名额。本工具按以下规则为风险团队排班，
并保证低频商户组有机会被抽到。

## 规则

1. **保留名额**：先从当前最高风险组按入队时间（早到晚）填保留名额。
2. **商户组同班上限**：每个商户组有可配置的同班上限（`group_caps`），
   保留名额阶段的占用也计入该上限。
3. **名额合并**：保留名额未用完的部分并入普通名额。
4. **顺延优先**：普通名额优先补上一班因组上限被推迟的记录
   （`deferred_cap` 顺延队列），再按风险等级（高到低）+ 入队时间（早到晚）排。
5. **重试幂等**：事务重试（同一 `shift_id`）复用首次锁定的名单；
   已被前一班领取的交易不再进入后续批次（`claimed` 过滤）。
6. **落选去向**：因组上限落选的记录进入 `deferred_cap` 顺延队列
   （下一班优先补位）；因名额耗尽早落选的记录留在 `pending` 待复核队列。

## 运行环境

Python 3 标准库，无第三方依赖。本地入口：

```bash
/usr/local/bin/python3 app.py run --shift samples/shift_s1.json --state /tmp/state.json
/usr/local/bin/python3 app.py run --shift samples/shift_s2.json --state /tmp/state.json
/usr/local/bin/python3 app.py sample --workdir /tmp/sched_demo   # 两班连续样例
/usr/local/bin/python3 tools/local_verify.py                     # 核验入口
```

## 输入格式（班次 JSON）

```json
{
  "shift_id": "S1",
  "reserved_slots": 2,
  "general_slots": 1,
  "group_caps": {"bulkA": 2},
  "records": [
    {"txn_id": "T1", "risk_level": 3, "merchant_group": "bulkA",
     "enqueued_at": "2026-11-14T08:01:00"}
  ]
}
```

- `risk_level`：整数，越大风险越高。
- `enqueued_at`：ISO 时间字符串，按字典序比较。
- `group_caps` 未列出的商户组不设上限。
- 上一班落选留在 `pending` 队列的记录，由上游在下一班输入中继续提供；
  已被领取的记录即使再次出现在输入中也会被过滤（`excluded_claimed`）。

## 输出语义

`run` 输出 JSON：

- `selected[]`：入选记录，`reason` 为
  `reserved_high_risk`（保留名额）/ `carryover_deferred`（顺延补位）/
  `general_risk_time`（普通名额按风险+入队时间）。
- `deferred_cap[]`：因组上限落选的记录，`queue=deferred_cap`，
  自动成为下一班的顺延队列。
- `pending[]`：因名额耗尽落选的记录，`queue=pending`。
- `excluded_claimed[]` / `excluded_duplicate[]`：被过滤的 txn_id。
- `slots`：名额使用汇总，`reserved_unused_merged` 为并入普通名额的保留名额数。
- `reused_lock`：本次是否为重试复用首次锁定名单。

状态文件（`--state`）保存 `claimed`（已领取）、`carryover`（顺延队列）、
`locks`（各班次首次锁定名单）。

## 两班连续样例（低频组何时被补进）

`samples/shift_s1.json`：保留 2 + 普通 1，`bulkA` 上限 2。
最高风险的 bulkA 有 T1/T2/T3，保留名额给 T1/T2，T3 触发组上限进入
`deferred_cap`；普通名额给 bulkB 的 T4；低频组 `rare` 的 T5 名额耗尽，
留在 `pending`。

`samples/shift_s2.json`：保留 1 + 普通 2。输入中再次出现的 T1 已被 S1
领取，被过滤；保留名额给新到的最高风险 T7；顺延的 T3 优先补位
（`carryover_deferred`）；**低频组 `rare` 的 T5 在普通名额阶段按
风险+入队时间被补进**（`general_risk_time`）。

## 需求到测试的映射

| 需求点 | 测试 / 样例断言 |
| --- | --- |
| 保留名额按最高风险组+入队时间 | `test_reserved_fills_highest_risk_by_enqueue_time` |
| 组上限跨保留/普通阶段合并计数 | `test_cap_counts_across_reserved_and_general` |
| 未用完保留名额并入普通名额 | `test_unused_reserved_merges_into_general` |
| 普通名额按风险+入队时间排序 | `test_general_orders_by_risk_then_enqueue_time` |
| 组上限落选进入顺延队列 | `test_group_cap_defers_excess_to_carryover_queue` |
| 顺延记录下一班优先补位 | `test_carryover_has_priority_over_new_arrivals` |
| 顺延记录仍超限则继续顺延 | `test_carryover_still_capped_stays_deferred` |
| 名额耗尽落选留在 pending | `test_pending_when_slots_exhausted` |
| 已领取交易不再进入后续批次 | `test_claimed_txns_excluded_from_later_batches`、`S2.excluded_claimed` |
| 事务重试复用首次锁定名单 | `test_retry_reuses_first_locked_list`、`S2.retry.*` |
| 两班连续运行低频组被补进 | `test_low_freq_group_filled_in_second_shift`、`S2.T5.*` |

## 限制

- 状态为本地 JSON 文件，无并发写入保护；同一状态文件请勿并行执行。
- `enqueued_at` 按字符串字典序排序，请使用统一长度的 ISO 格式。
- 外部系统（交易队列、通知等）均以本地 JSON 输入/状态文件模拟。
