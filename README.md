# SEC-20261114-059 — 多来源告警字段白名单与用途隔离代号转换器

为外发给外部值班团队的告警提供 JSONL 转换器：网关（gateway）与终端（endpoint）
两种来源各自配置字段策略，账号类字符串统一替换为基于 HMAC-SHA-256 的代号，
保证跨来源关联能力（同一账号同代号）且用途隔离（不同用途不同代号）。

## 运行环境

- Python 3 标准库，无第三方依赖、无网络访问。
- 本仓库内所有输入均为本地合成样例；密钥库 `keys.json` 为合成测试密钥。

## 目录结构

```
app.py                 CLI 主入口
transform.py           核心转换逻辑（路径提取、白名单构造、代号生成）
policies/gateway.json  网关来源字段策略
policies/endpoint.json 终端来源字段策略
keys.json              本地合成密钥库：{tenant: {key_version: hexkey}}
samples/               合成样例输入与任务文件
tests/                 unittest 测试套件
tools/local_verify.py  本地核验入口（单测 + 样例/边界断言）
```

## 本地可重跑入口

```bash
cd <workspace>
/usr/local/bin/python3 tools/local_verify.py
```

该命令先通过 `unittest.defaultTestLoader.discover("tests")` 运行测试套件
（0 个测试、全部跳过、任何失败/错误都以非零退出），再执行 S1–S4 样例/边界
断言（逐条打印 actual vs expected），全部通过才退出 0。

单独运行转换器：

```bash
/usr/local/bin/python3 app.py \
  --source gateway \
  --task samples/task.json \
  --input samples/gateway.jsonl \
  --output /tmp/out.jsonl \
  --errors /tmp/errors.jsonl
# 终端来源：--source endpoint --input samples/endpoint.jsonl
```

可选参数：`--policy <path>`（显式策略文件）、`--keystore`（默认 `keys.json`）、
`--policies-dir`（默认 `policies`）。

## 输入格式

- **输入 JSONL**：每行一个 JSON 对象（告警记录）。空行忽略；非 JSON、非对象、
  必填缺失、替换字段非字符串的行被跳过并记入错误报告。
- **任务文件**（可信上下文）：`{"task_id": ..., "tenant": "tenant-a",
  "key_version": "v1"}`。转换器只从任务文件取租户与密钥版本，再按
  `(tenant, key_version)` 从本地密钥库取 HMAC 密钥；查不到密钥即失败退出。
- **策略文件**（按来源各一份）：
  ```json
  {
    "source": "gateway",
    "keep": ["ts", "src.ip"],
    "replace": [{"path": "user.name", "purpose": "account"}],
    "required": ["ts", "user.name"]
  }
  ```
  - `keep`：原样保留的字段路径（点分路径，可指向嵌套对象）。
  - `replace`：要替换为代号的字符串字段路径及其用途（purpose）。
  - `required`：必填字段路径；任一路径在记录中解析不到任何值即跳过该行。
  - 路径经过数组时对其每个元素按同一规则处理（如 `alerts.owner` 作用于
    `alerts` 数组中每个对象的 `owner`）。同一路径不得同时出现在 keep 与
    replace 中（策略加载时报错）。

## 输出语义

- **外发 JSONL**：每行一个记录，**只**由策略声明的字段构造；未声明字段
  （含嵌套与数组元素内的字段）一律不出现。记录顺序与输入有效行顺序一致。
- **代号**：`cod_` + HMAC-SHA-256(key, `tenant \0 key_version \0 purpose \0
  value`) 的前 24 个十六进制字符。摘要输入不含字段路径，因此：
  - 同一 `tenant + key_version + purpose` 下，两种来源即使字段路径不同，
    相同账号字符串得到相同代号（跨来源关联）；
  - 更换租户、密钥版本或用途，代号随之变化（用途隔离）。
- **错误报告 JSONL**：每条仅含 `{"source", "line", "reason"}` 三个键
  （行号从 1 开始），不携带原始记录内容。reason 取值稳定：
  `invalid JSON`、`line is not a JSON object`、
  `missing required field: <path>`、`non-string value at replace path: <path>`。
- **确定性**：相同输入重跑得到字节级相同的输出与错误报告（顺序处理 +
  确定性 HMAC + `sort_keys` 序列化）。

## 限制

- 账号值按精确字符串匹配，不做大小写/空白归一化。
- 必填判定为“路径至少解析到一个值”；必填路径下数组为空视为缺失。
- 已声明的数组字段在输入中为空数组时，输出保留为 `[]`（结构保留）。
- `keep` 声明到对象时保留其整个子树（该子树即视为已声明）。
- 密钥库为本地合成文件，仅模拟“可信密钥来源”，不连接真实 KMS。

## 需求 → 测试/样例映射

| 需求 | 验证 |
| --- | --- |
| 两来源分别配置字段策略 | `policies/gateway.json`、`policies/endpoint.json`；`PolicyTests`；S1/S2 |
| 可指定嵌套对象/数组元素中的字段 | `FieldPolicyTests.test_nested_keep_path`、`test_array_elements_processed_by_same_rules`；S2 |
| 策略声明保留/替换/用途 | `PolicyTests`（含非法策略拒绝）；S1 |
| 可信租户与密钥版本来自外发任务 | `CliEndToEndTests.test_cli_unknown_key_fails`；S1–S4 均经任务文件 |
| 基于 HMAC-SHA-256 生成代号 | `CrossSourceCodenameTests.test_codename_format`；S1（独立重算比对） |
| 同租户/版本/用途跨路径同代号 | `...test_same_account_same_codename_across_sources`；S2 |
| 换租户/密钥版本/用途代号变化 | `...test_codename_changes_with_{tenant,key_version,purpose}`；S4 |
| 外发记录只从声明字段构造 | `FieldPolicyTests.test_output_only_contains_declared_fields`；S1 |
| 数组中的对象按同一原则处理 | `FieldPolicyTests.test_array_elements_processed_by_same_rules`；S2 |
| 必填缺失/类型错误跳过该行 | `SkipAndErrorTests`（缺失/类型/非法 JSON/非对象）；S1/S2 |
| 错误报告只含来源/行号/原因 | `SkipAndErrorTests.test_error_report_entries_have_only_source_line_reason`；S1 |
| 相同输入重跑结果一致 | `DeterminismTests.test_rerun_produces_identical_output`；S3 |
