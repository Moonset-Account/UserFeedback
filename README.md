# 安全转储计划器（Safe Dump Planner）

共享计算节点崩溃后，在预算内保留最有诊断价值的物理页片段，同时执行跨安全域
许可交集、间接指针闭包、字段/字节裁切和受限地址消隐。本仓库只实现题面要求的
防御性规划与校验，不包含漏洞利用、入侵、攻击或规避载荷；所有验证数据均为本地
合成数据。

运行环境：Python 3.11.6，仅使用标准库，无需安装依赖。

## 运行测试

在当前工作区根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

运行合成示例：

```bash
python3 -m examples.synthetic_demo
```

## 公开 API

最常用入口：

```python
from safe_dump_planner import (
    plan_safe_dump,      # 别名 plan_dump
    materialize_dump,
    DumpRequest,
    PageSpec,
    VirtualAlias,
    DomainPolicy,
    PolicyRule,
    DiagnosticTarget,
    FragmentNeed,
    PointerRef,
    Crop,
    FieldSpec,
    RecordSpec,
    ByteRange,
)
```

- `plan_safe_dump(request: DumpRequest | dict | None = None, **kwargs) -> DumpPlan`
  计算保留片段、预算选择、指针下游需求和可解释理由。传入字典时支持
  `data_hex` 字段，便于直接使用 JSON 风格数据。
- `materialize_dump(request, plan=None) -> SafeDumpArtifact`
  按计划复制合成页字节、原地等长清零敏感地址，并再次扫描已知受限地址编码。
  如果仍发现泄漏，抛出 `UnsafeOutputError`。
- `DumpPlan.explain() -> str`
  输出人类可读的目标状态、丢弃理由和保留片段。
- `DumpPlan.to_dict()` / `SafeDumpArtifact.to_dict(include_plan=False)`
  输出 JSON 可序列化结构。artifact 默认序列化是公开输出安全的：只含不透明句柄、
  偏移和已消隐字节；授权控制面可用 `control_plane_dict()` 或
  `to_dict(include_plan=True)` 获取逻辑页 ID 与完整解释。

### 主要输入对象

| 对象 | 用途 |
| --- | --- |
| `PageSpec` | 物理页、页大小、可选合成字节、`role="generic"|"stack"|"allocation"`、栈帧/分配记录 schema |
| `VirtualAlias` | 某安全域中的虚拟别名到物理页映射，也用于确定相干域和受限地址 |
| `DomainPolicy` / `PolicyRule` | 域内容许可、字段/区间许可、地址披露许可、拒绝规则 |
| `DiagnosticTarget` | 带优先级的诊断目标，可按页、字节区间、字段、记录或多个 `FragmentNeed` 描述 |
| `PointerRef` | 显式指针槽、目标页/别名/虚拟地址、下游字段或区间需求 |
| `Crop` | 转储器可选的字段、记录、字节区间白名单裁切 |
| `budget_bytes` | 保留页字节总预算；掩码仍占原槽位长度，裁切形成的空洞不计费 |

区间统一为半开区间 `[start, end)`。相邻或重叠区间会合并，共享物理页的重叠
需求按并集计费，不会重复保留。

## 核心安全规则

1. **多域许可是交集，不是并集。**  
   物理页的相干域来自虚拟别名、`owner_domain`、显式 `coherent_domains` 以及
   能解析到该页的指针域。一个片段只有在每个相干域策略都允许时才可保留。
   页面计划中保留 `domain_allowed_ranges`，拒绝理由会指出是哪些域拒绝了哪个
   子区间。`domain="*"` 策略只作为“没有显式策略的相干域”的回退；完全无相干域
   时可传 `default_policy`，否则默认拒绝。

2. **地址许可与内容许可分开。**  
   即使某段内容允许导出，只要任一相干域不允许披露该页地址，该页的物理/虚拟
   地址以及指向它的指针都视为受限地址信息。

3. **只有完整且明文保留的指针才产生下游读取。**  
   - 指针槽被字段/区间裁切排除：状态为 `omitted`，不添加下游字节。
   - 只保留部分指针：保留部分会被掩码，不产生下游读取。
   - 目标地址受限或指针无法解析且采用默认安全设置：指针被清零，不跟随。
   - 完整、许可且未命中秘密地址扫描：才加入 `downstream` 或
     `downstream_target_id` 指定的读取需求；指针环通过单调闭包和不动点处理。

4. **输出前主动消隐和复核。**  
   计划器会扫描已知受限地址的 4/8 字节大端/小端编码及十六进制文本形式。
   `materialize_dump` 对指针槽和扫描命中位置做等长清零，并在返回前重新扫描。
   公开 manifest 只包含 `page-0001` 这类不透明句柄和页内偏移，不包含物理/虚拟
   地址；完整 `DumpPlan` 是控制面结果，包含逻辑 `page_id` 和解释信息。

5. **预算优化。**  
   目标的优先级数值越大越重要。默认 `objective="lexicographic"` 先逐层最大化
   各优先级支持数量，再最大化总数/优先级，最后最小化保留字节和区间签名；也支持
   `objective="weighted"` 与 `"count"`。共享片段按并集计费，指针闭包自动扩展
   成本。一般的共享片段预算选择是 NP-hard，因此实现使用确定性分支搜索；在
   `max_search_nodes` 内给出精确最优，超过节点上限时会在
   `plan.optimization.search_truncated` 中如实标记，而不会把启发式结果声称为
   最优。当前合成测试规模均为精确搜索。

## 可解释状态和理由码

目标状态包括：

- `retained`：所需片段完整保留且无强制消隐。
- `retained_redacted`：安全片段已保留，但部分地址字节被掩码；结果中
  `redacted=true`、`complete=false`。
- `security_denied`：未通过所有相干域许可，常见码为
  `SECURITY_DENIED` / `NO_RELEASE_POLICY`。
- `cropped`：转储器裁切白名单删除了目标所需字节，码为
  `CROPPED_BY_DUMPER`。
- `budget_denied`：独立或合并闭包超出预算；`details` 包含
  `standalone_required_bytes`、`combined_plan_bytes`、`marginal_bytes`、
  `remaining_budget_bytes` 和 `deficit_bytes`。
- `downstream_denied`：完整指针会产生安全策略禁止的下游读取。
- `invalid_selector`：未知页/记录/字段或区间越界等。

指针状态包括 `clear_retained`、`omitted`、`masked_restricted_address`、
`masked_unresolved`、`masked_partial_pointer` 和
`masked_address_pattern`。

## 简例

```python
from safe_dump_planner import *

request = DumpRequest(
    pages=[
        PageSpec(
            "shared-slab", size=128, role="allocation",
            data=(0x8000).to_bytes(8, "little") + b"alloc-v7",
        ),
        PageSpec("downstream-record", size=128, role="allocation"),
    ],
    aliases=[
        VirtualAlias("shared-a", "shared-slab", 0x4000, "a"),
        VirtualAlias("shared-b", "shared-slab", 0x6000, "b"),
        VirtualAlias("down-a", "downstream-record", 0x8000, "a"),
    ],
    policies=[
        DomainPolicy.full("a", addresses=True),
        DomainPolicy(
            "b",
            allow_all_addresses=True,
            allow=[
                PolicyRule(page_id="shared-slab", ranges=[(0, 64)]),
                PolicyRule(page_id="downstream-record", whole_page=True),
            ],
        ),
    ],
    pointers=[
        PointerRef(
            pointer_id="alloc-next",
            page_id="shared-slab",
            offset=0,
            target_page="downstream-record",
            value=0x8000,
            downstream=FragmentNeed(
                page_id="downstream-record", ranges=[(0, 32)]
            ),
        )
    ],
    targets=[
        DiagnosticTarget("primary", priority=10,
                         page_id="shared-slab", ranges=[(0, 48)]),
    ],
    budget_bytes=80,
)

plan = plan_safe_dump(request)
print(plan.retained_bytes)       # 80：48 B 主页 + 32 B 下游
print(plan.explain())
artifact = materialize_dump(request, plan)
assert artifact.verify_no_restricted_addresses(request) == []
```

## 已验证范围

当前测试套件包含 34 个 `unittest` 用例，覆盖：

- 半开区间边界、相邻合并、重叠、交集、差集和恰好等于页大小的边界。
- 物理页 + 多个虚拟别名 + 多安全域的许可交集；支持按 `alias_id` 选择目标，
  也支持用域和 `target_virtual_address` 解析指针；字段级策略、通配策略和默认拒绝。
- `stack`、`stack_frame`、`allocation`、分配记录和命名字段解析。
- 字段/字节裁切、全局 `Crop` 白名单、未知字段和越界选择器。
- 完整指针产生下游读取；裁切、部分保留、受限目标和未解析指针均不产生下游。
- 指针环不动点、共享片段并集计费、预算恰好相等和差一字节边界。
- 优先级层、同优先级目标数、`count` 目标和最小字节/稳定签名决胜。
- 120 个确定性随机交错区间案例，将计划结果与独立暴力子集枚举比较目标向量和
  最小成本。
- 4/8 字节及文本形式的受限地址扫描、指针消隐、原始数据中的偶发地址命中，以及
  materialize 后的二次泄漏校验。
- dict/JSON 风格请求、`to_dict()` JSON 序列化和公开 manifest 不含地址字段。

规划器依赖显式提供的指针/字段元数据和已知地址编码扫描；它不会执行崩溃页中的
代码，也不尝试从任意机器指令中自动恢复所有指针。这是有意的防御性边界。
