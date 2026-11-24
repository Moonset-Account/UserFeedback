# 共享服务账号授权核对组件

这是一个仅使用 Python 标准库的防御性授权核对组件。它针对“共享服务账号接手项目”场景，核对两类批准：

1. **业务负责人批准**（`business_owner`，别名 `bo`/`business`/`owner`）：业务所有人同意服务账号或对应主体接手项目。
2. **账号保管人批准**（`account_custodian`，别名 `ac`/`custodian`）：账号保管人同意将该共享主体用于目标服务。

组件会把不同目录中的身份解析为同一自然人，强制两项职责由**不同自然人**承担，并对批准有效期做区间相交、边界衔接和交错组合计算，输出可开通区间、责任人、备选责任组合、缺口以及缺少的独立批准类型。

## 公开 API

包名为 `authorization_checker`，主要导出：

```python
from authorization_checker import (
    check_authorization,  # evaluate_authorization 的语义别名
    evaluate_authorization,
    AuthorizationChecker,
    AuthorizationRequest,
    Approval,
    ApprovalKind,
    Identity,
    NaturalPerson,
    PersonRegistry,
    Interval,
    Scope,
    UnresolvedIdentityError,
    IdentityConflictError,
)
```

### 一次性函数

```python
decision = evaluate_authorization(
    payload=None,
    *,
    approvals=None,
    identity_links=None,
    registry=None,
    strict_identities=True,
    target_service="shared-report-sa",
    project_id="proj-apollo",
    interval=Interval(start, end),   # 也可用 start=..., end=...
)
```

也可以直接传入 JSON 风格字典：

```python
decision = evaluate_authorization({
    "target_service": "shared-report-sa",
    "project": "proj-apollo",
    "requested_interval": {
        "start": "2026-03-01T00:00:00Z",
        "end": "2026-06-01T00:00:00Z"
    },
    "identity_links": [
        {
            "person_id": "person-alice",
            "identities": ["corp:alice", "partner-portal:alice@partner.test"]
        }
    ],
    "approvals": [
        {
            "id": "bo-1",
            "type": "business_owner",
            "identity": "corp:alice",
            "valid_from": "2026-03-01T00:00:00Z",
            "valid_until": "2026-06-01T00:00:00Z",
            "project": "proj-apollo",
            "service": "shared-report-sa"
        }
    ]
})

print(decision.authorized)       # 整个请求区间是否均可开通
print(decision.grants)          # 主时间线：非重叠、带责任人的区间
print(decision.gaps)            # 无法开通的区间
print(decision.to_dict())       # JSON 可序列化结果
```

### 有状态检查器

重复评估多个请求时可使用 `AuthorizationChecker`：

```python
checker = AuthorizationChecker(
    approvals=[...],
    identity_links=[...],
    strict_identities=True,
)
decision = checker.evaluate(
    target_service="shared-report-sa",
    project_id="proj-apollo",
    start=start,
    end=end,
)
```

## 输入模型

### 请求

支持以下字段名：

- 目标服务：`target_service`、`service_id`、`service`、`service_account_id`
- 项目：`project_id`、`project`
- 区间：`requested_interval`、`interval`、`validity`，或顶层 `start`/`end`
- 可选管辖区域：`jurisdiction_id`/`jurisdiction`

### 批准

每条批准支持：

- ID：`id`、`approval_id`、`approvalId`
- 类型：`type`、`kind`、`approval_type`、`category`
- 身份：
  - `person_id`：已知自然人 ID；或
  - `identity`/`approver`/`principal`：`Identity`、`{"directory": ..., "subject": ...}` 或 `"directory:subject"`
- 有效期：
  - 半开区间：`valid_from` + `valid_until`（或 `start` + `end`）
  - JSON 日期可用 `valid_from: "YYYY-MM-DD"` 与 `valid_to: "YYYY-MM-DD"`；日期型 `valid_to` 按**含当天**处理，内部归一到次日零点
  - 日期时间建议显式使用 `valid_until` 表达半开结束点
- 管辖范围：
  - `project`/`project_id`/`projects`/`project_ids`
  - `service`/`service_id`/`services`/`service_ids`
  - `jurisdiction`/`jurisdictions`
  - 或传入 `scope={"project_ids": ..., "service_ids": ..., "jurisdiction_ids": ...}`

`Scope` 中某一维度为 `None` 表示该维度不限制；配置为集合时，目标值必须属于该集合。

### 跨目录自然人

可以通过 `identity_links` 提供映射：

```python
identity_links=[
    NaturalPerson(
        "person-alice",
        identities=(
            Identity("corp", "alice"),
            Identity("partner-portal", "alice@partner.test"),
        ),
    ),
    {"person_id": "bob", "directory": "corp", "subject": "bob"},
    ("carol", "partner-portal", "carol@partner.test"),
]
```

同一个身份被绑定到两个不同 `person_id` 时抛出 `IdentityConflictError`。

默认 `strict_identities=True`：管辖范围内的批准若既没有 `person_id`，也无法从 `PersonRegistry` 解析，会抛出 `UnresolvedIdentityError`。显式设置 `strict_identities=False` 时不会猜测跨目录身份，而是记录 `unresolved_identity` 发现并在责任人配对中**失败关闭地排除该批准**；结果中的 `identity_assurance_complete` 为 `False`。

## 时间与区间语义

所有核心区间均为半开区间 `[start, end)`。因此：

- `[t0, t1)` 与 `[t1, t2)` 在 `t1` 处正好交接，形成连续覆盖；
- `[t0, t1)` 与 `[t1 + 1小时, t2)` 之间会如实输出一个缺口；
- 支持可比较的有序端点；测试同时覆盖了 `datetime`、`date` 和数值端点。
- 若使用带时区的 `datetime`，同一区间两端必须同为感知时间或同为原生时间。

## 输出内容

`AuthorizationDecision` 的主要字段：

- `authorized` / `fully_authorized`：整个请求区间是否始终存在有效的不同自然人组合。
- `grantable`：是否至少有一段可开通区间。
- `grants`：主时间线，按时间排列且互不重叠；每段包含稳定的业务负责人和账号保管人。若原组合仍有效，即使出现其他备选组合，也不会无意义切换。
- `authorized_intervals`：字典输出中 `grants` 的语义别名。
- `feasible_pair_intervals`：每个可行的“业务负责人 + 账号保管人”组合及其最大连续区间；多个组合可能在时间上重叠，表示存在多个可选责任人。
- `continuous_coverage`：忽略责任人切换后，所有相邻可开通段合并成的最大连续覆盖区间。
- `gaps`：请求区间中不能开通的部分。
- `independence`：按项目/服务管辖范围内批准生成的职责独立诊断。
- `interval_independence`：仅按与请求时间相交的批准生成的诊断。
- `findings`：过期批准、同一人组合被忽略、身份无法解析等说明。
- `to_dict()`：生成 JSON 兼容字典。

每个 `Grant` 都包含：

```json
{
  "start": "...",
  "end": "...",
  "business_owner": {"person_id": "alice", "approval_ids": ["bo-1"]},
  "account_custodian": {"person_id": "bob", "approval_ids": ["ac-1"]}
}
```

## “同一人两份批准”时的缺失类型

诊断由 `independence.status` 和 `independence.requirement_groups` 表达：

| 管辖范围内的情况 | 状态 | 应补充的独立批准 |
| --- | --- | --- |
| 存在两个不同自然人分别承担两类职责 | `independent_pair_available` | 无 |
| 只有同一自然人的业务负责人批准（例如两个目录身份重复登记） | `missing_account_custodian` | 不同于该自然人的账号保管人批准 |
| 只有同一自然人的账号保管人批准 | `missing_business_owner` | 不同于该自然人的业务负责人批准 |
| 两类批准都没有 | `missing_both_types` | 两类都需要 |
| 同一自然人同时有业务负责人和账号保管人批准 | `same_person_only` | 可补充另一人的账号保管人批准，**或**另一人的业务负责人批准；二选一即可 |

便捷字段：

- `missing_independent_approval_types`：所有补救选项涉及的类型。
- `primary_missing_independent_approval_type`：只有缺失类型唯一时返回该类型，否则为 `None`。

当同一人同时持有两类批准时，逻辑上不存在唯一的“哪一类”答案：保留其业务负责人批准就需要另一人的保管人批准，保留其保管人批准就需要另一人的业务负责人批准。因此组件将其表达为一个“二选一”的补救选项组，而不是武断指定某一类。

## 核对算法

1. 按目标服务、项目和可选管辖区域过滤批准。
2. 用 `person_id` 或 `PersonRegistry` 将目录身份解析为自然人。
3. 生成职责分离和缺失批准类型诊断。
4. 将每条批准与请求区间相交；不相交者记录为有效期问题。
5. 收集所有有效期端点并扫描基本时间段。
6. 在每个时间段内按自然人分组，枚举 `business_person != custodian_person` 的组合。
7. 合并同一责任组合的相邻时间段，生成最大责任区间；同时合并跨责任人交接的连续覆盖区间。
8. 用请求区间减去覆盖区间得到缺口。

## 运行测试

环境：

```bash
python3 --version
# Python 3.11.6
```

无需安装依赖。在工作区根目录运行题目指定命令：

```bash
python3 -m unittest discover -s tests -v
```

最近一次本地复核结果：`22` 个测试全部通过（`OK`）。

也可以运行本地合成 JSON 示例：

```bash
python3 -m authorization_checker examples/interleaving_request.json
python3 -m authorization_checker examples/same_person_missing_category.json
```

第一个示例验证 Alice 的业务负责人批准贯穿全区间，Bob 与 Carol 的保管人批准在 `2026-04-15T12:00:00Z` 精确交接，因此整个请求可连续开通，但两个子区间的责任人组合不同。测试中还通过子进程实际执行该 CLI 并解析其 JSON 输出。

## 已验证范围

`tests/` 中的合成数据和断言覆盖：

- 两名不同自然人的正常开通区间；
- 部分覆盖时前后缺口；
- 不同责任组合在日期/精确时间边界上的交错交接；
- 端点相接时无缺口，相隔一个时间单位时正确报告缺口；
- 重叠的多个备选责任组合；
- 同一自然人在 `corp` 与 `partner-portal` 等不同目录中的身份归并；
- 同一自然人在相邻时间段切换目录身份时，仍合并为同一个责任人区间；
- 同一人两份同类型批准时，明确标出缺少另一类独立批准；
- 同一人同时持有两类批准时，报告职责冲突和“两类中补任意一类”的补救选项；
- 项目、服务、管辖区域不匹配时不计入授权；
- 过期批准不产生区间，但保留管辖范围诊断和 finding；
- 日期型 `valid_to` 含当天的归一化；
- 同一自然人的相邻批准合并为一个责任区间并保留全部批准 ID；
- 严格身份解析报错，以及非严格模式下失败关闭；
- `PersonRegistry` 映射和身份冲突；
- 决策结果 JSON 可序列化；
- 两个 `examples/*.json` 本地合成样例可通过 CLI 运行。

未覆盖、也未执行的内容包括：真实目录服务查询、网络访问、真实目标/凭据、大规模性能和持久化存储。所有样例均为本地合成主体与 `.test` 域名；本组件只实现题面要求的防御性核对，不包含任何利用、入侵、攻击或规避载荷。
