# 取证证物双复核分配模块

一个仅使用 Python 3.11 标准库的小批证物分配器。每件被覆盖的证物都会得到**完整的两位复核人**分配；模块不会返回只找到一位复核人的“半分配”。

核心约束：

- 两位复核人必须不同；
- 两位复核人必须来自不同实验室；
- 接触过该证物同源组材料，或被显式标记为该证物回避的复核人，不能参与；
- 每位复核人的在办件数不能超过其 `capacity`；
- 默认两位复核人都必须单独满足证物列出的全部资质；
- 先最大化完整覆盖件数，再按证物优先级选择覆盖集合；
- 未分配证物返回结构化的资质、回避、实验室或名额原因；
- 复核人前端只能取得自己的匿名案号；具名信息只通过调度视图提供给授权人员。

## 运行环境与测试

环境：Python 3.11.6，无第三方依赖、无需联网。

运行全部测试：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成示例：

```bash
python3 demo.py
```

也可通过兼容入口 `allocator.py` 使用：

```python
import allocator
result = allocator.allocate(exhibits, reviewers)
```

## 公开 API

推荐从包入口导入：

```python
from forensic_allocator import (
    Exhibit,
    Reviewer,
    allocate,
    reviewer_tasks,
    reviewer_task_payload,
    reviewer_case_numbers,
    dispatch_view,
)
```

### 输入模型

```python
Exhibit(
    exhibit_id="E-1001",
    required_qualifications=frozenset({"DNA"}),
    source_group="SRC-A",       # 同源组；可为 None
    priority=100,               # 默认数值越大优先级越高
)

Reviewer(
    reviewer_id="r1",
    lab="LAB-NORTH",
    qualifications=frozenset({"DNA"}),
    capacity=2,                 # 可接收件数
    name="Zhang San",           # 调度视图使用；可空
    exposed_source_groups=frozenset({"SRC-A"}),  # 接触过、须回避的同源组
    recused_exhibit_ids=frozenset(),              # 显式证物回避/既往接触
    active=True,
)
```

`allocate()` 也接受普通字典，并支持常见别名字段和中文键，例如：

```python
{
    "证物ID": "E1",
    "所需资质": ["DNA"],
    "同源组": "G1",
    "优先级": 10,
}
```

复核人字典支持 `"复核人ID"`、`"姓名"`、`"所属实验室"`、`"资质"`、`"可接收件数"`、`"接触同源组"` 等键。

### 分配入口

```python
result = allocate(
    exhibits,
    reviewers,
    tie_break="lexicographic",       # 或 "priority_sum"
    priority_direction="larger",     # 或 "smaller"（数字越小优先级越高）
    anonymous_scope="case",          # 或 "review"（每个复核席位不同匿名号）
    anonymous_secret="server-secret",# 省略则使用 secrets 生成随机号
    qualification_mode="each",       # 或 "collective"
    source_group_recusals={"G1": ["r1"]},  # 可选：同源组 -> 须回避复核人
    exhibit_recusals={"E1": ["r2"]},       # 可选：证物 -> 须回避复核人
)
```

返回 `AllocationResult`，主要字段：

- `assignments`：完整分配，每项含证物 ID、两位复核人、实验室和匿名案号；
- `unassigned`：未分配证物的结构化诊断；
- `coverage_count` / `maximum_coverage`：覆盖件数；
- `coverage_exhibit_ids`：被覆盖证物；
- `loads` / `capacities`：每位复核人当前件数和名额；
- `exhibits` / `reviewers`：规范化后的输入，供调度视图解析名称。

算法会枚举每件证物的所有合法双人组合，再用按剩余名额状态记忆化的动态规划求真实最大覆盖，而不是贪心近似。常见的“全部证物均可覆盖”场景使用 MRV 回溯快速路径。该问题在一般形式下是指数级的，因此本模块按题面“小批证物”设计，不建议直接用于大规模排班。

### 优先级平局规则

默认 `tie_break="lexicographic"`：

1. 先比较覆盖件数；
2. 再按优先级从高到低检查每张“是否覆盖”的二元序列；
3. 在最高优先级证物上，只要存在同最大件数方案能覆盖它，就优先覆盖；然后比较下一优先级；
4. 在最大件数和优先级覆盖集合都相同的情况下，人员组合由确定性搜索/复核人排序决定，保证相同输入可复现。

如果业务上把 `priority` 当作可相加权重，可使用 `tie_break="priority_sum"`，在最大件数相同的方案中最大化优先级权重总和。`priority_direction="smaller"` 适用于“1 是最高优先级”的排名式字段。

## 未分配诊断

每个 `UnassignedReport` 包含：

- `primary_reason`：主原因；
- `reason_codes`：全部命中的结构化原因码；
- `message`：中文说明；
- `missing_qualifications`：当前可参与且未回避的复核人整体无人持有的资质；
- `reviewer_qualification_gaps`：每位可参与复核人缺少的资质；
- `recused_reviewer_ids` / `inactive_reviewer_ids`；
- `eligible_reviewer_ids`：通过资质/回避筛选的复核人；
- `cross_lab_pair_count`：忽略名额后仍存在的合法异实验室双人组合数；
- `candidate_pairs`：这些组合在最优分配中的剩余名额和饱和阻塞人；
- `minimum_additional_slots`：若按当前最优结果直接补入该证物，最少需要给候选组合增加几个名额。

主原因取值：

| `primary_reason` | 含义 |
| --- | --- |
| `qualification_gap` | 缺少足够资质合格的双人组合；详见资质缺口 |
| `capacity_constraint` | 存在资质/回避/实验室均合法的组合，但最优覆盖下名额不足 |
| `recusal_conflict` | 回避约束是无法组成合法双人组合的直接因素之一 |
| `lab_constraint` | 合格人员只能组成同实验室组合 |
| `no_eligible_pair` | 人员总数、在岗状态等导致没有合法双人组合 |

当主原因是 `capacity_constraint` 时，测试会断言每个候选异实验室组合至少有一名饱和复核人；否则可以直接补入该证物，与“最大覆盖”矛盾。

## 前端匿名视图与调度视图

### 复核人前端

应从已认证会话取得 `reviewer_id`，然后只返回该复核人的任务：

```python
payload = reviewer_task_payload(result, "r2")
# [{"anonymous_case_number": "ANON-...."}]
```

也可以使用：

```python
reviewer_tasks(result, "r2")          # 强类型 ReviewerTaskView
reviewer_case_numbers(result, "r2")   # ("ANON-....",)
```

`ReviewerTaskView` 只有 `anonymous_case_number` 一个字段。序列化后的复核人载荷不包含真实证物 ID、同源组、资质、复核人 ID/姓名、共同复核人或实验室。查询未知复核人会抛出 `UnknownReviewerError`。

两种匿名范围：

- `anonymous_scope="case"`（默认）：同一件证物的两位复核人拿到相同匿名案号；前端仍看不到共同复核人或具名分配。
- `anonymous_scope="review"`：每个复核席位得到不同且不可相互关联的匿名号码，进一步强化两次复核的隔离性；调度目录仍可把两个号码映射回同一证物。

提供 `anonymous_secret` 时，匿名号由服务端密钥 HMAC-SHA256 派生；省略时使用 `secrets` 生成 128 位随机号。密钥只能保存在服务端，不能下发前端。

本模块不实现登录、认证或 HTTP 授权。调用方必须把 URL/会话中的身份与已认证主体绑定，并确保只有授权调度角色可以调用 `dispatch_view()`。不要把完整 `AllocationResult` 直接序列化给复核人前端。

### 授权调度人员

```python
view = dispatch_view(result)
```

调度视图包含真实证物 ID、优先级、同源组、复核人 ID/姓名、实验室、匿名号映射目录、名额负载和未分配原因。示例见 `python3 demo.py`。

## 验证范围

`tests/` 中包含：

1. **正常场景**：两位资质合格、不同实验室的复核人完成分配；字典和中文输入键可用。
2. **回避边界**：同源组接触记录、显式证物回避，以及通过 `source_group_recusals` / `exhibit_recusals` 单独传入的回避映射都会排除相关复核人。
3. **实验室边界**：同实验室组合被拒绝，并可选择其他实验室组合。
4. **资质缺口**：单人缺少特定资质、某项资质整体无人持有、集体资质模式。
5. **名额边界**：零名额、容量饱和、负载不超限、总名额计算。
6. **交错竞争**：构造了一个“若先选字典序最前的组合，就会占用两名稀缺双资质复核人并少覆盖一件”的案例；测试断言优化器选择重组后的组合并达到最大覆盖。
7. **优先级平局**：验证默认字典序优先级和“数字越小优先级越高”的排名模式；另有 `priority_sum` 模式测试。
8. **随机合成数据与独立穷举 oracle**：60 个随机种子、两种平局模式共 120 个子场景，逐一比较最大覆盖数和最优覆盖集合，并校验回避、资质、实验室、容量和诊断一致性。
9. **隐私视图**：断言复核人 JSON 只含 `anonymous_case_number`，且不泄漏真实证物号、姓名、复核人 ID 或实验室；`review` 范围下两个席位号码不同；调度视图保留具名信息。
10. **输入校验**：重复 ID、空输入、负容量、未知复核人、非法参数。
11. **规模冒烟**：12 件证物、8 名复核人的全量覆盖和容量受限场景。

最近一次完整执行结果以 `python3 -m unittest discover -s tests -v` 的实际输出为准；编写 README 时套件为 30 个测试全部通过（其中随机 oracle 测试内部包含 120 个子场景）。
