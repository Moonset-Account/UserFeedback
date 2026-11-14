# SEC-20261114-021 告警事件生命周期管理

办公终端的拦截上报会在短时间内刷出一串相同告警。本模块按
**(租户 tenant, 终端 endpoint, 检测项 detection)** 三元组管理告警事件，
安全台每个事件只收到一次通知，处置队列保留完整计数。

仅使用 Python 3 标准库，无第三方依赖。

## 运行入口

```bash
# 单元测试 + 样例回归 + 边界断言(一键核验)
/usr/local/bin/python3 tools/local_verify.py

# 仅跑单元测试
/usr/local/bin/python3 -m unittest discover -s tests -v

# 运行演示样例(延迟到达 / 重新开启 / 事件归属)
/usr/local/bin/python3 app.py samples/demo.jsonl

# 从 stdin 读指令
echo '{"op":"hit","report_id":"r1","tenant":"acme","endpoint":"ws-001","detection":"trojan.x","occurred_at":"2026-11-14T09:00:00Z","arrived_at":"2026-11-14T09:00:01Z"}' | /usr/local/bin/python3 app.py
```

## 输入格式(JSON Lines，每行一条指令，按序处理)

| op | 字段 | 说明 |
|---|---|---|
| `hit` | `report_id, tenant, endpoint, detection, occurred_at, arrived_at` | 一条终端拦截上报，同时携带**发生时间**与**到达时间** |
| `receipt` | `receipt_id, event_id, disposition, received_at` | 处置回执；`disposition` 为 `"confirmed"` 才关闭事件 |
| `advance` | `now` | **显式推进时间**的入口；窗口已结束的打开事件随之到期 |
| `dump` | — | 输出全部事件与通知的快照 |

时间字段接受 ISO-8601 字符串(可带 `Z`)或 epoch 秒数。`#` 开头与空行跳过。
聚合窗口默认 600 秒(十分钟)，可用 `--window-seconds` 调整。

## 输出语义

每条指令输出一行 JSON：

- `new_event`：开启新事件并通知(`notified=true`)；窗口外命中触发时附 `superseded_event_id`。
- `aggregated`：窗口内重复命中，汇总到同一事件，`count` 增加，不通知。
- `duplicate_ignored`：重复 `report_id`，**不增加任何计数**。
- `late_attributed`：迟到上报归回旧事件，`late_count` 增加，不重开、不通知。
- `closed`：已确认回执关闭事件，返回最终 `count / late_count / total_count`。
- `receipt_not_confirmed` / `receipt_unknown_event` / `receipt_already_closed` / `duplicate_receipt_ignored`：回执未生效的各类原因。
- `advanced`：时间推进结果，`expired` 列出本次到期的事件。
- `dump`：完整快照；`error`：单行输入错误(不中断整批)。

## 生命周期规则

1. 某三元组首次命中 → 开启事件(OPEN)，立即通知一次；聚合窗口为
   `[首次发生时间, +600s)`，左闭右开。
2. 窗口内的重复命中(按**发生时间**判定)汇总到同一事件，计数保留，不重复通知。
3. 收到 `disposition=confirmed` 的回执 → 事件关闭(CLOSED)。
4. **关闭前发生**的迟到上报(发生时间 ≤ 关闭时间，到达可晚于关闭)归回旧事件；
   **关闭后发生**的新上报开启新事件并重新通知。
5. `advance` 推进时间越过窗口终点，或新命中的发生时间越过窗口终点 →
   打开事件到期(EXPIRED)；到期事件窗口内发生的迟到上报仍归回该事件，
   到期事件仍可被已确认回执关闭。
6. `report_id` 全局去重：重复 ID 不增加计数；`receipt_id` 同样去重。

## 需求 → 测试/样例映射

| 需求 | 验证点(case_id) |
|---|---|
| 首次命中立即通知 | CASE-001, CASE-013 |
| 十分钟内重复命中汇总到同一事件、计数保留 | CASE-002, CASE-016(窗口终点边界), 边界断言 B1/B3 |
| 已确认处置回执关闭事件 | CASE-004；未确认不关闭 CASE-005 |
| 关闭前的迟到上报归回旧事件 | CASE-006, CASE-017(关闭时刻边界), 边界断言 B2, 样例行 8 |
| 关闭后的新上报开启新事件 | CASE-007, 样例行 9 |
| 输入带发生/到达时间，归属按发生时间 | CASE-006, CASE-009, 样例行 8/11/17 |
| 重复事件 ID 不增加计数 | CASE-003, 边界断言 B4, 样例行 4 |
| 显式推进时间的入口 | CASE-008, CASE-018(禁止回退), 样例行 1/10 |
| 样例演示延迟到达、重新开启、事件归属 | CASE-015(端到端比对 `samples/demo_expected.jsonl` 18 行) |
| 按租户/终端/检测项隔离管理 | CASE-010, 样例行 6 |
| 回执健壮性(未知/重复/已关闭) | CASE-011, CASE-012, 样例行 14/15/16 |
| 窗口外命中滚动新事件 | CASE-014, CASE-016 |

## 限制

- 单进程内存实现，无持久化；进程退出后状态丢失。
- 事件归属依赖上报自带的 `occurred_at`，不校验其与 `arrived_at` 的先后关系。
- 时钟只能前进：`advance` 不接受早于当前时间的值(抛 `ValueError`，以 `error` 行输出)。
- 时间精度为秒级 ISO-8601 或 epoch 秒；聚合窗口固定左闭右开。
