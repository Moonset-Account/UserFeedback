# 财务权限批次：录入/批准职责分离审查

一个仅使用 Python 标准库的权限开通审查模块。它根据申请批次的**最终有效状态**，判断同一用户是否会在同一项目、同一有效时间段内同时拥有：

- 录入收款账户职责：`payment_account:enter`
- 批准收款账户职责：`payment_account:approve`

模块同时计算个人/直接授予与团队继承授予，并在冲突中返回重叠时间段以及每项职责的有效授予来源。

运行环境已用 Python 3.11.6 验证；不需要安装第三方依赖，不访问网络。

## 公开 API

推荐从 `access_review` 包导入：

```python
from access_review import review_batch, SodReviewer, TimeWindow

report = review_batch(payload)

if report["status"] == "suspended":
    ...
```

主要公开对象：

- `review_batch(payload=None, **kwargs) -> ReviewReport`：默认策略的一键审查函数。也支持 `review_batch(roles=..., grants=..., ...)` 关键字形式。
- `SodReviewer(rules=None, *, extra_duty_aliases=None, block_on_unrelated_conflicts=True)`：可复用审查器。
- `DutyRule`：自定义互斥职责规则。
- `TimeWindow`：有效期、交集和端点包含语义。
- `ENTER_DUTY`、`APPROVE_DUTY`、`GLOBAL_PROJECT`：默认职责和全局项目常量。
- `ReviewReport`：普通字典报告，另提供 `.status`、`.suspended`、`.allowed`、`.conflicts`、`.blocking_conflicts` 属性。
- `ReviewInputError`：结构性或时间格式输入错误。

兼容导入名：

- `from review import review_batch`
- `from sod_review import review_batch`
- `from permission_review import review_batch`
- 别名函数：`review`、`review_request`、`review_permission_batch`、`review_access`、`review_permissions`、`review_grants`、`check_batch`、`evaluate_batch`
- 类别名：`AccessReviewer`、`PermissionReviewer`、`SoDReviewer`

## 命令行

```bash
python3 -m access_review examples/clear_replacement.json
python3 -m access_review examples/team_inheritance_conflict.json
```

加 `--compact` 输出单行 JSON；加 `--strict` 时，如果状态不是 `approved`，进程以状态码 `3` 退出。

## 输入格式

输入是 JSON 兼容字典。核心字段如下：

```jsonc
{
  "batch_id": "B-123",
  "roles": {
    "entry-clerk": ["payment_account:enter"],
    "approver": ["payment_account:approve"]
  },
  "existing_grants": [
    {
      "id": "old-entry",
      "user_id": "u1",
      "role_id": "entry-clerk",
      "project_id": "prj-1",
      "valid_from": "2026-01-01T00:00:00Z",
      "valid_to": "2026-03-01T00:00:00Z"
    }
  ],
  "grants": [
    {
      "id": "new-approval",
      "user_id": "u1",
      "role_id": "approver",
      "project_id": "prj-1",
      "valid_from": "2026-02-01T00:00:00Z",
      "valid_to": "2026-04-01T00:00:00Z"
    }
  ],
  "revocations": [
    {"grant_id": "old-entry"}
  ],
  "team_grants": [
    {
      "id": "team-entry",
      "team_id": "finance",
      "role_id": "entry-clerk",
      "project_id": "prj-1",
      "valid_from": "2026-01-01",
      "valid_to": "2026-12-31"
    }
  ],
  "team_memberships": [
    {
      "id": "m1",
      "user_id": "u1",
      "team_id": "finance",
      "valid_from": "2026-01-01",
      "valid_to": "2026-06-30"
    }
  ]
}
```

支持常见别名字段，例如：

- 用户：`user_id`、`user`、`subject_id`、`principal_id`、`employee_id`、`username`
- 项目：`project_id`、`project`、`project_code`、`scope`、`project_scope`
- 角色：`role_id`、`role`、`role_code`
- 职责：`duties`、`responsibilities`、`permissions`、`actions`
- 有效期：`valid_from`/`valid_to`、`starts_at`/`ends_at`、`effective_from`/`effective_to`，也支持嵌套 `validity` 或 `period`
- 批次操作：除 `grants` 和 `revocations` 外，也可放在 `changes`、`operations`、`items`、`batch_items` 中，用 `op`/`operation`/`action` 区分 `grant` 与 `revoke`

角色也支持 `includes`/`inherits` 包含其他角色。常见中英文职责名会归一化到两个默认常量，例如 `create_payment_account`、`录入收款账户` 归为录入，`approve`、`批准收款账户`、`审批` 归为批准。

### 撤销与整批最终状态

审查器先构造整批的最终状态，而不是逐条独立判断：

1. 以 `existing_grants` 为当前个人/直接授予。
2. 显式 `grant_id`/`target_grant_id` 撤销同时作用于旧授予和同批授予；如果同批既授予又按同一 ID 撤销，该授予不会进入最终状态。
3. `grants` 中相同 ID 的记录覆盖旧记录，表示整批后的授予状态。
4. 不带 ID、但包含 `user_id`/`role_id`/`project_id`/`source_type`/`team_id` 选择条件的撤销，用于匹配当前旧授予。
5. 个人角色的条件撤销默认不会撤销团队授予；团队来源必须通过团队授予 ID、`team_id` 或 `source_type: "team"` 明确匹配。
6. 授予中的 `replaces`、`replaces_grant_id`、`old_grant_id` 也会把旧授予 ID 计入同批撤销集合。

因此，“同批撤销旧录入职责，再开通批准职责”的替换申请不会再因为旧职责产生误报。

### 团队继承和有效期

团队职责的有效区间是：

```text
团队授予有效期 ∩ 用户团队成员有效期
```

如果没有提供成员名册，但紧凑格式的团队授予直接带了 `user_id`，该用户被视为有效成员。若提供了成员名册但找不到匹配成员，则不会虚构继承来源。

项目作用域按精确项目匹配；`"*"`、`"global"`、`"all"` 或空作用域视为全局。全局授予与具体项目授予冲突时，冲突项目解析为该具体项目；两边都为全局时，冲突项目为 `"*"`。

### 时间边界

- ISO 8601 日期时间会被解析；不带时区的值按 UTC 处理，输出为 UTC。
- 日期时间区间默认是半开区间 `[start, end)`。例如 `[2026-01-01, 2026-02-01)` 与 `[2026-02-01, 2026-03-01)` 在边界点不重叠。
- 纯日期的 `valid_to`/`end_date` 按常见 IAM 习惯视为**包含当天**，报告中保留日期并给出 `end_inclusive: true`。
- 可通过 `start_inclusive`、`end_inclusive` 显式控制端点。
- `start` 或 `end` 为 `null` 表示该侧无界。

## 冲突报告

无冲突时：

```json
{
  "status": "approved",
  "decision": "approve",
  "suspended": false,
  "allowed": true,
  "conflicts": []
}
```

有冲突或无法完成安全判断时：

- `status: "suspended"` / `decision: "suspend"`：最终状态存在职责冲突，应暂停批次并人工复核。
- `status: "error"` / `decision: "manual_review"`：角色未定义、撤销条件为空、批次条目无法识别等，按失败关闭处理，`allowed` 为 `false`。

顶层同时提供 `state`、`outcome`、`blocked` 等别名字段；`blocked` 与 `suspended` 同义。

每个 `conflicts[]` 包含：

```jsonc
{
  "conflict_id": "SOD:payment-account:entry-vs-approval:u-2002:prj-finance",
  "user_id": "u-2002",
  "project_id": "prj-finance",
  "entry_duty": "payment_account:enter",
  "approval_duty": "payment_account:approve",

  // 每项职责的去重后有效来源
  "entry_sources": [ /* grant_id, source_type=direct|team, role, team, membership, validity... */ ],
  "approval_sources": [ /* ... */ ],
  "duty_sources": {
    "payment_account:enter": [ /* ... */ ],
    "payment_account:approve": [ /* ... */ ]
  },
  "responsibilities": [
    {"duty": "payment_account:enter", "label": "entry", "sources": []},
    {"duty": "payment_account:approve", "label": "approval", "sources": []}
  ],

  // 每一对来源的具体交集
  "overlaps": [
    {
      "start": "2026-03-01",
      "end": "2026-09-30",
      "start_inclusive": true,
      "end_inclusive": true,
      "entry_source_id": "team:team-entry-source:...",
      "approval_source_id": "direct:enable-approval",
      "entry_grant_id": "team-entry-source",
      "approval_grant_id": "enable-approval"
    }
  ],

  // 去重后的重叠时间段
  "overlap_periods": [ /* start/end/inclusivity */ ],
  "overlapping_periods": [ /* 同 overlap_periods */ ],

  "batch_item_ids": ["ticket-7788"],
  "batch_relevant": true,
  "blocking": true,
  "recommended_action": "Suspend this batch ..."
}
```

报告还包含 `effective_sources`（最终状态中的全部职责来源）和 `changes`（授予、撤销匹配、最终授予 ID 列表），便于审计。

默认情况下，只要最终状态存在冲突就暂停整批。若调用方只希望暂停与本批授予来源直接相关的冲突，可使用 `SodReviewer(block_on_unrelated_conflicts=False)`；此时报告仍返回全部冲突，并用 `batch_relevant`/`blocking` 区分。

## 题面关键场景

场景：撤销个人录入角色，但用户仍通过 `team-finance-ops` 团队继承录入权，同时本批开通批准职责。

本地合成示例：`examples/team_inheritance_conflict.json`。

实际 CLI 复核摘要：

```text
suspended suspend True
prj-finance [('team', 'team-entry-source')] [('direct', 'enable-approval')]
[{'start': '2026-03-01', 'end': '2026-09-30', 'start_inclusive': True, 'end_inclusive': True}]
```

因此，即使个人录入授予已在同批撤销，只要团队来源仍有效且时间重叠，本批批准职责仍必须暂停。

替换撤销的正常示例：`examples/clear_replacement.json`，CLI 输出 `"status": "approved"`，且 `g-old-entry` 出现在 `changes.revoked_existing_grant_ids`。

## 运行测试

在工作区根目录执行题目要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实际执行共收集并通过 26 个测试，结果为 `OK`（详细输出包含 26 个 `ok` 用例；耗时随机器略有差异）。

测试与本地合成数据覆盖：

- 正常批准：无授予、仅单一职责、不同项目不冲突。
- 直接授予的时间重叠、来源和重叠区间返回。
- 同批显式撤销旧职责后，替换为批准职责应通过。
- 同批授予又按同一 ID 撤销时，该授予不进入最终状态。
- 撤销个人录入角色后，团队继承录入权仍导致批准批次暂停。
- 条件撤销个人授予不会误删团队授予；显式撤销团队授予后冲突消除。
- 团队成员有效期与团队授予有效期的交集、无交集边界。
- 同一角色同时包含录入和批准两项职责。
- 全局项目作用域与具体项目的解析。
- 多个录入/批准时间段交错时返回多个重叠区间。
- 半开区间相邻边界不重叠；显式闭区间共享端点可形成时点重叠。
- 中文职责别名、嵌套 `batch` 载荷、`changes` 操作列表。
- 一个授予引用多个角色、团队授予直接放在 `changes` 中的路由。
- 未知角色、空撤销条件、无法识别的批次条目均失败关闭。
- 报告可 JSON 序列化，CLI 正常输出且 `--strict` 冲突时返回状态码 `3`。

## 目录结构

```text
access_review/
  __init__.py        # 公开导出
  __main__.py        # python -m access_review JSON 文件
  core.py            # 归一化、最终状态、团队继承、冲突检测
  time_windows.py    # ISO 时间/日期与区间交集
review.py            # 常用顶层兼容导入
sod_review.py        # 兼容模块名
permission_review.py # 兼容模块名
examples/            # 本地合成批次
tests/               # unittest 测试
```
