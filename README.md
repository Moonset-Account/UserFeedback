# SEC-20261114-033 — 权限撤回后的分级响应缓存隔离

本地缓存层：同一资源支持**公共摘要（summary）**与**受限详情（detail）**两种视图。
仅使用 Python 3 标准库，无外部依赖。

## 文件结构

| 文件 | 说明 |
|---|---|
| `cache_layer.py` | 核心缓存层：`ResponseCache`、`AuthorizationService`、`Identity`、`LoadResult`、`ViewRecord` |
| `loaders.py` | `ControllableLoader`（可控制完成顺序的加载器）与 `StaticLoader` |
| `app.py` | CLI 主入口：复现并发查询 + 撤权 + 旧请求晚到交错场景 |
| `tests/test_cache_layer.py` | 12 项 unittest 测试 |
| `tools/local_verify.py` | 本地核验包装器（发现→实际运行→样例断言） |

## 运行方式（全部使用 /usr/local/bin/python3）

```bash
# 一键核验（单测 + 样例/边界断言，全部通过退出码为 0）
/usr/local/bin/python3 tools/local_verify.py

# 仅运行单元测试
/usr/local/bin/python3 -m unittest discover tests

# 运行演示场景（人类可读表格）
/usr/local/bin/python3 app.py

# 运行演示场景（JSON Lines，便于机器校验）
/usr/local/bin/python3 app.py --json
```

## 输入格式

调用方提供**已验证**的身份与版本信息，缓存层不自行构造身份：

- `Identity(tenant, principal)` — 已验证的租户与主体身份。
- `resource_id`、`resource_version`（可选，指定后缓存命中要求版本一致）、`view ∈ {"summary","detail"}`。
- `loader(identity, resource_id, view) -> LoadResult` — 回源加载器。
- `LoadResult(resource_id, resource_version, perm_version, shareable, summary, detail)`：
  - `perm_version`：加载**开始时**观察到的主体权限版本，写缓存前会与当前版本复核；
  - `shareable`：为 `True` 时摘要才允许进入租户级缓存。
- `AuthorizationService`：按 (tenant, principal) 维护当前权限版本；`revoke()` 使版本号 +1。

`app.py` 的演示场景为内置脚本，不需要任何外部输入文件或网络访问。

## 输出语义

每次请求追加一条 `ViewRecord`：`seq, tenant, principal, resource_id, view, source, ok, detail`。
`source` 取值：

| source | 含义 |
|---|---|
| `loader` | 回源加载并（按规则）写缓存 |
| `summary_cache` | 命中租户级摘要缓存 |
| `detail_cache` | 命中按主体隔离的详情缓存（命中时已复核当前权限版本） |
| `summary_fallback_refresh_failed` | 刷新失败，复用仍有效（未过期）的摘要 |
| `detail_fallback_refresh_failed` | 刷新失败，复用仍有效且授权仍成立的详情 |
| `denied_perm_version_changed` | 加载期间权限版本变化（撤权），旧详情被丢弃、不写回缓存 |
| `denied_refresh_failed` | 刷新失败且无仍有效/仍授权的缓存可复用 |

异常语义：详情视图授权不成立抛 `AuthorizationError`；刷新失败且无可复用缓存抛 `RefreshError`。

## 需求 → 测试/样例映射

| 需求 | 覆盖 |
|---|---|
| 摘要仅在明确标记可共享时进入租户级缓存 | `test_summary_cached_only_when_shareable`、`test_summary_shared_within_tenant_not_across_tenants` |
| 详情按主体隔离 | `test_detail_isolated_per_principal` |
| 命中详情仍确认当前权限版本 | `test_detail_hit_revalidates_perm_version` |
| 撤权后旧请求晚到不得写回旧详情 | `test_late_load_after_revocation_never_written_back`、`test_concurrent_users_revocation_interleaving`、demo[2] |
| 刷新失败复用仍有效摘要 | `test_refresh_failure_reuses_still_valid_summary`、`test_refresh_failure_with_expired_summary_raises`、demo[5] |
| 刷新失败时受限详情按授权结果处理 | `test_refresh_failure_detail_fallback_when_authorized`、`test_refresh_failure_detail_denied_after_revocation`、demo[6]/demo[7] |
| 可控完成顺序加载器复现并发/撤权/晚到交错 | `ControllableLoader` + `test_concurrent_users_revocation_interleaving` + `app.py` 演示 |
| 记录每次请求的视图来源 | `test_every_request_records_its_view_source`、`ViewRecord`、demo 全部 8 条记录 |

## 限制

- 进程内内存缓存，无持久化、无跨进程共享；TTL 基于单调时钟（测试可注入假时钟）。
- 身份与权限版本由调用方/本地 `AuthorizationService` 模拟，不连接真实身份系统。
- 加载器为本地合成实现（`StaticLoader`/`ControllableLoader`），不访问网络。
- 摘要回退复用仅要求未过期；详情回退复用同时要求未过期且权限版本仍等于当前版本。
