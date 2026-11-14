# 跨时区工作区锁定排程器（本地可试算）

驻场账号下班后仍开着应用时，终端管理需要在锁定时间到达时处理仍在用的工作区。
本工具在本地根据员工时区、每周工作时段、临时延期与当前时间，试算每个应用工作区
当前是否需要锁定、决定依据，以及下一次状态切换时间。纯 Python 3 标准库，无外部依赖。

## 运行

```bash
# 试算任意输入
/usr/local/bin/python3 app.py samples/case_dst_overlap.json

# 本地核验入口（单元测试 + 已交付样例逐条断言，全部通过才退出 0）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式（JSON 文件）

```json
{
  "case_id": "可选标签",
  "timezone": "America/New_York",            // IANA 时区名
  "now_utc": "2026-11-01T05:45:00Z",         // 当前时间，必须带显式偏移
  "weekly_shifts": [                          // 每周工作时段，本地墙钟时间
    {"weekday": "sun", "start": "01:30", "end": "05:00"}
  ],
  "extensions": [                             // 临时延期，UTC 绝对时间
    {"start_utc": "2026-03-08T06:00:00Z",
     "end_utc":   "2026-03-08T06:15:00Z",
     "label": "hotfix-window"}
  ],
  "workspaces": [{"id": "ws-nyc-01"}]
}
```

- `weekday` 接受 `mon`..`sun`（大小写不限）或整数 0-6（周一=0）。
- 时段均为**左闭右开** `[start, end)`：起点时刻属于时段，终点时刻不属于。
- **跨午夜班次归到发起日期**：`end <= start` 时，班次归属开始当天，结束于次日
  本地时间（`end == start` 视为 24 小时班次）。
- **夏令时跳变**（基于 `zoneinfo`）：
  - 重叠（秋季拨回）：本地时刻出现两次时，取**较早**的 UTC 瞬间；
  - 缺失（春季拨快）：本地时刻不存在时，**顺延到首个有效**墙钟时刻。
- 临时延期使用 **UTC 绝对时间**，同样左闭右开；与每周时段同时覆盖当前时间时，
  决定依据优先报告临时延期。

## 输出语义

对每个工作区输出一条记录：

```json
{
  "workspace_id": "ws-nyc-01",
  "locked": false,                      // 当前是否需锁定
  "basis": {                            // 决定依据
    "type": "weekly_shift",             // weekly_shift | temporary_extension | none
    "weekday": "sun", "local_date": "2026-11-01",
    "local_start": "01:30", "local_end": "05:00",
    "utc_start": "2026-11-01T05:30:00Z", "utc_end": "2026-11-01T10:00:00Z"
  },
  "next_transition_utc": "2026-11-01T10:00:00Z",  // 下一次状态切换（UTC），无则为 null
  "next_state": "locked"                // 切换后的状态，无则为 null
}
```

`locked=true` 表示当前时间不在任何每周时段与临时延期之内，工作区应被锁定。
下一次状态切换通过对所有允许区间（每周时段在本地日期窗口内展开为 UTC +
临时延期）求并集后计算：当前解锁时为并集区间终点，当前锁定时为下一区间起点。

## 已交付的时区边界样例（供值班人员核对换算）

| case_id | 场景 | 关键换算 |
| --- | --- | --- |
| `dst-overlap-ny-2026-11-01` | 纽约秋季拨回，01:30 出现两次 | 班次起点取较早瞬间：01:30 EDT = **05:30Z**；05:00 EST = 10:00Z；now=05:45Z → 不锁定，下次切换 10:00Z |
| `dst-gap-ny-2026-03-08` | 纽约春季拨快，02:30 不存在 | 班次起点顺延到 03:00 EDT = **07:00Z**；临时延期 06:00–06:15Z 已过期（右开）；now=06:30Z → 锁定，下次切换 07:00Z |

样例输入在 `samples/case_*.json`，期望输出在 `samples/case_*.expected.json`，
由 `tools/local_verify.py` 逐条比对实际输出。

## 项目结构

- `app.py` — CLI 主入口（读取输入 JSON，输出报告 JSON）
- `lock_scheduler.py` — 核心逻辑（DST 换算、班次展开、区间并集、状态判定）
- `tests/test_lock_scheduler.py` — 17 项单元测试，逐条映射需求规则
- `samples/` — 两条时区边界样例及期望输出
- `tools/local_verify.py` — 本地核验入口

## 限制

- 仅本地试算，不连接任何真实终端、云、身份或邮件系统。
- 时区数据来自系统 tzdata（`zoneinfo`）；使用 IANA 时区名。
- 每周时段按本地日期窗口（当前日前 2 天至后 10 天）展开；无任何时段与延期时
  输出 `locked=true` 且 `next_transition_utc=null`。
- 所有工作区共用同一份排程（输入未规定按工作区分别排程）。
