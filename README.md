# 联合登录声明投影模块（Claim Projection）

面向联合登录场景的声明投影：输入**已验证的身份属性**、**受众允许的用途**
与**带版本的用户同意记录**，按用途授权与最小披露原则输出允许签发的声明。

## 需求约束与实现映射

| 题面约束 | 实现位置 |
| --- | --- |
| 部分字段只有在特定用途下才可发送 | `registry.DEFAULT_ATTRIBUTE_PURPOSES` 登记每个原始属性所需用途；`projector.build_projection` 在访问任何原始属性前先做用途门控 |
| 衍生字段需要访问额外原始属性时，必须满足相应用途授权 | `registry.ClaimSpec.required_detail_purposes()` 汇总明细依赖的全部用途；跨用途衍生声明 `employment_verification` 同时要求 `employment` 与 `team`（`test_boundary.py::test_derived_claim_needs_every_purpose`） |
| 能用已验证布尔证明满足请求时只发该证明 | `employment_status`、`team` 带 `BooleanProofSpec`；默认请求路径仅输出布尔值，工号/团队标识不泄露（`test_acceptable.py::test_boolean_proof_minimization_hides_detail_attributes`） |
| 必需字段无法授权时停止本次签发 | `build_projection` 遇到必需声明失败立即返回 `issued=False`、`claims={}` 并在 `blocked` 中记录原因；严格入口 `project_claims` 抛 `ClaimProjectionError` |
| 可选字段无法授权时省略 | 可选声明失败进入 `ProjectionResult.omitted`，后续声明继续处理 |
| 同意更新时整份投影使用一致的同意版本 | 同意记录为完整快照，投影只取版本号最高的一条；有效用途 = 受众允许用途 ∩ 该版本授予用途（见 `tests/test_consent_versions.py`） |

## 目录与实际入口

```
claim_projection/
├── __init__.py     # 公共 API 导出
├── models.py       # Attribute / ClaimRequest / ConsentRecord / ProjectionResult
├── registry.py     # 属性-用途注册表与默认声明目录
└── projector.py    # build_projection（结果入口）/ project_claims（严格入口）
tests/
├── test_acceptable.py         # 可接受结果（7 个）
├── test_blocked.py            # 应被阻止/拒绝（9 个）
├── test_boundary.py           # 边界与交错场景（13 个）
└── test_consent_versions.py   # 同意版本一致性（9 个）
```

公共入口（均为真实实现，无桩代码）：

- `claim_projection.build_projection(attributes, audience_purposes, consent_records, requests)`
  → 返回 `ProjectionResult`，包含 `claims / omitted / blocked / issued /
  consent_version / effective_purposes`，授权失败不抛异常。
- `claim_projection.project_claims(...)` → 严格入口，成功返回声明字典，
  必需声明被阻止时抛 `ClaimProjectionError`。

## 默认声明目录

| 声明 | 类型 | 所需用途 | 最小披露行为 |
| --- | --- | --- | --- |
| `name` | 明细（`given_name` + `family_name`，支持部分构造） | `profile` | 无布尔证明，直接构造姓名 |
| `employment_status` | 布尔证明 + 明细 | `employment` | 默认只发 `is_active_employee` 布尔值；`require_detail=True` 时发工号 |
| `team` | 布尔证明 + 明细 | `team` | 默认只发 `is_team_member` 布尔值；`require_detail=True` 时发团队 ID/名称 |
| `employment_verification` | 跨用途衍生字段 | `employment` + `team` | 必须同时满足两个用途，访问任一额外原始属性都要求授权 |

输入支持三种属性写法：裸值（视为已验证）、`(value, verified)` 二元组、
`Attribute` 对象；同意记录支持 `ConsentRecord` 对象或
`{"version": int, "granted_purposes": [...], "revoked": bool}` 字典。

## 验证范围与运行方式

运行环境：Python 3.11.6，仅使用标准库，无第三方依赖。测试均为真实断言，
覆盖一个可接受结果、一个应被阻止/拒绝的结果，以及影响两者的边界/交错场景：

- **可接受结果**（`tests/test_acceptable.py`，7 个）：全授权投影、布尔证明
  最小化、显式明细展开、`false` 证明原样输出、跨用途衍生字段双用途齐备。
- **阻止/拒绝**（`tests/test_blocked.py`，9 个）：必需声明无用途即停止且
  清空已收集声明、严格入口抛异常、可选声明省略、受众允许与用户同意的交集
  缺失、未验证/缺失属性、未知声明、非布尔证明值。
- **边界/交错**（`tests/test_boundary.py`，13 个）：布尔证明缺失时回退明细
  路径（回退后仍受用途门控）、跨用途恰好缺一个用途、可选失败后必需成功、
  必需失败短路、重复请求去重、空请求、部分姓名属性、`None` 视为缺失。
- **同意版本一致性**（`tests/test_consent_versions.py`，9 个）：整份投影
  采用最高版本、高版本收窄/撤回后旧授权失效、撤回后重新授予、无同意记录、
  乱序版本输入、重复版本号被拒绝。

在仓库根目录执行：

```bash
# 一次性运行全部 38 个验收用例
python3 -I -m unittest discover -s tests -v

# 也可按文件独立运行（每个文件都带 unittest 真实断言入口）
python3 -I tests/test_acceptable.py
python3 -I tests/test_blocked.py
python3 -I tests/test_boundary.py
python3 -I tests/test_consent_versions.py
```

`-I`（隔离模式）下当前目录不在 `sys.path`，各测试文件已显式插入仓库根
目录保证导入稳定。最近一次全量运行结果：**Ran 38 tests — OK**。

## 设计边界

- 本模块只做本地声明投影决策，不发起任何网络请求、不读取环境凭据。
- 阻止原因码集中定义在 `claim_projection/projector.py`
  （`REASON_*` 常量），便于上层审计与日志映射。
- 声明目录可通过 `build_projection(..., registry=...)` 整体替换，
  新增声明只需提供 `ClaimSpec`，核心门控逻辑无需修改。
