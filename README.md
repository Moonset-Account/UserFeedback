# 进程-登录会话归属工具（离线）

把进程启动记录关联到能够确认的登录会话。工具读取登录/登出 CSV、进程启动
JSONL 和按（主机， 启动批次）给出的时钟校准表，在考虑时钟偏移与误差的情况下
判断每条进程记录能否唯一归属到某个已闭合会话，并让分析人员从时间线上的每条
结果点回原始文件和行号。

仅使用 Python 3 标准库，无需安装任何依赖。

## 运行

```sh
/usr/local/bin/python3 app.py \
    --sessions sessions_a.csv [--sessions sessions_b.csv ...] \
    --processes procs_a.jsonl [--processes procs_b.jsonl ...] \
    --calibration calibration.csv \
    [--output report.json]
```

- `--sessions` / `--processes` 可重复，用于合并多份重复导出；文件读取顺序
  不影响输出。
- 报告为 JSON，默认写到标准输出，`--output` 可指定文件。
- 退出码：0 成功；2 输入/校验错误（错误信息含文件与行号）。

## 本地核验

```sh
/usr/local/bin/python3 tools/local_verify.py
```

该入口先通过 `unittest.defaultTestLoader.discover("tests")` 发现并实际运行
全部单元测试（发现 0 项、全部跳过或任何失败都以非零退出），随后用
`examples/` 中的样例数据运行 `app.py`，逐条打印每个样例断言的实际值与
期望值（case_id S1–S11）。

## 输入格式

### 登录/登出 CSV（--sessions）

表头必须包含以下列（顺序不限）：

```
source,event_id,event_type,host,boot_id,session_id,user,timestamp
```

- `event_type`：`login` 或 `logout`。
- `timestamp`：该主机本地时钟的朴素 ISO 8601（如 `2026-11-14T08:00:00`）。
- `source` + `event_id` 标识一次导出中的同一事件（用于去重）。

### 进程启动 JSONL（--processes）

每行一个 JSON 对象，必填字段：

```json
{"source": "edr", "event_id": "p-1", "host": "web1", "boot_id": "boot-100",
 "session_id": "s-1", "pid": 1001, "process": "sshd",
 "timestamp": "2026-11-14T08:30:00"}
```

空行跳过；行号按物理行计。

### 时钟校准 CSV（--calibration）

```
host,boot_id,offset_seconds,error_seconds
web1,boot-100,3600,30
```

含义：该主机该启动批次下，本地时间 + `offset_seconds` = UTC，误差
±`error_seconds` 秒。同一 (host, boot_id) 出现数值不一致的行会报错。

## 输出语义

```json
{
  "timeline": [ ... ],   // 每条去重后的进程事件一条
  "sessions": [ ... ],   // 汇总出的会话及其确定有效时段
  "conflicts": [ ... ]   // 同一事件键下内容不一致的记录
}
```

### 时间线条目（timeline）

每条包含：事件标识（source/event_id/host/boot_id/session_id/pid/process）、
`local_timestamp`、`utc_interval`（本地时间按校准换算后的可能 UTC 区间，
缺校准为 null）、`attribution`、`provenance`（全部出处 `[{file, line}]`）。

`attribution.status`：

- `confirmed`：进程整个可能 UTC 区间落在**唯一一个**已闭合会话的确定有效
  时段内；附 `user`、`session_definite_window_utc`、`session_provenance`。
- `unattributed`：进程仍保留在时间线中，`reason` 说明无法确认的原因：
  - `missing_calibration`：该 (host, boot_id) 没有校准行；
  - `no_matching_session`：没有主机、启动批次、会话 ID 都相同的会话；
  - `session_not_closed`：匹配到的会话缺 login 或 logout，未闭合；
  - `interval_outside_session`：可能时间区间未完整落入任何已闭合会话的
    确定有效时段（跨界或落在会话之外）；
  - `ambiguous_sessions`：区间同时落入多个已闭合会话。

### 匹配规则

1. 先核对 `host`、`boot_id`、`session_id` 三者都相同的会话；
2. 会话必须已闭合（同时有 login 和 logout；多条时取最早 login、最晚 logout）；
3. 会话的确定有效时段 = `[login_utc + err, logout_utc - err]`（误差向内收缩）；
4. 进程的可能区间 = `[t_utc - err, t_utc + err]`（误差向外扩张）；
5. 进程区间完整（含边界）落入唯一一个会话的确定有效时段才确认归属。

### 去重与冲突

- 事件键 = `(source, event_id)`。内容完全相同的重复导出合并为一条，
  `provenance` 保留全部文件与行号。
- 同一事件键下内容不一致的记录单列入 `conflicts`（含每个版本的内容与
  出处），且不参与会话汇总和时间线，避免使用不可信内容。

### 确定性

所有输出（时间线、会话、冲突、出处列表）都按稳定键排序，调换输入文件的
读取顺序或文件内记录顺序，导出结果逐字节一致（样例断言 S11 与单测覆盖）。

## 限制

- 时间戳为本地朴素时间，时区换算完全依赖校准表；不解析带时区的时间戳。
- 校准只按 (host, boot_id) 一个粒度，不建模漂移随时间变化。
- 冲突记录被排除而不是择一采用；需要人工核对后修正输入再重跑。
- 未知额外字段：CSV 额外列、JSONL 额外键会保留在记录内容里参与去重比较，
  但不参与匹配。
