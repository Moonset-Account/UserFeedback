# GraphQL 解析器授权覆盖 CI 校验器 (SEC-VULN-030)

离线静态分析: 当多个对象类型实现同一接口时, 校验敏感字段在各种合法选择树中
是否始终绑定到明确的授权检查。纯 Python 3 标准库, 无第三方依赖。

## 运行

```bash
# CI 校验 (报告写 stdout, JSON)
/usr/local/bin/python3 app.py examples/schema.json examples/resolvers.json examples/operations.json

# 本地核验 (单元测试 + 样例/边界断言)
/usr/local/bin/python3 tools/local_verify.py
```

退出码: `0` 无 missing 结论; `1` 存在 missing (缺少授权检查, CI 应失败);
`2` 输入不合法或 IO 错误。

## 输入格式 (均为 JSON)

### schema.json — 精简 schema 表

```json
{"types": {"TypeName": {
  "kind": "object" | "interface",
  "fields": {"fieldName": {"type": "NamedType", "sensitive": true}},
  "interfaces": ["Iface"],          // object 可选, 声明顺序即继承查找顺序
  "possible_types": ["Impl"]        // interface 必填
}}}
```

只有 `sensitive: true` 的字段参与授权覆盖结论; 非敏感字段仍会被遍历以到达子选择。

### resolvers.json — 解析器授权契约与继承证据

```json
{
  "contracts": {"Type.field": {"checks": ["require_owner_or_admin"]}},
  "inheritance": [{
    "interface_field": "Node.internalNote",
    "implementation": "User",
    "resolver_inherits": true,
    "evidence": "ci://resolver-map/12345"
  }]
}
```

- `checks` 非空 -> 该实现可证实已覆盖; 空数组 -> 缺少授权检查 (missing)。
- 实现无直接契约时, 按 `interfaces` 声明顺序查找定义同名字段且有契约的接口;
  必须存在对应 `inheritance` 记录且 `evidence` 非空, 才能认定继承接口契约,
  否则结论为 manual_review (继承顺序证据缺失)。
- 既无直接契约也无可继承接口契约 -> diagnostics.missing_resolver_metadata
  单独报告, 结论为 manual_review。

### operations.json — 已解析的字段选择与命名片段

```json
{
  "operations": [{"name": "GetNode", "root_type": "Query",
                  "selections": [ ... ]}],
  "fragments": {"FragName": {"type_condition": "User", "selections": [ ... ]}}
}
```

选择节点:
- `{"kind": "field", "name": "ownerEmail", "alias": "contact", "selections": [...]}`
- `{"kind": "fragment_spread", "name": "FragName"}`
- `{"kind": "inline_fragment", "type_condition": "User", "selections": [...]}`

## 输出语义

报告 JSON 包含:

- `missing` / `covered` / `manual_review`: 三组结论, 每条含
  - `path`: 根操作到字段的路径 (段名为别名或实际字段名);
  - `field` / `alias`: 别名映射回的实际字段;
  - `applicable`: 按接口可能实现类型与片段类型条件求得的每个具体类型的
    命中契约 (`via`: direct / inherited / inheritance / none)、checks 与状态。
  - 路径级状态取最差: 任一实现 missing -> missing; 否则任一 manual_review
    -> manual_review; 否则 covered。
- `diagnostics.fragment_cycles`: 片段循环 (循环上的片段列表), 分析仍终止。
- `diagnostics.missing_resolver_metadata`: 缺失解析器元数据的类型/字段/路径。
- `diagnostics.unknown_fields`: 选择集中 schema 未定义的字段。

共享片段在每个根操作中独立求值 (各自生成独立路径)。片段/内联片段的类型
条件会收窄接口的可能实现类型, 只命中落在条件内的实现契约。

## 需求到测试的映射 (tests/test_checker.py)

| 需求 | 测试 |
| --- | --- |
| 基接口与具体实现权限一致 | `ConsistentPermissionsTest` |
| 某个实现遗漏校验 | `OneImplementationMissingTest` |
| 同名别名指向不同字段 | `SameAliasDifferentFieldsTest` |
| 继承顺序证据缺失 | `InheritanceEvidenceTest` |
| 修正一条契约只改变相关结论 | `ContractFixScopeTest` |
| 片段循环单独报告且分析终止 | `FragmentCycleTest` |
| 缺失解析器元数据单独报告 | `MissingResolverMetadataTest` |
| 共享片段多根操作独立求值 | `SharedFragmentTest` |
| 片段类型条件收窄实现集合 | `FragmentTypeConditionTest` |

`tools/local_verify.py` 额外对 `examples/` 样例逐条断言实际输出 (退出码、
missing/covered/manual_review 路径、别名映射、继承证据、元数据缺失诊断)。

## 限制

- 输入是上游解析器已展开的离线产物; 本工具不解析 GraphQL 文本、不连接任何
  真实服务。
- 继承证据只认定显式 `inheritance` 记录; 不静态分析解析器源码。
- 接口字段在实现中未重复声明时, 以接口定义判断敏感性; 实现中重复声明的
  敏感标记不参与契约查找 (契约键始终为 `Type.field`)。
