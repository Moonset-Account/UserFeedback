# SEC-20261114-055 — MFA 挑战与一次性导出许可兑换

仅使用 Python 3 标准库实现。核心保证：**同一张导出许可被两个请求同时兑换时，
只有一个请求能拿到租户的密钥访问审计记录**（兑换的"检查-置位"在单把锁内完成）。

## 结构

```
app.py                  CLI 主入口（场景演示）
mfa_export/
  service.py            三个接口：发起 MFA / 确认挑战 / 兑换许可
  adapters.py           本地测试适配器：可控时钟、假 SSO、假 MFA
  audit.py              审计记录（判定结果 + 原因，不含敏感凭证）
tests/test_service.py   单元测试（CASE-055-01 ~ CASE-055-14）
tools/local_verify.py   本地核验入口
```

## 运行

```bash
cd <workspace>
/usr/local/bin/python3 tools/local_verify.py        # 核验：单测 + 样例/边界断言
/usr/local/bin/python3 app.py scenarios             # 列出可重现场景
/usr/local/bin/python3 app.py demo all              # 运行全部场景，输出 JSON
/usr/local/bin/python3 app.py demo concurrent_redeem
/usr/local/bin/python3 -m unittest discover tests   # 仅跑单测
```

## 接口语义

三个接口都在 `mfa_export.service.ExportLicenseService` 上：

| 接口 | 输入 | 输出（ok=True） |
|---|---|---|
| `initiate_mfa(sso_token, export_scope)` | SSO token、导出范围 | `challenge_id`、`expires_at` |
| `confirm_challenge(challenge_id, mfa_proof)` | 挑战 ID、MFA 凭证 | `license_id`、`expires_at` |
| `redeem_license(license_id, sso_token)` | 许可 ID、当前 SSO token | `payload`（租户密钥访问审计记录） |

- **发起**：SSO 适配器验证 token 得到已验证的租户/用户/会话；挑战绑定该
  租户、用户、会话与导出范围；挑战 **120 秒**内可确认（`now < expires_at`，
  到达即过期）。
- **确认**：MFA 适配器返回指定挑战的验证结果；**每个挑战最多签发一张许可**；
  许可自签发起 **300 秒**内可兑换。
- **兑换**：重新核对当前会话（会话切换拒绝）与导出权限（权限撤回拒绝）；
  同一许可的并发兑换在锁内原子完成，只有一个请求成功，其余得到
  `ERR_LICENSE_ALREADY_USED`。

## 审计原因码（判定结果及原因）

允许：`OK_CHALLENGE_ISSUED` / `OK_LICENSE_ISSUED` / `OK_LICENSE_REDEEMED`。
拒绝：`ERR_SSO_INVALID`、`ERR_EXPORT_PERMISSION_DENIED`、
`ERR_CHALLENGE_NOT_FOUND`、`ERR_CHALLENGE_EXPIRED`（挑战超时）、
`ERR_CHALLENGE_ALREADY_CONSUMED`（一挑战一许可）、
`ERR_MFA_VERIFICATION_FAILED`、`ERR_LICENSE_NOT_FOUND`、
`ERR_LICENSE_EXPIRED`、`ERR_LICENSE_ALREADY_USED`（许可已使用）、
`ERR_IDENTITY_MISMATCH`、`ERR_SESSION_MISMATCH`（会话切换）、
`ERR_PERMISSION_REVOKED`（权限撤回）。

**敏感验证凭证留在服务端**：SSO token 与 MFA proof 只传给服务端适配器，
不写入审计记录、不出现在返回值中（CASE-055-11 / SEC055-S13 验证）。

## 本地测试适配器

- `ManualClock`：`advance(seconds)` / `set(t)` 控制时钟，重现超时与边界。
- `FakeSSOAdapter`：`register_identity` 登记 token→身份；
  `set_export_permission` 随时授予/撤回导出权限（重现权限撤回）；
  给同一用户登记不同会话的 token（重现会话切换）。
- `FakeMFAAdapter`：`set_expected_proof` 控制指定挑战的验证结果。

## 需求 → 测试映射

| 原需求 | case_id |
|---|---|
| 三接口完整链路、挑战绑定租户/用户/会话/范围 | CASE-055-01 / SEC055-S01,S02 |
| 挑战两分钟内可确认（含 119/120 秒边界） | CASE-055-02,03 / SEC055-S03 |
| 每个挑战最多签发一张许可 | CASE-055-04 / SEC055-S05 |
| MFA 适配器给出验证结果，失败被拒 | CASE-055-05 / SEC055-S04 |
| 许可五分钟内可兑换（含 299/300 秒边界） | CASE-055-06 / SEC055-S06 |
| 兑换重新核对会话（会话切换） | CASE-055-07 / SEC055-S07 |
| 兑换重新核对导出权限（权限撤回） | CASE-055-08 / SEC055-S08 |
| 许可已使用（顺序 + 并发只有一个成功） | CASE-055-09,10 / SEC055-S09~S12 |
| 审计区分四类原因并记录判定结果与原因 | CASE-055-02,07,08,09 / SEC055-S14 |
| 敏感凭证留在服务端 | CASE-055-11 / SEC055-S13 |
| SSO 提供已验证身份，无效 token 被拒 | CASE-055-12,13,14 |

## 限制

- 单进程内存实现：状态与审计不持久化，进程退出即丢失。
- 并发保证基于单把 `threading.RLock`，仅覆盖单进程内并发；
  跨进程/分布式一次性语义需要外部原子存储（不在本题范围）。
- SSO/MFA 为本地合成适配器，不连接真实身份系统；导出载荷为合成数据。
- 时间语义为严格小于 `expires_at` 有效；时钟由调用方注入。
