# SEC-20261114-005 — 历史聊天附件两步清理（本地目录模拟）

仅用 Python 3 标准库实现，无第三方依赖。用本地目录模拟"文件索引 / 保留规则 / 法务冻结 / 确认清单 / 执行日志"，完整演示两步清理流程。

## 流程语义

1. **plan（运营确认）**：扫描文件索引，把"按业务空间保留时长已过期"的附件写入确认清单 `confirmed.json`，每条记录**确认时刻的内容版本号**（`confirmed_version`）。开始新一轮 plan 会使上一轮的执行日志失效（重新开始）。
2. **execute（程序执行）**：逐条处理确认过的条目。**真正删除前逐项重新读取**当前冻结、保留规则和内容版本：
   - 附件已不在索引 → 跳过，理由 `missing_from_index`
   - 当前版本 ≠ 确认版本（附件被替换）→ 跳过，理由 `version_changed`，**确认清单不对新版本生效**，新文件保留
   - 存在任一有效冻结（针对单个附件或整个案件，多个有效冻结中任意一个即阻止）→ 跳过，理由 `legal_hold:<hold_id>`
   - 按当前保留规则未过期 → 跳过，理由 `within_retention`
   - 索引路径越出数据目录 → 跳过，理由 `unsafe_path`；索引在但文件不在 → `file_missing`
   - 删除时发生 I/O 错误 → 标记 `pending_retry`，重跑时会重试
   - 否则删除文件并从索引移除，标记 `deleted`
3. **断点续跑**：每条处理结果即时写入 `state/journal.json`（原子写）。执行到一半重跑时，`deleted`/`skipped` 为终态不再处理（结果来自日志，`from_journal=true`），`pending`/`pending_retry` 会继续处理。结果区分**已删 / 已跳过 / 待重试 / 待处理**四类，汇总写入 `result.json`。跳过为终态：冻结解除后需运营重新 plan 一轮才会重新考虑。

## 快速开始

```bash
cd <workspace>
/usr/local/bin/python3 app.py demo    --data demo_data
/usr/local/bin/python3 app.py plan    --data demo_data --now 2026-11-14
/usr/local/bin/python3 app.py execute --data demo_data --now 2026-11-14
/usr/local/bin/python3 app.py status  --data demo_data
```

`--now` 指定"今天"（ISO 日期，默认真实当天），用于确定性重跑。重复执行 `execute` 演示断点续跑。

## 一键核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

- 通过 `unittest.defaultTestLoader.discover("tests")` 发现并运行全部单测；发现 0 项、全部跳过、异常或任何失败都以非零退出。
- 随后执行端到端样例/边界断言（稳定 case_id），逐条把"期望值 / 实际值 / 是否通过"打印并写入 `verification_results.json`。
- 总耗时 < 1 秒，远低于 80 秒预算。

## 输入格式（数据目录内）

| 文件 | 结构 |
|---|---|
| `spaces.json` | `{"spaces": {"<space>": {"retention_days": N}}}` — 按业务空间的保留天数 |
| `index.json` | `{"attachments": [{"attachment_id", "case_id", "space", "path", "version", "created_at"}]}` — 文件索引，`path` 相对数据目录，`version` 为内容版本 |
| `holds.json` | `{"holds": [{"hold_id", "scope": "attachment"\|"case", "target_id", "active"}]}` — 法务冻结 |
| `confirmed.json` | plan 产物：`{"generated_at", "entries": [{"attachment_id", "case_id", "space", "confirmed_version"}]}` |
| `state/journal.json` | execute 产物：每条 `{status, reason, confirmed_version, updated_at}` |
| `result.json` | execute 产物：`{counts: {deleted, skipped, pending_retry, pending}, outcomes: [...]}` |

过期判定：`age_days >= retention_days`（恰好满保留期当天即可删）。

## 需求 → 验证映射

| 原始需求 | 单测（tests/test_cleanup.py） | 样例断言（local_verify case_id） |
|---|---|---|
| 两步：先确认清单再逐条执行 | PlanTests.*, ExecuteDeleteTests.* | CASE-EXPIRED-DELETE |
| 按业务空间给出保留时长 | test_per_space_retention, test_retention_boundary_exactly_expired | CASE-RETENTION-BOUNDARY, CASE-RETENTION-FRESH |
| 冻结可针对单附件或整案；任一有效冻结即阻止 | HoldTests.*（4 个用例） | CASE-HOLD-CASE, CASE-HOLD-ANY-ACTIVE, CASE-HOLD-INACTIVE |
| 索引记录内容版本；删除前逐项重读冻结/规则/版本 | RecheckTests.test_version_change…, test_retention_rule_tightened… | CASE-VERSION-REPLACED |
| 被替换或新纳入冻结 → 写出跳过理由；确认清单不对新版本生效 | test_version_change_after_confirm_skips_and_preserves, test_hold_added_after_confirm_skips | CASE-VERSION-PRESERVED, CASE-HOLD-NEW |
| 本地目录模拟；中途重跑接着处理剩余条目 | ResumeTests.test_interrupted_run_resumes…, test_rerun_continues… | CASE-RESUME-JOURNAL, CASE-SKIPPED-TERMINAL |
| 区分已删 / 已跳过 / 待重试 | test_status_summary_distinguishes_categories, test_pending_retry_on_io_error_then_resume_deletes | CASE-RETRY-FIRST-RUN, CASE-RETRY-RESUME, CASE-RESULT-COUNTS |

## 限制

- 纯本地模拟：不连接真实云存储、身份、邮件或生产系统；冻结/规则/索引均为本地 JSON。
- 时间粒度为日期（非时刻）；`--now` 用于确定性测试。
- `skipped` 是本轮终态：状态变化（如冻结解除）后需重新 plan 一轮，execute 不会自动重估。
- 删除只移除文件并更新索引，不清理遗留空目录。
