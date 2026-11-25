# 跨域互认票据信任校验

纯 Python 3.11 实现，无第三方依赖。服务收到的票据摘要假定**已经完成验签**；本模块只处理票据声明的起始域、目标域、实际中转域、压缩父链段以及目标服务的审查策略，不做网络请求、密钥读取或真实身份提供方交互。

## 运行测试

```bash
python3 --version
python3 -m unittest discover -s tests -v
```

当前工作区实测：`Ran 31 tests ... OK`。

也可运行纯本地合成端到端示例：

```bash
python3 examples/demo.py
```

## 公开 API

推荐使用有状态校验器：

```python
from trust_chain import TrustValidator, ParentSegment

validator = TrustValidator(
    # 每个 key 是签发下一跳的域，value 是它能负责签发/授权的紧邻下一跳。
    issued_hops={
        "partner.example": {"audit.example"},
        "audit.example": {"gateway.example"},
        "gateway.example": {"app.example"},
    },
    # 子域 -> 直接父域；只负责还原压缩段，不自动授予信任。
    parents={},
    # 某些来源必须经过指定审查域。
    review_requirements={
        "partner.example": "audit.example",
    },
)

result = validator.validate({
    "origin": "partner.example",
    "target": "app.example",
    "transit": ["audit.example", "gateway.example"],
})

print(result.allowed, result.code, result.position)
print(result.expanded_path)
print(result.to_dict())
```

也可以使用函数式 API：

```python
from trust_chain import validate_ticket, verify_ticket, first_failure_position

result = validate_ticket(ticket, issued_hops, parents, review_requirements)
allowed, position, code = verify_ticket(ticket, issued_hops=..., parents=...)
position_or_none = first_failure_position(ticket, ...)
```

常用导出包括：

- `TrustValidator` / `TrustChainValidator`
- `validate_ticket` / `validate_trust_chain` / `validate_chain`
- `verify_ticket` / `first_failure_position`
- `Ticket`、`ParentSegment`（别名 `CompressedSegment`）、`ReviewRequirement`、`ValidationResult`
- 结果码常量：`OK`、`UNTRUSTED_HOP`、`PARENT_CYCLE`、`TICKET_CYCLE`、`MISSING_PARENT_EDGE`、`AMBIGUOUS_SEGMENT`、`REVIEW_MISSING`、`ORIGIN_MISMATCH`、`TARGET_MISMATCH`、`INVALID_TICKET`

根目录的 `solution.py` 重新导出了同一套 API，因此也可以写 `from solution import validate_ticket`。

## 输入结构

### 票据

标准形式：

```python
{
    "origin": "a.example",          # 也接受 start/source/start_domain 等
    "target": "z.example",          # 也接受 destination/to/audience 等
    "transit": ["b.example", ...],  # 也接受 intermediates/hops/via 等
}
```

`transit`/`intermediate_domains` 不包含起始域和目标域。也可以用 `path`/`route`/`chain` 给出包含端点的完整路径：

```python
{
    "origin": "a",
    "target": "e",
    "path": ["a", {"start": "a", "end": "e", "compressed": True}],
}
```

压缩段支持以下写法：

```python
ParentSegment(start="a", end="d")
ParentSegment(end="d")          # 接在上一个路径域之后时可推断 start
{"start": "a", "end": "d"}
{"from": "a", "to": "d", "compressed": True}
("a", "d")                       # 二元字符串元组/列表
"a..d"                           # 双点压缩写法
["a", "...", "d"]                # 路径中的省略号写法
```

### 签发的下一跳信任关系

```python
issued_hops={
    "a": {"b", "shortcut-z"},
    "b": {"c"},
}
```

也接受边列表：

```python
issued_hops=[("a", "b"), ("b", "c")]
# 或 [{"issuer": "a", "next_hop": "b"}]
```

校验严格按票据还原后的相邻边进行：对于 `x -> y`，必须有 `y ∈ issued_hops[x]`。信任图中即使存在 `a -> z` 的可信短路，只要票据走的是 `a -> b -> z`，仍会检查 `a -> b` 和 `b -> z`；若 `b` 未签发 `z`，就在 `b` 处返回 `UNTRUSTED_HOP`，不会用拓扑短路改写路径。

### 父域关系

```python
parents={"a": "b", "b": "c", "c": "d"}
```

也接受 `(child, parent)` 边列表，或将一个子域配置为多个候选父域：

```python
parents={"a": ["b1", "b2"], "b1": "d", "b2": "d"}
```

压缩段会逐级沿 `child -> parent` 还原：

- 遇到没有父边且尚未到达段终点：`MISSING_PARENT_EDGE`；
- 父链重复：`PARENT_CYCLE`；
- 存在不止一条能从段起点到达段终点的父链：`AMBIGUOUS_SEGMENT`；
- 额外的、无法到达该段终点的死分支不会影响由段终点唯一确定的还原路径。

父域关系只用于还原票据压缩段；还原出的每条边仍必须在 `issued_hops` 中获得签发域授权。

### 审查策略

简单映射表示所有目标都适用：

```python
review_requirements={"partner": "audit"}
```

目标级策略：

```python
review_requirements=[
    {"source": "partner", "reviewer": "audit", "targets": "secret-app"},
    {"source": "partner", "reviewer": "cn-audit", "targets": ["cn-app"]},
]
```

默认只把票据起始域作为 `"source"` 匹配；构造 `TrustValidator(..., review_source_scope="any_domain")` 后，会对展开路径中出现的任何策略来源生效，并返回最早的漏审来源位置。

只有**票据实际展开路径**中出现审查域才算经过审查。信任拓扑或父域拓扑中存在审查域、但票据路径未经过它，不能通过检查。

## 返回位置约定

`ValidationResult` 主要字段：

| 字段 | 含义 |
| --- | --- |
| `allowed` / `ok` / `valid` | 是否通过 |
| `code` / `reason` | 稳定结果码和可读说明 |
| `expanded_path` | 已处理部分还原出的完整域名序列 |
| `position` | 责任域在 `expanded_path` 中的下标 |
| `domain` | 责任域：失信边的签发域、父链错误的当前子域、漏审来源域 |
| `next_domain` | 被拒绝的下一跳 |
| `edge_index` | 相邻边序号；普通路径中等于签发域下标 |
| `item_index` | 原始 `transit`/`path` 条目序号，可定位压缩段 |
| `cycle_domain` / `cycle_start_position` | 环路回到的域及其首次出现位置 |
| `source_position` | 漏审来源位置 |
| `target_position` / `details["enforcement_position"]` | 目标服务执行审查策略的位置 |
| `required_reviewer` / `source` / `target` | 审查策略上下文 |

校验按票据顺序快速失败：先还原并检查较早的边/段；所有跳均可信后再汇总审查要求。这样第一个失信跳不会被后续段错误或末端策略掩盖。漏审结果同时给出来源责任位置和目标强制执行位置，避免“位置”语义歧义。

域名默认做防御性规范化：去除首尾空白、转小写、忽略根尾点。可传 `normalize_domains=False` 关闭。

## 已验证范围

`tests/test_trust_chain.py` 全部使用本地合成域名和策略，覆盖：

1. 正常显式路径；
2. 压缩父链段、字典/元组/双点/省略号写法；
3. 压缩段中的审查域在展开后被正确认可；
4. 首个未授权下一跳及其签发域、下标、边序号；
5. 父域关系不隐式授予信任；
6. 可信短路不能替代票据实际路径；
7. 父链缺边、环路、多父链不可唯一还原；
8. 票据显式路径环路；
9. 漏审、目标级审查策略、`any_domain` 最早来源；
10. 拓扑中存在审查域但票据未经过时仍判漏审；
11. 显式域与压缩段交错、前后错误顺序；
12. 零边 `origin == target`、零长度段、可推断段起点；
13. 非法票据、来源/目标不一致、域名规范化和紧凑元组 API。

未覆盖真实 SSO 协议、密码学验签、密钥管理、网络服务和性能压测；这些不属于题面要求。
