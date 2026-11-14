# SEC-20261114-014 — 临时诊断探针窗口控制器（模拟设备）

生产诊断需要临时开放一个可读探针，窗口到期后自动回到原先的受管状态。本仓库用
**Python 3 标准库**实现该控制器，并配套一个**纯本地、文件持久化的模拟设备集群**，
无需任何真实设备、网络或外部服务即可完整跑通整个生命周期。

## 目录结构

```
app.py                  # CLI 主入口（所有命令输出 JSON 到 stdout）
controller.py           # 控制器核心：状态机、版本门控、到期恢复、回执幂等、持久化
tests/test_controller.py# 需求映射单元测试（unittest，稳定 case_id）
tools/local_verify.py   # 本地核验包装器（单测 + 端到端样例/边界断言）
README.md
```

## 一键核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

包装器通过 `unittest.defaultTestLoader.discover("tests")` 发现测试，要求发现数 ≥ 1、
实际执行且未跳过数 ≥ 1，任何失败/错误/零测试都以非零退出；随后用 `app.py` 在临时
状态目录里跑通端到端样例（版本门控开启 → 边界时刻到期 → 恢复原值 → 重复回执幂等
→ 人为变更转人工 → 离线重连优先处理到期窗口），逐条打印实际结果与期望值。

## 业务语义

一次开启请求指定 **设备、探针、写入值、工单、批准版本、起止时段**：

1. **版本门控**：仅当设备当前配置版本与工单批准版本一致时才允许开启；设备离线、
   版本不符、时段非法（end ≤ start）都会被拒绝且不产生任何副作用。
2. **开启记录**：开启成功时窗口记录探针**原值**（`original_value`）与本次
   **写入版本令牌**（`written_version = managed-<window_id>`），该令牌同时盖在
   设备对应探针的标记上；受管基线版本不被控制器写操作扰动。
3. **到期恢复**：到期（`end <= now`）时，仅当设备仍保有本次写入版本令牌且基线
   版本未被改动，才写回原值并迁移到 `CLOSED_RESTORED`。
4. **人为变更转人工**：窗口期内任何带外人为修改（`human-edit`，版本变为
   `human-<n>`）都会在到期时被检测出来，窗口迁移到 `ESCALATED`，现场值原样
   保留等待人工处理。
5. **离线/重连**：设备离线时到期窗口转入 `RESTORE_PENDING`；设备重连
   （`reconnect`）时**优先处理该设备已经到期的窗口**，按到期时间从早到晚，
   未到期的窗口不受影响。
6. **重启恢复**：所有窗口、设备、回执持久化在状态目录的 JSON 文件中，控制器
   重启（新实例指向同一状态目录）后能找回未关闭的窗口并继续到期处理。
7. **回执幂等**：每次设备写入产生带回执 ID 的回执；同一回执重复投递只产生
   **一次**状态迁移，重复投递被记录为 `duplicate` 且不再改变状态。

### 窗口状态机

```
OPENING --open-ack--> OPEN --到期且版本在位--> RESTORING --restore-ack--> CLOSED_RESTORED
                      OPEN --到期且设备离线--> RESTORE_PENDING --重连后重试--> RESTORING/ESCALATED
                      OPEN|RESTORE_PENDING --检测到人为变更--> ESCALATED
```

## CLI 用法

```bash
PY=/usr/local/bin/python3
S=./state   # 状态目录，可任意指定

# 注册模拟设备（配置版本 v7，探针 temp=20）
$PY app.py --state-dir $S init-device --device dev-1 --version v7 --probe temp=20

# 申请开窗（工单 WO-1，批准版本 v7，09:00–10:00）
$PY app.py --state-dir $S open --device dev-1 --probe temp --value ro-diag \
    --work-order WO-1 --approved-version v7 \
    --start 2026-11-14T09:00:00 --end 2026-11-14T10:00:00 --now 2026-11-14T09:00:00

# 推进时钟，处理到期窗口
$PY app.py --state-dir $S tick --now 2026-11-14T10:00:00

# 模拟人为变更 / 离线 / 重连
$PY app.py --state-dir $S human-edit --device dev-1 --probe temp --value 99
$PY app.py --state-dir $S offline --device dev-1
$PY app.py --state-dir $S reconnect --device dev-1 --now 2026-11-14T10:30:00

# 重投递一张已存在的回执（幂等）
$PY app.py --state-dir $S receipt --receipt-id rcpt-win-WO-1-dev-1-temp-restore

# 查看全部状态
$PY app.py --state-dir $S status
```

### 输入格式

- 时间：naive ISO-8601（如 `2026-11-14T10:00:00`）；不传 `--now` 时使用系统时钟（UTC）。
- 探针值一律按字符串处理（模拟设备不做类型解释）。
- 所有命令向 stdout 输出单个 JSON 文档；`open` 输出 `accepted`/`reason`/`window`，
  `tick`/`reconnect` 输出按到期时间排序的 `actions` 列表。

### 状态文件（状态目录内）

| 文件 | 内容 |
| --- | --- |
| `devices.json` | 模拟设备：在线状态、配置版本、探针值、每探针写入版本令牌 |
| `windows.json` | 窗口记录：原值、写入版本、工单、时段、状态、迁移历史 |
| `receipts.json` | 已见回执及是否已处理（幂等依据） |
| `requests_log.json` | 开启请求审计日志（含被拒绝的请求及原因） |

## 需求 → 测试映射

| 需求 | case_id | 位置 |
| --- | --- | --- |
| 版本一致才能开启（含离线/非法时段拒绝） | SEC-014-REQ-01 | `TestOpenGating`（4 例） |
| 开启记录原值与所写版本 | SEC-014-REQ-02 | `TestOpenRecording` |
| 到期恢复原值（含 end 恰等于 now 的边界） | SEC-014-REQ-03 | `TestExpiryRestore`（3 例） |
| 随后人为变更转人工（含改动其他探针） | SEC-014-REQ-04 | `TestHumanChangeEscalation`（2 例） |
| 重连优先处理已到期窗口 | SEC-014-REQ-05 | `TestReconnectPriority`（2 例） |
| 重启后找回未关闭窗口 | SEC-014-REQ-06 | `TestRestartRecovery` |
| 重复回执只迁移一次 | SEC-014-REQ-07 | `TestReceiptIdempotency`（2 例） |
| 关闭后重复 tick 幂等 | SEC-014-REQ-08 | `TestExpiryIdempotency` |

另由 `tools/local_verify.py` 的 SAMPLE-01…07 通过 CLI 端到端复核上述链路。

## 限制

- 设备为本地文件模拟的适配器，不连接任何真实设备、云、身份或邮件系统。
- 单进程、单状态目录；未实现并发访问锁。
- 时钟由调用方注入（`--now`）或使用系统 UTC 时钟，无后台定时器。
- 探针值按不透明字符串处理；窗口 ID 由工单/设备/探针派生，同一组合存在
  活跃窗口时重复开启会被拒绝（`window-already-open`）。
