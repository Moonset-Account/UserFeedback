# SEC-VULN-006 — 签名下载对象的规范化一致性检查

## 背景与目标

下载服务按对象编号授权。一次存储迁移后，签名校验模块与对象定位模块可能
以不同规则理解同一条请求记录。本工具对**已解析的合成记录**做离线一致性
检查：用两套可配置规则分别计算“授权对象”和“实际对象”的规范化形式，
仅当两者在**租户、对象编号、版本**上完全一致时该记录才通过；否则输出
分歧报告。

纯 Python 3 标准库实现，无第三方依赖，不访问网络与外部系统。

## 目录结构

```
app.py                 CLI 主入口
core.py                规则集、规范化与一致性比对逻辑
samples/records.json   交付样例：合成记录（含分歧与通过用例）
samples/records_passing.json  交付夹具：全部通过的记录（边界断言用）
samples/rules.json     交付样例：两套规则配置
tests/                 unittest 测试
tools/local_verify.py  本地核验入口（发现+运行测试，并执行样例/边界断言）
```

## 运行

```bash
# 一致性检查（缺省读取 samples/ 下样例）
/usr/local/bin/python3 app.py [--input records.json] [--rules rules.json] [--output report.json]

# 本地核验（测试 + 样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py

# 仅跑单元测试
/usr/local/bin/python3 -m unittest discover -s tests -v
```

退出码：`0` 无分歧；`1` 存在分歧（核验脚本为失败）；`2` 输入/配置错误。

## 输入格式

### 记录（--input，JSON 数组）

| 字段 | 含义 |
| --- | --- |
| `record_no` | 来源编号（记录标识，进入报告） |
| `tenant` | 租户或租户别名 |
| `object_no` | 对象编号 |
| `version` | 版本；数组表示**重复版本字段**，`null`/缺省表示**版本丢失** |
| `query` | 需保留的查询项（键值对） |

### 规则（--rules，`{"rules_a": {...}, "rules_b": {...}}`）

每套规则字段：

| 字段 | 含义 |
| --- | --- |
| `name` / `version` | 规则名与版本（版本进入报告） |
| `tenant_aliases` | 旧别名 → 规范租户名映射 |
| `known_tenants` | 规范租户名集合；不在别名表且不在集合中的租户视为**未解析的租户别名**，该规则拒绝此记录 |
| `duplicate_version` | 重复版本字段处理：`first` / `last` / `reject` |
| `missing_version` | 版本丢失处理：`default`（用 `default_version`）/ `reject` |
| `ignored_query_keys` | **可忽略的追踪字段**，支持 fnmatch 通配（如 `trace_id`、`utm_*`），不参与比对 |

## 输出语义

报告为 JSON 数组，按 `record_no` 排序（与输入顺序无关）。每条仅包含：

```json
{"record_no": "r-002", "rule_versions": ["sig-1.2", "loc-2.0"], "divergent_fields": ["version"]}
```

- `divergent_fields` 取值于 `tenant` / `object_no` / `version` / `query`；
- 仅一侧规则拒绝记录时，分歧字段为该侧无法规范化的字段；
- 两套规则在同一字段上一致拒绝同一记录时视为理解一致（`both_rejected`），不进报告。

## 三类边界的不同处理结果

1. **重复版本字段**：按各套规则的 `duplicate_version` 策略取首个/末个或拒绝；
   两套策略不同且重复值不同时产生 `version` 分歧。
2. **未解析的租户别名**：该套规则拒绝此记录；仅一侧无法解析时产生
   `tenant` 分歧，两侧一致拒绝则不算分歧。
3. **可忽略的追踪字段**：规范化时丢弃，永不参与比对，不会导致分歧。

## 需求 → 测试/样例映射

| 需求点 | 覆盖位置 |
| --- | --- |
| 兼容的旧别名 | `tests/test_consistency.py::LegacyAliasTest`；样例 `r-001` |
| 对象版本丢失 | `MissingVersionTest`；样例 `r-002`（分歧 `version`） |
| 同编号跨租户 | `CrossTenantTest`；样例 `r-003`/`r-004`；核验边界断言 5 |
| 输入顺序变化 | `InputOrderTest`；核验边界断言 2 |
| 重复版本字段 | `DuplicateVersionTest`；样例 `r-005`；核验边界断言 3 |
| 未解析租户别名 | `LegacyAliasTest` 后两项；样例 `r-007`（分歧 `tenant`） |
| 可忽略追踪字段 | `TrackingFieldTest`；样例 `r-006`；核验边界断言 4 |
| 报告仅含三项内容 | `ReportShapeTest`；核验样例断言 1 |

## 限制

- 仅处理已解析的合成记录（JSON），不做签名校验、不访问真实存储/云服务；
- 记录字段缺失（`record_no` 除外）按各规则策略处理，`record_no` 缺失时报告中为 `null`；
- 查询项值统一按字符串比较。
