# SEC-20261114-004 — 跨应用登出事件定向清退

应用 A / B 通过同一身份提供方（issuer）建立本地会话。本模块接收**已验证**的登出事件，
只让事件对应的那批会话失效：单会话事件按 `(签发方, 会话标识)` 关联，账号清退事件按
`(签发方, 账号)` 关联，且两者都只影响**事件签发时已经建立**的会话
（`login_time <= issued_at`）。

纯 Python 3 标准库实现，无第三方依赖。

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `app.py` | CLI 主入口：读取场景 JSON，执行事件处理并输出结果 JSON |
| `models.py` | 数据模型：`SessionRecord` / `LogoutEvent` / `PurgeRecord` / `AppPurgeDetail` |
| `service.py` | 核心服务 `LogoutService`：事件匹配、去重、清退、重试 |
| `adapters.py` | 可控应用适配器 `ControllableAppAdapter`：模拟应用超时与用户重新登录 |
| `tests/` | unittest 测试（case_id `SEC004-Txx`） |
| `samples/` | 样例场景（case_id `SEC004-Sxx`，由核验脚本断言） |
| `tools/local_verify.py` | 本地核验入口 |

## 本地运行

```sh
# 一键核验（单测 + 样例断言），任一失败以非零退出
/usr/local/bin/python3 tools/local_verify.py

# 仅跑单测
/usr/local/bin/python3 -m unittest discover tests

# 跑一个场景
/usr/local/bin/python3 app.py samples/session_logout.json --pretty
```

## 输入格式（场景 JSON）

```json
{
  "sessions": [{"app": "A", "issuer": "idp-1", "account": "alice",
                "session_id": "s1", "login_time": 100}],
  "events":   [{"event_id": "e1", "issuer": "idp-1", "issued_at": 200,
                "type": "session", "session_id": "s1"},
               {"event_id": "e2", "issuer": "idp-1", "issued_at": 210,
                "type": "account", "account": "alice"}],
  "adapter":  {"timeout_apps": ["B"], "failures": {"B": 1}},
  "relogins": [{"app": "B", "issuer": "idp-1", "account": "alice",
                "session_id": "s9", "login_time": 300}],
  "retry":    true
}
```

- `sessions`：事件到达前的本地会话记录（应用、签发方、账号、会话标识、登录时间）。
- `events`：按送达顺序处理的已验证登出事件。`type=session` 需带 `session_id`；
  `type=account` 需带 `account`。`event_id` 重复送达只记一次清退。
- `adapter`（可选）：`timeout_apps` 中的应用调用一律超时；`failures` 让某应用
  接下来 N 次调用瞬时超时（用于演示"保留待处理 + 重试补做"）。
- `relogins`（可选）：事件处理之后、重试之前模拟用户重新登录（新登录时间更晚，
  因此不会被已签发的事件波及）。
- `retry`（可选）：最后对所有仍待处理的清退执行重试，**只补做失败的应用**。

## 输出语义（stdout JSON）

- `events[]`：每个事件的 `event_id`、`duplicate`（是否重复送达）、`purge_status`。
- `purges[]`：每次清退的处理明细（每个 `event_id` 恰好一条）：
  - `status`：`completed`（全部应用成功）或 `pending`（仍有失败应用）；
  - `duplicate_count`：重复送达次数；
  - `apps{<app>}`：该应用的 `status`（`success`/`failed`）、`attempts`、
    `error`、`session_ids` 及逐次 `history`（每次尝试的结果）。
- `retried[]`：本轮执行过重试的事件 ID。
- `active_sessions[]`：处理结束后仍有效的会话。
- `adapter_calls[]`：适配器收到的全部调用及结果（`success`/`timeout`），
  用于核对"重试只补做失败应用"。

## 需求 → 测试映射

| 需求 | case_id | 位置 |
| --- | --- | --- |
| 单会话通知按签发方+会话标识关联，不误伤同账号其他会话/其他签发方 | SEC004-T01 | `tests/test_logout_service.py::TestSessionEventCorrelation` |
| 账号清退按签发方+账号关联，跨应用清退该账号全部会话 | SEC004-T02 | `TestAccountPurgeCorrelation` |
| 只影响签发时已建立的会话（含 `login_time == issued_at` 边界） | SEC004-T03 | `TestIssuedAtCutoff` |
| 事件重复送达只记一次清退，不重复调用应用 | SEC004-T04 | `TestDuplicateDelivery` |
| 应用暂时失败时保留待处理状态，会话保持有效 | SEC004-T05 | `TestAppFailureKeepsPending` |
| 重试只补做失败应用，成功应用不被重复调用 | SEC004-T06 | `TestRetryOnlyFailedApps` |
| 重新登录后的新会话不被旧签发时间的事件清退 | SEC004-T07 | `TestReloginSurvivesOldEvent` |
| 适配器可模拟超时/重登录，清退处理明细可查看 | SEC004-T08 | `TestProcessingDetailsVisible` |
| 畸形事件被拒绝且不产生清退记录 | SEC004-T09 | `TestEventValidation` |
| 端到端：单会话事件 + 重复送达（CLI 样例） | SEC004-S01 | `samples/session_logout.json` |
| 端到端：账号清退 + 应用失败 + 重登录 + 重试（CLI 样例） | SEC004-S02 | `samples/account_purge_retry.json` |
| 端到端：`issued_at` 边界（CLI 样例） | SEC004-S03 | `samples/boundary_issued_at.json` |

## 限制

- 事件假定已在模块外完成身份验证（签名/来源校验不在本模块范围内）。
- 状态保存在内存中，进程退出即丢失；无持久化、无并发控制。
- 时间为调用方提供的整数逻辑时间，模块不读取系统时钟。
- 外部身份系统与应用均以本地适配器/合成记录模拟，不访问任何真实外部系统。
