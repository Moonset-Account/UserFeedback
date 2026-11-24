# 留存裁决模块（retention_decisions）

调查卷归档后，合规人员可能收到只保全某几项原始字段的通知。本模块在给定日期上裁决：

- 哪些**原始字段**还须留在**受控保全视图**（controlled preservation view）；
- 哪些内容应从**普通调查视图**撤下；
- **派生件**（引用原始字段的衍生材料）随其引用字段如何升降级；
- 完整的**字段 → 派生件引用关系**及每条边在裁决日的状态。

纯标准库实现（仅依赖 `dataclasses`、`datetime`、`enum`、`typing`），无第三方依赖，
Python 3.11.6 验证通过。模块不读取时钟、不做任何 I/O，所有裁决只依赖注册数据和
传入的 `as_of` 日期，因此历史回看完全确定。

## 目录结构

```
retention_decisions/
  __init__.py        # 公开 API 导出
  models.py          # 领域模型与裁决结果数据类
  engine.py          # RetentionEngine 裁决引擎与一次性便捷函数
tests/
  test_basic_lifecycle.py        # 普通期限、到期边界、两列保全其余列照旧
  test_artifact_references.py    # 派生件裁决与引用关系
  test_notice_timeline.py        # 生效/失效边界、重叠、更正、历史回看
  test_validation_and_isolation.py  # 输入校验、材料隔离、便捷函数
smoke_demo.py        # 合成数据端到端演示
```

## 公开 API

### 领域对象（`retention_decisions.models`，均为不可变 dataclass）

| 类型 | 说明 |
| --- | --- |
| `Material(material_id, fields, normal_expiry)` / `.create(...)` | 一份归档材料：字段清单与普通留存到期日 |
| `PreservationNotice(notice_id, material_id, fields, effective_date, expires_on=None, summary="", amends_notice_id=None)` / `.create(...)` | 带生效时间的保全通知，可声明可选失效日与"更正某通知"关系 |
| `DerivedArtifact(artifact_id, material_id, referenced_fields)` / `.create(...)` | 派生件及其引用的原始字段 |
| `RetentionStatus` | 枚举：`NORMAL` / `PRESERVED` / `EXPIRED` |

通知时间语义：

- 生效区间为半开区间 `[effective_date, expires_on)`；不填 `expires_on` 即永久有效。
- `amends_notice_id` 表示本通知是一份**更正**：自本通知生效日起，被更正的旧通知
  当日停止生效，保全范围以更正通知的字段集为准；更正失效（到达自身 `expires_on`）
  后，旧通知恢复适用。更正自身生效日之前（含历史回看）仍由旧通知裁决。

### 引擎（`retention_decisions.engine`）

- `RetentionEngine()`
  - `register_material(material)`
  - `register_notice(notice)`：校验材料/字段存在、更正目标合法（同材料、已注册、不得自更正）
  - `register_artifact(artifact)`：校验引用字段存在
  - `decide(material_id, as_of) -> RetentionDecision`
- `decide_retention(materials, notices, artifacts, material_id, as_of)`：一次性便捷封装

### 裁决结果 `RetentionDecision`

| 属性 | 内容 |
| --- | --- |
| `controlled_view_fields` / `controlled_view_artifacts` | 当日须置于受控保全视图的字段/派生件（有序元组） |
| `withdrawn_fields` / `withdrawn_artifacts` | 当日应从普通调查视图撤下的内容（= 非 NORMAL 的全部项） |
| `normal_view_fields` / `normal_view_artifacts` | 仍留在普通调查视图的内容 |
| `field_decisions` | 每个字段的 `FieldDecision`：状态、当日生效通知 ID 列表、普通到期日 |
| `artifact_decisions` | 每个派生件的 `ArtifactDecision`：状态、引用字段、内嵌引用边、保全它的通知 ID |
| `reference_links` | 全部 `ReferenceLink(artifact_id, material_id, field_name, field_status)` 引用边 |
| `field_status(name)` / `artifact_status(id)` | 单项状态查询 |

## 裁决规则

1. **字段三态**（裁决日 `as_of`）：
   - 被任一当日有效通知覆盖 → `PRESERVED`，进入受控保全视图并从普通视图撤下；
   - 无有效通知且 `as_of >= normal_expiry`（到期日当天即算到期）→ `EXPIRED`，
     从普通视图撤下，且不在受控视图中；
   - 其余 → `NORMAL`，留在普通视图。
2. **部分列保全**：通知覆盖一份材料中的两列（或任意子集）时，其余列继续按原
   普通期限计算，通知对它们没有任何影响。
3. **派生件随引用字段**：
   - 任一引用字段处于 `PRESERVED` → 派生件 `PRESERVED`（受控视图保留、普通视图撤下），
     并通过 `holding_notice_ids` 给出保全来源；
   - 否则，只要全部引用字段均 `EXPIRED` → 派生件 `EXPIRED`（失去全部支撑，撤下）；
   - 其余情况（至少一个引用字段仍 `NORMAL`）→ `NORMAL`。
   - 不引用任何字段的派生件不会被保全也不会到期，保持 `NORMAL`。
4. **历史回看**：`decide(mid, 过去日期)` 只选取生效区间覆盖该日期的通知，注册顺序
   不影响结果。后到达的**更正**按其自身 `effective_date` 影响回看：回看日早于更正
   生效日则旧通知仍然有效；回看日达到更正生效日则按更正裁决（即使更正物理上是
   后来才录入的）。

## 运行方法

```bash
# 标准测试命令（题面指定）
python3 -m unittest discover -s tests -v

# Python 隔离模式（忽略 PYTHONPATH/用户站点包），需显式指定 top-level 目录
python3 -I -m unittest discover -s tests -t .

# 合成数据端到端演示
python3 smoke_demo.py
```

## 验证范围

全部为本地合成数据，无联网、无真实目标、无外部依赖。35 个 unittest 用例覆盖：

- 正常场景：通知生效前全部 `NORMAL`；生效后被覆盖列进入受控视图、其余列照旧；
  派生件按引用关系升降级；引用边逐字段状态与保全通知溯源。
- 边界：通知生效日前一天/当天；普通到期日前一天/当天（到期日当天即到期）；
  通知 `expires_on` 半开区间（最后有效日与失效日当天）；字段与派生件空集。
- 交错/更正：重叠通知共同列名汇总；注册顺序无关；更正自生效日起收窄保全范围、
  更正失效后旧通知恢复；未来生效的更正不影响当天；历史回看只采用当时有效的
  通知，后到更正按自身生效日影响回看；同一引擎重复回看结果确定。
- 隔离与校验：多材料通知/派生件/引用边互不可见；未知材料/字段、重复注册、
  非法更正目标、非法日期区间、错误参数类型均被拒绝；模型不可变。
- 已在 Python 3.11.6 上以普通模式与 `python3 -I` 隔离模式各执行一次全部用例，
  均为 `OK`（35 通过、0 失败）；`smoke_demo.py` 实际输出与上述规则逐条核对一致。

## 设计说明（解释性裁决）

- "从普通调查视图撤下"按状态而非视图位置定义：凡不是 `NORMAL` 的字段/派生件都
  出现在 `withdrawn_*` 中；其中 `PRESERVED` 项同时出现在受控视图，`EXPIRED` 项
  不出现在任何视图。`controlled_view_*` 与 `normal_view_*` 两个集合互不相交。
- 通知本身只做"加法保全"；需要表达"更正/收窄/撤销"时使用 `amends_notice_id`
  显式建模取代关系，避免让两份通知的字段集隐式相减而产生歧义。
- 所有输出元组均按名称排序，保证结果可直接比较、可回放。
