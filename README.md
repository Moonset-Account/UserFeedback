# SEC-VULN-043 — 高风险操作审批对象绑定回归套件

管理服务的批量导出（`batch_export`）属于高风险操作，需要**数据负责人**
（`data_owner`）和**审计人**（`auditor`）分别批准。历史缺陷：资源更新后，
两个审批人针对**不同版本**的决定被合并成一次批准。本套件用纯 Python 3
标准库实现内存回归，验证审批严格绑定到单一操作对象，且提交时原子校验
资源修订号。

## 运行入口（全部本地、离线、确定性）

```bash
# 完整核验：unittest 发现/执行检查 + 场景断言 + 边界断言
/usr/local/bin/python3 tools/local_verify.py

# 仅运行场景套件，输出每个决定的 JSON 报告（审批记录 + 触发规则）
/usr/local/bin/python3 app.py

# 仅运行单元测试
/usr/local/bin/python3 -m unittest discover tests
```

退出码：全部通过为 0；任何测试失败、零测试、全部跳过、场景/边界断言
不一致均为非 0。无 pip / venv / pytest / 外部依赖；典型运行耗时 < 1 秒。

## 文件结构

| 文件 | 职责 |
|---|---|
| `approval_core.py` | 参数规范化、操作绑定、审批评估（R1–R5）、原子提交（R6）、确定性调度器 |
| `identity_stub.py` | 可信身份桩：主体、角色、租户；唯一可签发审批记录的入口 |
| `scenarios.py` | 7 个确定性场景（S1–S7）的夹具与执行 |
| `app.py` | CLI 主入口：运行场景套件并输出 JSON 报告 |
| `tests/test_approval_regression.py` | 28 个 unittest 用例 |
| `tools/local_verify.py` | 核验包装器（发现≥1、实际执行且未跳过≥1、场景/边界逐条断言） |

## 输入格式

套件不读取外部输入；所有输入为 `scenarios.py` 中确定性构造的合成夹具：

- **主体**（可信身份桩签发）：`alice`（data_owner, tenant-a）、
  `bob`（auditor, tenant-a）、`carol`（同时持有两角色, tenant-a）、
  `dave`/`erin`（tenant-b）。
- **审批记录**：`{record_id, tenant, resource_id, resource_revision,
  operation, params, approver, role, decision, expires_at}`。
- **操作请求**：`{tenant, resource_id, resource_revision, operation,
  params}`；逻辑时钟固定 `now=1000`，审批到期 `expires_at=2000`。
- **绑定键**：`tenant | resource_id | resource_revision | operation |
  canonical(params)`。`canonical` 为规范化参数的 JSON（字典键递归排序、
  列表按规范化形式排序——顺序变化但语义相同的参数视为相同；内容变化
  则绑定键不同）。

## 审批规则（每个决定均输出触发的规则及状态）

| 规则 | 语义 |
|---|---|
| R1_BINDING_MATCH | 审批记录绑定键与操作绑定键完全一致（租户/资源/修订号/操作/规范化参数） |
| R2_DECISION_APPROVE | 记录决定为 `approve` |
| R3_NOT_EXPIRED | 未过期（`expires_at > now`，边界为排他） |
| R4_ROLE_COVERAGE | `data_owner` 与 `auditor` 两角色均有有效记录 |
| R5_DISTINCT_SUBJECTS | 两个角色由**两个不同主体**批准 |
| R6_ATOMIC_REVISION_MATCH | 提交时在锁内原子重读实时修订号并与审批绑定比对 |

批准条件：R1–R6 全部 pass。任何拒绝路径都**不产生导出记录**（套件逐条断言）。

## 输出语义

`app.py` 输出 JSON：每个 case 含 `case_id`、`expected`/`decision`、
`approvals_used`（使用的审批记录 id/审批人/角色）、`rules_triggered`
（规则/状态/明细）、`export_record`（批准时为 `exp-NNNN`，拒绝时为
`null`）、`export_count`。`all_ok` 为全部场景是否符合预期。

## 需求 → 场景/测试映射（实际运行结果）

| 需求 | case_id / 测试 | 输入要点 | 期望 | 实际输出（本轮运行） |
|---|---|---|---|---|
| 正常双人审批 | `S1_normal_dual_approval` | alice(data_owner)+bob(auditor) 批准 tenant-a/res-1@rev7 | approved | approved，R1–R6 全 pass，导出 `exp-0001` |
| 同一人兼任两角色 | `S2_same_subject_both_roles` | carol 以两角色各批一条 | rejected | rejected，R5_DISTINCT_SUBJECTS:fail，export=null |
| 跨租户同名资源 | `S3_cross_tenant_same_name` | 审批绑 tenant-a/res-1@7，请求 tenant-b/res-1@7 | rejected | rejected，R1_BINDING_MATCH:fail，export=null |
| 同租户不同资源同修订号 | `S4_same_revision_different_resource` | 审批绑 res-1@7，请求 res-2@7 | rejected | rejected，R1_BINDING_MATCH:fail，export=null |
| 参数顺序变化语义相同 | `S5_param_order_semantic_same` | `{"tables":["beta","alpha"],"format":"csv"}` | approved | approved，R1 pass，导出 `exp-0001` |
| 参数内容变更 | `S6_param_content_changed` | tables 中 beta→gamma | rejected | rejected，R1_BINDING_MATCH:fail，export=null |
| 校验后提交前修订变化 | `S7_revision_changed_before_commit` | 确定性调度器在提交前 bump 7→8 | rejected | rejected，R6 fail（`7 != live revision 8`），export=null |
| 拒绝不产生导出记录 | `test_rejection_produces_no_export_record` 等 | 全部 rejected case | export_count==0 | 通过 |
| 到期边界 | `test_expiry_boundary_is_exclusive` / B3 | `expires_at == now` | 无效 | 通过（rejected，无导出） |
| 身份桩强制 | `IdentityStubTest` | 未知主体/无角色/跨租户签发 | PermissionError | 通过 |

## 限制

- 纯内存实现，逻辑时钟（整数），无持久化、无网络、无真实身份系统；
  外部系统由本地身份桩与合成记录模拟。
- 并发以 `DeterministicScheduler` 的固定交错模拟（校验→资源更新→提交），
  保证可重放；非真实多线程压力测试。
- 列表参数按无序集合规范化；若业务需要有序列表语义，需调整
  `normalize_params`。
