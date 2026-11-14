# SEC-20261114-018 — 邮件隔离区按收件人审查释放（本地状态模块）

同一封隔离邮件的每个收件人拥有独立的隔离记录与释放任务：某位收件人申诉
成功只释放其本人副本，不替其他收件人解封。本仓库提供一个纯 Python 3
标准库的本地状态模块，从投递结果与审批事件生成可释放任务，并提供幂等
的释放接口与操作员状态视图。

## 运行环境

- Python 3（仅标准库），解释器路径：`/usr/local/bin/python3`
- 不使用 pip / venv / pytest / 任何外部库；不联网

## 本地入口（均可重跑）

```bash
# 1) 一键核验（unittest 套件 + samples/ 下逐条样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py

# 2) 仅跑单元测试
/usr/local/bin/python3 -m unittest discover tests

# 3) CLI：从文件或标准输入读取 JSON 命令批次
/usr/local/bin/python3 app.py samples/bc01_multi_recipient.json
cat samples/bc04_release_idempotency.json | /usr/local/bin/python3 app.py -
```

## 输入格式

`app.py` 接受一个 JSON 命令列表（或含 `commands` 列表的对象）。每条命令
含 `op` 字段：

| op | 必填字段 | 语义 |
| --- | --- | --- |
| `record_delivery` | `email`, `recipient`, `version`, `reasons` | 按 （邮件， 收件人） 登记/更新隔离投递记录；`reasons` 为当前命中的全部隔离原因（追加新原因即传更新后的全集） |
| `submit_approval` | `email`, `recipient`, `approval_id`, `version`, `reasons` | 审批事件：必须匹配同收件人、同当前版本；`reasons` 为该审批覆盖的原因 |
| `release` | `email`, `recipient`, `request_id` | 释放请求；`request_id` 标识一次申请，重试幂等 |
| `confirm_downstream` | `email`, `recipient`, `confirmation_id` | 下游释放确认回调，同一确认只记录一次 |
| `status` | `email` | 操作员视图：该邮件下各收件人状态 |

标识符（`email`/`recipient`/`version`/`approval_id`/`request_id`）均为非空
字符串，模块不解析其格式。

## 输出语义

输出为 `{"results": [...]}`，每条命令对应一项：`ok=true` 时带 `result`，
`ok=false` 时带 `error`（业务错误不会中断后续命令）。进程退出码：批次
成功解析并执行恒为 0；输入非法或用法错误为 2。

状态机（按 （邮件， 收件人） 计算）：

- `waiting`：仍有有效隔离原因未被同版本审批覆盖；
- `releasable`：同版本审批已覆盖全部仍有效原因，可释放；
- `released`：当前版本已执行过释放。

关键字段：

- `submit_approval` 返回 `missing_reasons`（仍缺覆盖的原因）与
  `idempotent_replay`（同一 `approval_id` 同载荷重放）。
- `release` 返回 `executed`、`downstream_confirmation_sent`、
  `idempotent_replay`、`duplicate_of_prior_release`：
  - 同一 `request_id` 重试 → `idempotent_replay=true`，不产生新的下游确认；
  - 已释放后新的 `request_id`（第二次独立申请）→ 仅登记，
    `duplicate_of_prior_release=true`，不重复发下游确认；
  - 释放请求绑定首次提交时的版本，版本变更后需用新的 `request_id` 申请。
- 下游确认 `confirmation_id` 形如 `cfm:<email>:<recipient>:<version>`，
  `confirm_downstream` 对同一确认只记录一次（`acknowledged_count` 恒为 1）。

## 需求 → 测试/样例映射

| 原始需求 | case_id | 覆盖位置 |
| --- | --- | --- |
| 同一邮件多收件人，一人申诉成功不替他人解封 | SEC018-RQ-01 | `tests/test_quarantine.py::RQ01MultiRecipientIsolationTests`；`samples/bc01_multi_recipient.json` (SEC018-BC-01) |
| 投递记录按 （邮件， 收件人） 定位，含当前版本与命中原因 | SEC018-RQ-02 | `RQ02DeliveryRecordTests` |
| 审批须同收件人、同版本并覆盖全部仍有效原因 | SEC018-RQ-03 | `RQ03ApprovalCoverageTests`；`samples/bc02_approval_coverage.json` (SEC018-BC-02) |
| 新隔离原因追加后，已有释放任务重新等待审查 | SEC018-RQ-04 | `RQ04ReasonAppendTests`；`samples/bc03_reason_appended.json` (SEC018-BC-03) |
| 释放区分同请求重试与第二次独立申请；下游确认只记一次 | SEC018-RQ-05 | `RQ05ReleaseIdempotencyTests`；`samples/bc04_release_idempotency.json` (SEC018-BC-04) |
| 操作员查看邮件下各收件人 等待/可释放/已释放 状态 | SEC018-RQ-06 | `RQ06OperatorStatusViewTests`；`samples/bc05_status_view.json` (SEC018-BC-05) |
| CLI 批次端到端执行与逐命令错误报告 | SEC018-BC-CLI | `CliBatchTests` |

`tools/local_verify.py` 通过 `unittest.defaultTestLoader.discover("tests")`
发现测试并检查 `countTestCases() >= 1`，实际运行后要求"实际执行且未跳过"
的测试数 >= 1，发现 0 项、全部跳过、任一失败/错误均以非零退出；随后逐个
回放 `samples/` 场景并逐条打印 `expected`/`actual` 与 PASS/FAIL。

## 限制

- 状态仅保存在内存中，进程退出即丢失；无持久化、无并发控制。
- 下游邮件系统以模块内确认记录模拟，不连接真实邮件/云/身份系统。
- 释放请求的幂等键为 `request_id`，由调用方保证其唯一性；释放请求绑定
  首次提交时的内容版本。
- 已执行释放是历史事实：同版本追加新原因不会撤销已释放副本，但版本变更
  会开启新的审查周期（回到 `waiting`）。
